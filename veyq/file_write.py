"""Stage UTF-8 writes beside their destination and publish only complete files."""
import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def validate_write(args):
    path, content = args["path"], args["content"]
    if not path.strip() or "\x00" in path:
        raise ValueError("path: provide a non-empty file path.")
    if "\x00" in content:
        raise ValueError("content: NUL characters are not supported in text files.")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("content: invalid Unicode for UTF-8.") from None
    if args.get("part", 1) < 1:
        raise ValueError("Numbered parts start at 1.")


def file_version(target):
    """Refuse special files; detect changed bytes and replaced file identities."""
    try:
        info = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Destination must be a regular file.")
    with target.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").digest()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, digest


def new_temp(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".veynuq-", suffix=".tmp", dir=target.parent, delete=False) as stream:
        return Path(stream.name)


@dataclass
class Draft:
    temp: Path
    expected: tuple | None
    overwrite: bool
    parts: list = field(default_factory=list)

    def append(self, data):
        with self.temp.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            return stream.tell()


def publish(temp, target, expected, backup, cancel):
    """Prepare backup and check again just before the atomic commit."""
    if file_version(target) != expected:
        raise FileExistsError("Destination changed during writing. Inspect it before retrying.")
    if expected is not None and not os.access(target, os.W_OK):
        raise PermissionError("Destination is read-only or you do not have permission to write it.")
    backup_id = backup(target) if expected is not None else None
    if expected is not None:
        shutil.copymode(target, temp)
    if file_version(target) != expected:
        raise FileExistsError("Destination changed during writing. Inspect it before retrying.")
    if cancel.is_set():
        raise InterruptedError("Write cancelled before the final file was modified.")
    if expected is not None:
        os.replace(temp, target)
    elif os.name == "nt":
        # Windows rename fails if another process has already created target.
        os.rename(temp, target)
    else:
        # POSIX rename replaces an existing file. Hard-linking publishes the
        # fully written inode atomically with the required no-clobber behavior.
        os.link(temp, target)
    return backup_id
