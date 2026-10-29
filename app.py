"""Veynuq desktop launcher."""
import os
import sys
import faulthandler
import logging
from pathlib import Path


def startup_log():
    """Keep startup errors visible even when launched through pythonw."""
    import json
    data_dir = os.environ.get("VEYNUQ_DATA_DIR") or os.environ.get("VEYQ_DATA_DIR")
    profile = Path(__file__).parent / "profile.json"
    if not data_dir and profile.exists():
        try: data_dir = json.loads(profile.read_text(encoding="utf-8"))["data_dir"]
        except (OSError, ValueError, KeyError): pass
    folder = Path(data_dir) if data_dir else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Veynuq"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "startup.log"
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(folder / "startup.previous.log")
    stream = path.open("a", encoding="utf-8", buffering=1)
    logging.basicConfig(stream=stream, level=logging.WARNING)
    if sys.stderr is None:
        sys.stderr = stream
    if sys.stdout is None:
        sys.stdout = stream
    faulthandler.enable(file=stream)
    if not any(flag in sys.argv for flag in ('--install','--prepare-images','--prepare-all','--self-check','--background')):
        faulthandler.dump_traceback_later(30, file=stream)
    return stream, path


def ensure_dependencies(stream):
    import importlib.util
    import subprocess
    required = ["webview", "requests", "bs4", "cryptography", "pypdf", "PIL", "playwright", "faster_whisper", "sounddevice", "mcp", "jsonschema", "jedi", "packaging"]
    if os.name == "nt":
        required.append("pywinauto")
    if any(importlib.util.find_spec(name) is None for name in required):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(Path(__file__).parent / "requirements.txt")],
                                stdout=stream, stderr=stream, creationflags=flags, timeout=600)
        if result.returncode:
            raise RuntimeError("Dependency installation failed. Run Installer.bat; details are in startup.log.")

if __name__ == '__main__':
    gui_launch = sys.stdout is None
    diagnostic_stream, diagnostic_path = startup_log()
    try:
        # A scheduled read-only worker must never install software in the background.
        if '--background' not in sys.argv: ensure_dependencies(diagnostic_stream)
        from veyq.desktop import main
        main()
    except Exception as error:
        faulthandler.cancel_dump_traceback_later()
        logging.exception("Veynuq startup failed")
        if os.name == 'nt' and gui_launch and '--background' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, str(error) + "\n\nDiagnostica: " + str(diagnostic_path), 'Veynuq - Avvio non riuscito', 0x10)
        else:
            print(str(error), file=sys.stderr)
        sys.exit(1)
