"""Private local JSON I/O; compatible with maxc.semantic/v1 documents."""
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 16 * 1024 * 1024

class ValidationError(ValueError):
    pass

def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_path(value: str) -> Path:
    path = Path(os.path.abspath(os.path.expanduser(value)))
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValidationError("Semantic files must not traverse symlinks or reparse points.")
    return path


def read_json(value: str) -> dict:
    path = safe_path(value)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValidationError("Semantic input must be a regular JSON file.")
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValidationError("Semantic input exceeds the 16 MiB file limit.")
        def unique(pairs):
            obj = {}
            for key, item in pairs:
                if key in obj:
                    raise ValueError("duplicate key")
                obj[key] = item
            return obj
        def invalid_constant(_):
            raise ValueError("non-finite number")
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValidationError("Cannot read a valid semantic JSON object (duplicate keys and NaN are rejected).") from exc


def write_json(value: str, document: dict, *, overwrite: bool = False) -> dict:
    path = safe_path(value)
    if not path.parent.is_dir() or (path.exists() and not path.is_file()):
        raise ValidationError("Choose a regular output file in an existing directory.")
    if path.exists() and not overwrite:
        raise ValidationError("Output already exists; choose another path or explicitly use --overwrite.")
    raw = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise ValidationError("Export exceeds the 16 MiB portable file limit.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".semantic-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(str(path))
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    except OSError as exc:
        raise ValidationError("Could not atomically publish semantic export; the existing file was preserved.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
