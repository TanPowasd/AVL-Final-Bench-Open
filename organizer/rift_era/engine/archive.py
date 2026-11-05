"""Bounded extraction of untrusted submissions into a fresh private directory."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import stat
import tempfile
import time
import zipfile
from contextlib import contextmanager

from .errors import RuleViolation

MAX_COMPRESSED = 512 * 1024**2
MAX_EXTRACTED = 2 * 1024**3
MAX_ENTRIES = 4096


def _path(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename
    if '\x00' in info.orig_filename:
        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'NUL in archive path')
    if not (info.flag_bits & 0x800) and not name.isascii():
        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'non-UTF8 path')
    parts = name.rstrip('/').split('/')
    if (not name or '\\' in name or '\x00' in name or ':' in name or name.startswith('/') or
            any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts)):
        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'unsafe archive path')
    mode = (info.external_attr >> 16) & 0xffff
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'non-regular archive entry')
    if info.flag_bits & 1:
        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'encrypted entry')
    if info.file_size > 100 * max(1, info.compress_size):
        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'compression ratio exceeded')
    return PurePosixPath(*parts)


@contextmanager
def extracted_submission(archive: Path, *, staging_parent: Path | None = None,
                         timeout_s: float = 10.0):
    """Never extracts into an existing user directory; cleans only its own temp."""
    if archive.stat().st_size > MAX_COMPRESSED:
        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'compressed archive exceeds 512MiB')
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='rift-submission-', dir=staging_parent) as temp:
        root = Path(temp)
        try:
            with zipfile.ZipFile(archive) as zf:
                infos = zf.infolist()
                if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXTRACTED:
                    raise RuleViolation('ERR_ARCHIVE_SECURITY', 'archive quota exceeded')
                paths, names, regular = [], set(), set()
                for info in infos:
                    path = _path(info)
                    key = str(path).casefold()
                    if key in names:
                        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'case-insensitive path collision')
                    names.add(key)
                    if not info.is_dir():
                        regular.add(key)
                    paths.append((info, path))
                for _, path in paths:
                    if any(str(parent).casefold() in regular for parent in path.parents if str(parent) != '.'):
                        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'file shadows directory')
                total = 0
                for info, path in paths:
                    if time.monotonic() - start > timeout_s:
                        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'extraction deadline exceeded')
                    destination = root.joinpath(*path.parts)
                    if info.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with zf.open(info) as source, destination.open('xb') as target:
                        while chunk := source.read(65536):
                            total += len(chunk)
                            written += len(chunk)
                            if total > MAX_EXTRACTED or written > info.file_size or time.monotonic() - start > timeout_s:
                                raise RuleViolation('ERR_ARCHIVE_SECURITY', 'streaming extraction quota exceeded')
                            target.write(chunk)
                    if written != info.file_size:
                        raise RuleViolation('ERR_ARCHIVE_SECURITY', 'entry size mismatch')
                    destination.chmod(0o400)
            if not (root / 'main.py').is_file():
                raise RuleViolation('ERR_ARCHIVE_SECURITY', 'missing root main.py')
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, UnicodeError) as exc:
            if isinstance(exc, RuleViolation):
                raise
            raise RuleViolation('ERR_ARCHIVE_SECURITY', 'malformed archive') from exc
        # Exceptions from the caller are not malformed-archive errors.
        yield root
