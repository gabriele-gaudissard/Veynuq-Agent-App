"""Atomic local state, credential vault and metadata-only audit log."""
import base64
import copy
import ctypes
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


DEFAULTS = {
    "lang": "en", "provider": "local", "url": "http://localhost:11434",
    "model": "qwen3:14b", "permission": "auto", "network": False,
    "workspace": "", "max_steps": 0, "command_timeout": 0,
    "github_repo": "", "context_chars": 60000, "auto_update": True, "vision": False,
    "execution_environment": "host",
    "desktop_scope": "all", "background_schedules": False,
    "image_provider": "managed", "image_url": "http://127.0.0.1:7860", "image_model": "",
    "context_tokens": 16384, "response_tokens": 4096,
    "collapsed_projects": [],
    "disabled_tools": [],
}


def activity_title(text):
    line=text.strip().splitlines()[0] if text.strip() else 'New activity'
    if len(line)<=100: return line
    prefix=line[:99]
    if ' ' in prefix: prefix=prefix.rsplit(' ',1)[0]
    return prefix.rstrip()+'…'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def atomic_bytes(path, value):
    """Write a complete file before replacing its destination."""
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Vault:
    """Windows DPAPI (current user); system keyring on other platforms."""
    def __init__(self, root):
        self.path = Path(root) / "credentials.dpapi"
        self.lock = threading.RLock()

    def _crypt(self, data, decrypt=False):
        class Blob(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        buf = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        result = Blob()
        crypt = ctypes.windll.crypt32
        fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        # CRYPTPROTECT_UI_FORBIDDEN: no prompts; bound to this Windows user.
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise RuntimeError("Impossibile accedere al vault Windows.")
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            ctypes.windll.kernel32.LocalFree(result.data)

    def _load(self):
        if not self.path.exists():
            return {}
        return json.loads(self._crypt(base64.b64decode(self.path.read_bytes()), True))

    def get(self, name):
        with self.lock:
            if os.name == "nt":
                return self._load().get(name, "")
            import keyring
            return keyring.get_password("veyq-agent", name) or ""

    def set(self, name, value):
        with self.lock:
            if os.name == "nt":
                data = self._load()
                if value:
                    data[name] = value
                else:
                    data.pop(name, None)
                encoded = base64.b64encode(self._crypt(json.dumps(data).encode()))
                temp = self.path.with_suffix(".tmp")
                temp.write_bytes(encoded)
                os.replace(temp, self.path)
            else:
                import keyring
                if value:
                    keyring.set_password("veyq-agent", name, value)
                elif keyring.get_password("veyq-agent", name):
                    keyring.delete_password("veyq-agent", name)


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,})\b", "[REDACTED]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    return text


class Store:
    def __init__(self, root=None, legacy=None):
        data_home = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share")))
        existing = data_home / "Veyq"
        default_root = existing if (existing / "state.json").exists() else data_home / "Veynuq"
        self.root = Path(root or os.environ.get("VEYNUQ_DATA_DIR") or os.environ.get("VEYQ_DATA_DIR") or default_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.lock = threading.RLock()
        self.vault = Vault(self.root)
        self.recovery_notice = ""
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(self.data, dict) or not isinstance(self.data.get("settings"), dict) or not isinstance(self.data.get("sessions"), list):
                    raise ValueError("Invalid local database structure")
            except (ValueError, UnicodeError):
                quarantine = self.root / ("state.corrupt-" + str(time.time_ns()) + ".json")
                os.replace(self.path, quarantine)
                previous = self.root / "state.previous.json"
                try:
                    self.data = json.loads(previous.read_text(encoding="utf-8"))
                    if not isinstance(self.data, dict) or not isinstance(self.data.get("settings"), dict) or not isinstance(self.data.get("sessions"), list):
                        raise ValueError("Invalid backup")
                    self.recovery_notice = "Local data recovered from the previous valid snapshot. The damaged file was preserved."
                except (OSError, ValueError, UnicodeError):
                    self.data = {"settings": {}, "sessions": [], "projects": [], "memory": ""}
                    self.recovery_notice = "A damaged database was preserved separately. No valid backup was available; a new local database was created."
        else:
            self.data = {"settings": {}, "sessions": [], "projects": [], "memory": ""}
            if legacy and Path(legacy).exists():
                old = json.loads(Path(legacy).read_text(encoding="utf-8"))
                token = old.get("settings", {}).pop("token", "")
                if token:
                    self.vault.set("provider", token)
                self.data.update(old)
                # Scrub the legacy plaintext credential only after vault succeeds.
                atomic_json(legacy, old)
        self.data["settings"] = {**DEFAULTS, **self.data.get("settings", {})}
        self.data.setdefault("projects", [])
        self.data.setdefault("memory", "")
        self.data.setdefault("automations", [])
        self.data.setdefault("plugins", [])
        self.data.setdefault('tool_environments', [])
        for job in self.data['automations']:
            if job.get('last_state') == 'running':
                job.update(last_state='interrupted', enabled=False)
        from .durability import recover
        for session in self.data["sessions"]:
            recover(session)
        self.data["settings"].pop("token", None)
        # Old installation had unrestricted execution. Migration defaults to auto.
        for session in self.data.get("sessions", []):
            session.setdefault("workspace", "")
            session.setdefault("plan", [])
            session.setdefault("project_id", "")
        self.save()
        # Windows MSIX filesystem redirection can map newly created profile
        # files to a cache while resolving the existing logical directory to
        # another location. Bind all subsequent paths to the actual state file.
        self.path = self.path.resolve()
        self.root = self.path.parent
        self.vault = Vault(self.root)

    def save(self):
        with self.lock:
            if self.path.exists():
                try:
                    previous = json.loads(self.path.read_text(encoding="utf-8"))
                    atomic_json(self.root / "state.previous.json", previous)
                except (ValueError, UnicodeError):
                    pass
            atomic_json(self.path, self.data)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def audit(self, run_id, tool, outcome, digest=""):
        row = {"time": datetime.now(timezone.utc).isoformat(), "run": run_id,
               "tool": tool, "outcome": outcome, "digest": digest}
        with self.lock:
            path = self.root / "audit.jsonl"
            if path.exists() and path.stat().st_size > 5_000_000:
                os.replace(path, path.with_suffix(".previous.jsonl"))
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
