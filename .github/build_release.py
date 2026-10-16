"""Build the public, data-free update ZIP and hash manifest."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from veyq.updater import DISTRIBUTION_ROOT_FILES, DISTRIBUTION_ASSET_FILES, validate_bundle
from veyq.signing import sign_manifest


def validate_distribution_files(names):
    forbidden = {"state.json", "state.previous.json", "profile.json", "runtime.json", "credentials.dpapi", "installation.json", "ui-translations.json", "package.json", "package-lock.json", "REQUIREMENTS.md", "Veyq.bat", "veyq-dark.ico", "Installer_only_shortcut.bat", "icon-dark.png", "mark-light.svg"}
    for name in names:
        path = Path(name)
        if path.parts[0] in {"tests", "scripts", ".github", "node_modules", ".venv"} or path.name in forbidden or path.name.startswith((".env", "test_")):
            raise ValueError("Development or private file cannot be distributed: " + name)


def build(destination, commit=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    commit = commit or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    sequence = int(os.environ.get("GITHUB_RUN_NUMBER") or subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=ROOT, text=True).strip())
    signing_key = os.environ.get("VEYQ_RELEASE_SIGNING_KEY", "")
    if not signing_key:
        raise ValueError("VEYQ_RELEASE_SIGNING_KEY richiesta: i pacchetti non firmati non vengono prodotti.")
    files = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in (ROOT / "veyq").glob("*.py")}
    for name in (DISTRIBUTION_ROOT_FILES | DISTRIBUTION_ASSET_FILES) - {"build.json"}:
        files[name] = (ROOT / name).read_bytes()
    files["build.json"] = json.dumps({"version": "4.5.5", "commit": commit, "sequence": sequence}, indent=2).encode()
    validate_distribution_files(files)
    archive = destination / "veyq-update.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in sorted(files.items()):
            z.writestr(name, content)
    manifest = {"version": "4.5.5", "commit": commit, "sequence": sequence, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    manifest = sign_manifest(manifest, signing_key)
    (destination / "veyq-update.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        validate_bundle(archive.read_bytes(), manifest, temp)
    print(json.dumps({"archive": str(archive), "files": len(files), "bytes": archive.stat().st_size, "commit": commit}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    parser.add_argument("--commit")
    args = parser.parse_args()
    build(args.destination, args.commit)
