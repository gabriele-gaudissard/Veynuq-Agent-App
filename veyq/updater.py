"""Automatic updates from the publisher's public GitHub Releases only."""
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

from .network import public_request
from .storage import atomic_json
from .signing import verify_manifest

UPDATE_REPO = "gabriele-gaudissard/Veynuq-Agent-App"
ROOT_FILES = {"app.py", "index.html", "app.js", "app.css", "i18n.js", "ui-translations.json", "Veyq.bat", "Veynuq.bat", "Launcher.ps1", "install.ps1", "PythonSetup.ps1",
              "Installer.bat", "Installer_only_shortcut.bat", "requirements.txt", "README.md",
              "LICENSE", "THIRD_PARTY_NOTICES.md", "build.json"}
ASSET_FILES = {"assets/brand/mark-dark.svg", "assets/brand/mark-light.svg", "assets/brand/logo-dark.svg",
               "assets/brand/logo-light.svg", "assets/brand/logo-dark.png", "assets/brand/logo-light.png",
               "assets/brand/icon-dark.png", "assets/brand/veyq-dark.ico", "assets/brand/veynuq-dark.ico", "assets/brand/README.md",
               "docs/screenshots/workspace.png", "docs/screenshots/explorer.png", "docs/screenshots/permissions.png",
               "assets/vendor/marked.js", "assets/vendor/purify.js", "assets/vendor/highlight.js",
               "assets/vendor/atom-one-dark.css", "assets/vendor/marked-LICENSE.md",
               "assets/vendor/DOMPurify-LICENSE", "assets/vendor/highlight-LICENSE", "docs/REQUIREMENTS.md"}

# Keep legacy names in the accepted update paths so older installations can
# be verified and migrated. They are no longer shipped in new packages.
DISTRIBUTION_ROOT_FILES = ROOT_FILES - {"ui-translations.json", "Veyq.bat", "Installer_only_shortcut.bat"}
DISTRIBUTION_ASSET_FILES = ASSET_FILES - {"docs/REQUIREMENTS.md", "assets/brand/veyq-dark.ico", "assets/brand/icon-dark.png", "assets/brand/mark-light.svg"}


def program_path(name):
    path = PurePosixPath(name)
    if "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError("Percorso non valido nell'aggiornamento.")
    if str(path) != name:
        raise ValueError("Percorso non canonico.")
    if name not in ROOT_FILES | ASSET_FILES and not (len(path.parts) == 2 and path.parts[0] == "veyq" and path.suffix == ".py" and re.fullmatch(r"[A-Za-z0-9_]+\.py", path.name)):
        raise ValueError("File non ammesso nel pacchetto: " + name)
    return name


def validate_bundle(blob, manifest, destination):
    verify_manifest(manifest)
    if hashlib.sha256(blob).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Checksum archivio non corrispondente.")
    files = manifest["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= 100:
        raise ValueError("Manifest non valido.")
    if not {"app.py", "index.html", "build.json", "requirements.txt", "veyq/desktop.py", "veyq/update_worker.py"}.issubset(files):
        raise ValueError("Pacchetto incompleto.")
    destination = Path(destination).resolve()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [i.filename for i in archive.infolist()]
        if len(names) != len(set(names)) or set(names) != set(files):
            raise ValueError("Contenuto dell'archivio diverso dal manifest.")
        total = 0
        for entry in archive.infolist():
            program_path(entry.filename)
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.is_dir():
                raise ValueError("Link/cartella non ammesso nel pacchetto.")
            total += entry.file_size
            if entry.file_size > 2_000_000 or total > 20_000_000:
                raise ValueError("Pacchetto troppo grande.")
            data = archive.read(entry)
            if hashlib.sha256(data).hexdigest() != files[entry.filename]:
                raise ValueError("Checksum file non corrispondente.")
            target = destination / entry.filename
            if not target.resolve().is_relative_to(destination):
                raise ValueError("Percorso fuori dal pacchetto.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def enable_updates(root, store):
    root = Path(root).resolve()
    if (root / ".git").exists():
        # A developer checkout must never be overwritten by the updater.
        return {"managed": False, "message": "Checkout di sviluppo: aggiorna con Git."}
    build = json.loads((root / "build.json").read_text(encoding="utf-8-sig")) if (root / "build.json").exists() else {"commit": "initial"}
    files = {}
    for path in [*(root / "veyq").glob("*.py"), *(root / name for name in ROOT_FILES | ASSET_FILES if (root / name).exists())]:
        files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_json(store.root / "installation.json", {"root": str(root), "commit": build["commit"], "sequence": build.get("sequence", 0), "files": files})
    return {"managed": True}


class Updater:
    def __init__(self, root, store):
        self.root, self.store = Path(root).resolve(), store
        self.pending = store.root / "pending-update.json"

    def _installation(self):
        p = self.store.root / "installation.json"
        if (self.root / ".git").exists() or not p.exists():
            return None
        installation = json.loads(p.read_text(encoding="utf-8"))
        if Path(installation["root"]).resolve() != self.root:
            return None
        for name, digest in installation["files"].items():
            program_path(name)
            path = self.root / name
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("File dell'app modificati localmente: aggiornamento sospeso per preservare le modifiche.")
        return installation

    def check_and_stage(self):
        installation = self._installation()
        if not installation:
            return {"ready": False, "message": "Checkout di sviluppo. Gli aggiornamenti automatici sono attivi sulle installazioni da pacchetto, non sul sorgente Git."}
        if self.pending.exists():
            return {"ready": True, "message": "Aggiornamento gia' preparato."}
        prefix = f"https://github.com/{UPDATE_REPO}/releases/download/"
        try:
            r = public_request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
            release = json.loads(r["text"])
            assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
            if not {"veyq-update.zip", "veyq-update.json"}.issubset(assets):
                return {"ready": False, "message": "La release non contiene un pacchetto di aggiornamento."}
            if any(not url.startswith(prefix) for url in assets.values()):
                raise ValueError("Asset pubblicato fuori dal repository autorizzato.")
            manifest = json.loads(public_request(assets["veyq-update.json"])["text"])
        except RuntimeError as error:
            if not re.match(r'HTTP (403|429):', str(error)):
                raise
            # Public API quotas require no account: use GitHub's official asset
            # route, then pin the ZIP to the signed revision. All normal
            # signature, monotonic-version and bundle checks still apply.
            manifest = json.loads(public_request(f"https://github.com/{UPDATE_REPO}/releases/latest/download/veyq-update.json")["text"])
            assets = {'veyq-update.zip':prefix+'build-'+str(manifest.get('commit',''))+'/veyq-update.zip'}
        verify_manifest(manifest)
        if not re.fullmatch(r"[a-f0-9]{40}", manifest.get("commit", "")):
            raise ValueError("Identificatore versione non valido.")
        if manifest["commit"] == installation["commit"]:
            return {"ready": False, "message": "Veynuq e' aggiornato."}
        if manifest["sequence"] <= installation.get("sequence", 0):
            raise ValueError("Versione precedente o ripetuta: aggiornamento rifiutato.")
        failed = self.store.root / "failed-update.json"
        if failed.exists():
            previous_failure = json.loads(failed.read_text(encoding="utf-8"))
            if previous_failure.get("manifest", {}).get("commit") == manifest["commit"]:
                return {"ready": False, "message": "Questa versione non e' stata installata per un errore. La versione precedente resta attiva; attendi una nuova release o reinstalla il pacchetto."}
        blob = public_request(assets["veyq-update.zip"], limit=20_000_000)["data"]
        stage = self.store.root / "updates" / manifest["commit"]
        stage.mkdir(parents=True, exist_ok=True)
        validate_bundle(blob, manifest, stage)
        atomic_json(self.pending, {"root": str(self.root), "stage": str(stage), "manifest": manifest,
                                  "previous": installation, "data_root": str(self.store.root)})
        return {"ready": True, "commit": manifest["commit"], "message": "Pacchetto verificato e pronto per il riavvio."}

    def launch_apply(self):
        if not self.pending.exists() or not self._installation():
            return {"ok": False, "message": "Nessun aggiornamento disponibile."}
        helper = self.store.root / "updates" / "update_worker.py"
        shutil.copy2(self.root / "veyq" / "update_worker.py", helper)
        shutil.copy2(self.root / "veyq" / "signing.py", helper.with_name("signing.py"))
        shutil.copy2(self.root / 'veyq' / 'runtime.py',helper.with_name('runtime.py'))
        # Use the active runtime: signature verification must be available before
        # creating a separate environment for a dependency update.
        python = sys.executable
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([python, str(helper), str(self.pending), str(os.getpid())], cwd=self.root, **flags)
        return {"ok": True}
