"""Hash prompt trees and private resource files without following redirected entries."""

import hashlib
from pathlib import Path

from .resources import digest
from .storage import IntegrityError


def resource_sha256(path):
    path = Path(path)
    if path != path.resolve():
        raise IntegrityError("Adoption resource was redirected")
    if path.is_dir():
        entries = []
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                raise IntegrityError("Adoption resource contains a symbolic link")
            if item.is_file():
                entries.append((str(item.relative_to(path)), resource_sha256(item)))
            elif item.is_dir():
                entries.append((str(item.relative_to(path)) + "/", None))
            else:
                raise IntegrityError("Adoption resource is not a regular file")
        return digest(entries)
    if not path.is_file():
        raise IntegrityError("Adoption resource is missing or not a regular file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
