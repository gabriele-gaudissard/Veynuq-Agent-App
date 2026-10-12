"""Opt-in, pinned local image engine; no server, accounts or Python packages."""
import hashlib
import os
import platform
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

ENGINE_URL = 'https://github.com/leejet/stable-diffusion.cpp/releases/download/master-956-1b0ba10/sd-master-1b0ba10-bin-win-vulkan-x64.zip'
ENGINE_SHA = '9e8b9baff514fe5dfb77e51bf08d85f11e6f10128eac14d4474c87f563309725'
ENGINE_SIZE = 30123908
MODEL_REVISION = '451f4fe16113bff5a5d2269ed5ad43b0592e9a14'
MODEL_URL = f'https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/{MODEL_REVISION}/v1-5-pruned-emaonly.safetensors'
MODEL_SHA = '6ce0161689b3853acaa03779ec93eafe75a02f4ced659bee03f50797806fa2fa'
MODEL_SIZE = 4265146304


def root_for(data_root):
    root = Path(data_root).resolve() / 'image-engine'
    if root.exists() and (root.is_symlink() or getattr(root.lstat(), 'st_file_attributes', 0) & 0x400):
        raise PermissionError('Image engine links are not supported.')
    return root


def digest(path):
    if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
        raise PermissionError('Image engine links are not supported.')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ready(data_root):
    try:
        root = root_for(data_root)
        return (root/'sd15.safetensors').stat().st_size == MODEL_SIZE and (root/'bin/sd-cli.exe').is_file() and (root/'engine.zip').stat().st_size == ENGINE_SIZE
    except OSError:
        return False


def entries(archive):
    rows = []
    total = 0
    for item in archive.infolist():
        path = Path(item.filename)
        if len(path.parts) != 1 or '\\' in item.filename or ':' in item.filename or item.is_dir() or stat.S_ISLNK(item.external_attr >> 16):
            raise ValueError('Invalid image engine archive path.')
        if path.name == 'sd-server.exe':
            continue  # This integration launches no HTTP service.
        if path.name != 'sd-cli.exe' and path.suffix not in {'.dll', '.txt'}:
            raise ValueError('Unexpected image engine archive file.')
        total += item.file_size
        if total > 100_000_000 or item.file_size > 60_000_000:
            raise ValueError('Image engine archive exceeds its limits.')
        rows.append(item)
    if not any(item.filename == 'sd-cli.exe' for item in rows):
        raise ValueError('Image engine archive is incomplete.')
    if len(rows) != len({item.filename for item in rows}):
        raise ValueError('Duplicate image engine archive path.')
    return rows


def verify(data_root):
    root = root_for(data_root)
    archive, model = root/'engine.zip', root/'sd15.safetensors'
    if archive.stat().st_size != ENGINE_SIZE or digest(archive) != ENGINE_SHA:
        raise PermissionError('Image engine archive changed. Prepare it again.')
    if model.stat().st_size != MODEL_SIZE or digest(model) != MODEL_SHA:
        raise PermissionError('Image model changed. Prepare it again.')
    binary = root/'bin'
    if binary.is_symlink() or getattr(binary.lstat(), 'st_file_attributes', 0) & 0x400:
        raise PermissionError('Image engine links are not supported.')
    with zipfile.ZipFile(archive) as bundle:
        rows = entries(bundle)
        for item in rows:
            if digest(binary/item.filename) != hashlib.sha256(bundle.read(item)).hexdigest():
                raise PermissionError('Image engine executable changed. Prepare it again.')
        expected = {item.filename for item in rows}
        # A replacement DLL next to the executable must never be loaded.
        actual = {p.name for p in binary.iterdir()}
        if actual - expected:
            raise PermissionError('Unexpected file in image engine folder. Prepare a clean installation.')
    return binary/'sd-cli.exe', model


def prepare(data_root, cancel, progress=lambda state: None):
    if os.name != 'nt' or platform.machine().upper() not in {'AMD64', 'X86_64'}:
        raise RuntimeError('The built-in image engine currently requires Windows x64. Use an external image API on other systems.')
    import requests
    root = root_for(data_root)
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < MODEL_SIZE + 1_000_000_000 and not ready(data_root):
        raise RuntimeError('The local image engine needs approximately 5 GB of free disk space.')
    with requests.Session() as client:
        client.trust_env = False
        for name, url, size, checksum in [('engine.zip', ENGINE_URL, ENGINE_SIZE, ENGINE_SHA), ('sd15.safetensors', MODEL_URL, MODEL_SIZE, MODEL_SHA)]:
            dest = root/name
            if dest.is_file() and dest.stat().st_size == size and digest(dest) == checksum:
                progress({'file':name, 'downloaded':size, 'total':size})
                continue
            part = root/(name+'.download')
            if part.exists() and part.is_symlink():
                raise PermissionError('Invalid image download destination.')
            try:
                hashed, amount = hashlib.sha256(), 0
                with client.get(url, stream=True, timeout=(20,30)) as response:
                    response.raise_for_status()
                    if response.url.split(':',1)[0] != 'https':
                        raise ValueError('Image download was redirected outside HTTPS.')
                    with part.open('wb') as output:
                        for chunk in response.iter_content(1024*1024):
                            if cancel.is_set():
                                raise RuntimeError('Image setup stopped.')
                            amount += len(chunk)
                            if amount > size:
                                raise ValueError('Image download exceeded its expected size.')
                            hashed.update(chunk)
                            output.write(chunk)
                            progress({'file':name, 'downloaded':amount, 'total':size})
                if amount != size or hashed.hexdigest() != checksum:
                    raise ValueError('Image download integrity check failed.')
                if cancel.is_set():
                    raise RuntimeError('Image setup stopped.')
                part.replace(dest)
            finally:
                part.unlink(missing_ok=True)
    binary = root/'bin'
    if binary.exists() and (binary.is_symlink() or getattr(binary.lstat(), 'st_file_attributes', 0) & 0x400):
        raise PermissionError('Invalid image engine folder.')
    previous=None
    with tempfile.TemporaryDirectory(dir=root, prefix='prepared-') as staging:
        prepared = Path(staging)/'bin'
        prepared.mkdir()
        with zipfile.ZipFile(root/'engine.zip') as archive:
            for item in entries(archive):
                (prepared/item.filename).write_bytes(archive.read(item))
        if binary.exists():
            previous=root/('previous-bin-'+uuid.uuid4().hex)
            binary.rename(previous)
        try:prepared.rename(binary)
        except Exception:
            if previous:previous.rename(binary)
            raise
    # Keep upstream license notices with the actual downloaded model.
    for name, url in [('MODEL-CARD.md', f'https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/raw/{MODEL_REVISION}/README.md'), ('MODEL-LICENSE.txt','https://raw.githubusercontent.com/CompVis/stable-diffusion/21f890f9da3cfbeaba8e2ac3c425ee9e998d5229/LICENSE'), ('ENGINE-LICENSE.txt', 'https://raw.githubusercontent.com/leejet/stable-diffusion.cpp/1b0ba10/LICENSE')]:
        if cancel.is_set():raise RuntimeError('Image setup stopped.')
        with requests.Session() as client:
            client.trust_env = False
            response = client.get(url, timeout=30)
            response.raise_for_status()
            if len(response.content)>100_000:
                raise ValueError('Unexpected image license response.')
            (root/name).write_bytes(response.content)
    verify(data_root)
    return {'ready':True, 'model':'Stable Diffusion 1.5', 'download_bytes':MODEL_SIZE+ENGINE_SIZE, 'account_required':False}


def prepare_for_install(store, progress=lambda state: None):
    """Prepare the default engine; preserve an existing explicit provider choice."""
    if store.snapshot()['settings']['image_provider'] != 'managed':
        return {'skipped':True, 'reason':'Existing image provider selection preserved.'}
    # A complete verified installation is reusable even without network access.
    if ready(store.root):
        try:
            verify(store.root)
            root = root_for(store.root)
            notices = [root/name for name in ('MODEL-CARD.md','MODEL-LICENSE.txt','ENGINE-LICENSE.txt')]
            if all(p.is_file() and not p.is_symlink() and 0 < p.stat().st_size <= 100_000 for p in notices):
                return {'ready':True, 'reused':True, 'model':'Stable Diffusion 1.5', 'account_required':False}
        except (OSError, ValueError):
            pass  # Repair a missing or changed download through pinned preparation.
    import threading
    result = prepare(store.root, threading.Event(), progress)
    return {**result, 'reused':False}
