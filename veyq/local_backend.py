"""Resolve an owned local Ollama llama.cpp worker for supported tool choice."""
import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

import requests

log = logging.getLogger("veyq.engine")


def split_command(command):
    count = ctypes.c_int()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    result = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not result:
        return []
    try:
        return list(result[:count.value])
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(result)


def worker_from_snapshot(snapshot, model_path, api_port):
    processes = {row["ProcessId"]: row for row in snapshot.get("processes", [])}
    listeners = snapshot.get("listeners", [])
    owners = {row["OwningProcess"] for row in listeners if row.get("LocalPort") == api_port
              and row.get("LocalAddress", "127.0.0.1") in {"127.0.0.1", "0.0.0.0", "::1", "::"}}
    for row in processes.values():
        if row.get("Name", "").lower() != "llama-server.exe" or row.get("ParentProcessId") not in owners:
            continue
        parent = processes.get(row["ParentProcessId"], {})
        if parent.get("Name", "").lower() != "ollama.exe" or not parent.get("ExecutablePath") or not row.get("ExecutablePath"):
            continue
        parent_path = Path(parent["ExecutablePath"])
        worker_path = Path(row["ExecutablePath"])
        expected = (parent_path.parent / "lib" / "ollama" / "llama-server.exe").resolve()
        if worker_path.resolve() != expected:
            continue
        argv = split_command(row.get("CommandLine", ""))
        def flag(name):
            try:
                return argv[argv.index(name) + 1]
            except (ValueError, IndexError):
                return ""
        if not flag("--model") or Path(flag("--model")).resolve() != Path(model_path).resolve():
            continue
        if flag("--host") not in {"127.0.0.1", "localhost", "::1"}:
            continue
        try:
            port = int(flag("--port"))
        except ValueError:
            continue
        if not 1024 <= port <= 65535:
            continue
        if not any(value.get("OwningProcess") == row["ProcessId"] and value.get("LocalPort") == port for value in listeners):
            continue
        return "http://127.0.0.1:" + str(port)
    return None


def discover_worker(base, metadata, token=""):
    parsed = urlsplit(base)
    if (os.name != "nt" or token or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.path.rstrip("/") or metadata.get("template_source") != "gguf_chat_template"):
        return None
    # A fixed read-only OS query associates both listening ports with the
    # native Ollama process and its own worker. No port scanning is used.
    model_path = next((line[5:].strip().strip('"') for line in metadata.get("modelfile", "").splitlines()
                       if line.startswith("FROM ")), "")
    if not model_path or not Path(model_path).is_absolute():
        return None
    port = parsed.port or 80
    script = (
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "$p=@(Get-CimInstance Win32_Process -Filter \"Name='ollama.exe' OR Name='llama-server.exe'\" | "
        "Select-Object Name,ProcessId,ParentProcessId,ExecutablePath,CommandLine); "
        "$c=@(Get-NetTCPConnection -State Listen | Select-Object LocalPort,LocalAddress,OwningProcess); "
        "@{processes=$p;listeners=$c}|ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            return None
        root = worker_from_snapshot(json.loads(result.stdout), model_path, port)
        return {"root": root, "model_path": model_path} if root else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError):
        log.info("Local tool-choice bridge unavailable")
        return None


def verify_worker(session, candidate):
    try:
        response = session.get(candidate["root"] + "/props", timeout=(2, 5), allow_redirects=False)
        try:
            response.raise_for_status()
            props = response.json()
        finally:
            response.close()
        if not isinstance(props, dict) or not isinstance(props.get("chat_template"), str):
            return None
        if Path(props.get("model_path", "")).resolve() != Path(candidate["model_path"]).resolve():
            return None
        capacity = props.get("default_generation_settings", {}).get("n_ctx")
        if type(capacity) is not int or capacity < 1:
            return None
        return {**candidate, "capacity": capacity, "slots": props.get("total_slots")}
    except (requests.RequestException, ValueError, TypeError, OSError):
        return None


def renew_lease(base, model, capacity, stop):
    """Keep the native scheduler aware of a run using its direct worker."""
    with requests.Session() as client:
        client.trust_env = False
        while not stop.wait(60):
            try:
                with client.post(base + "/api/chat", json={
                    "model": model, "messages": [], "stream": False,
                    "options": {"num_ctx": capacity}, "keep_alive": "10m",
                }, timeout=(5, 15), allow_redirects=False) as response:
                    response.raise_for_status()
            except requests.RequestException:
                log.warning("Local worker lease refresh failed")
