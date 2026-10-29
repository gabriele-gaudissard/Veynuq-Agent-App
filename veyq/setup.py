"""Optional Windows model-engine setup from the publisher's signed installer."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import requests


def engine_available():
    client = requests.Session()
    client.trust_env = False
    try:
        response = client.get("http://localhost:11434/api/version", timeout=(2, 2), allow_redirects=False)
        return response.status_code == 200
    except requests.RequestException:
        return False
    finally:
        client.close()


def setup_engine(data_root):
    if os.name != "nt":
        raise RuntimeError("Automatic local-engine setup is available on Windows.")
    if engine_available():
        return {"ok": True, "message": "Local engine is already running."}
    executable = shutil.which("ollama") or str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe")
    if not Path(executable).is_file():
        folder = Path(data_root) / "setup"
        folder.mkdir(exist_ok=True)
        installer = folder / "local-engine-setup.exe"
        client = requests.Session()
        client.trust_env = False
        try:
            with client.get("https://ollama.com/download/OllamaSetup.exe", stream=True, timeout=(10, 90)) as response:
                response.raise_for_status()
                size = 0
                with installer.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > 2_000_000_000:
                            raise RuntimeError("Installer exceeds the download limit.")
                        output.write(chunk)
            literal = str(installer).replace("'", "''")
            check = "Import-Module (Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop; $s=Get-AuthenticodeSignature -LiteralPath '" + literal + "'; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Ollama'){exit 1}"
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", check], creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
            if result.returncode:
                raise RuntimeError("Local-engine installer signature is invalid or has an unexpected publisher.")
            result = subprocess.run([str(installer), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], creationflags=subprocess.CREATE_NO_WINDOW, timeout=600)
            if result.returncode:
                raise RuntimeError("Local-engine installation failed: " + str(result.returncode))
        finally:
            client.close()
            installer.unlink(missing_ok=True)
        executable = str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe")
    if not Path(executable).is_file():
        raise RuntimeError("Engine executable was not found after installation.")
    # Server is an independent user service, not a terminal window or elevated process.
    subprocess.Popen([executable, "serve"], creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if engine_available():
            return {"ok": True, "message": "Local engine is ready. Choose a model in the catalog."}
        time.sleep(.5)
    raise RuntimeError("Engine startup timed out. See its logs or choose a remote API.")
