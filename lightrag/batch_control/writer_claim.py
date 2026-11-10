"""Hold the physical workspace writer lock through initialization and finalization."""

import fcntl
from functools import wraps
import json
import os
from pathlib import Path

from pydantic import TypeAdapter

from .contracts import Name
from .runtime_process import atomic_json, process_identity
from .storage import Conflict, IntegrityError


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        return json.load(source)


def managed_owner(batch, registry):
    TypeAdapter(Name).validate_python(batch, strict=True)
    path = Path(registry)
    if not path.is_absolute() or path != path.resolve():
        raise IntegrityError("Workspace registry must be a canonical absolute path")
    return {"schema_version": 1, "data_batch_id": batch, "registry": str(path)}


class WorkspaceClaim:
    def __init__(self, working_directory, workspace, *, owner=None, publish=True):
        if workspace:
            TypeAdapter(Name).validate_python(workspace, strict=True)
        self.directory = (Path(working_directory) / workspace).resolve()
        self.owner = owner
        self.publish = publish
        self.descriptor = None
        self.owner_file = self.directory / ".batch-owner.json"

    @classmethod
    def from_environment(cls, working_directory, workspace):
        batch = os.getenv("LIGHTRAG_MANAGED_BATCH_ID")
        registry = os.getenv("LIGHTRAG_BATCH_REGISTRY")
        if bool(batch) != bool(registry):
            raise IntegrityError("Both managed workspace identity fields are required")
        return cls(
            working_directory,
            workspace,
            owner=managed_owner(batch, registry) if batch else None,
        )

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.directory / ".batch-writer.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if os.fstat(descriptor).st_nlink != 1:
                raise IntegrityError("Workspace writer lock must not be a hard link")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict(
                    "An existing process still owns this workspace writer"
                ) from exc
            self.descriptor = descriptor
            try:
                recorded = read_json(self.owner_file)
            except FileNotFoundError:
                recorded = None
            if recorded is not None and recorded != self.owner:
                raise Conflict("This workspace is owned by a different batch registry")
            if self.owner is not None and recorded is None and self.publish:
                self.transfer(self.owner)
            metadata = {
                "schema_version": 1,
                "pid": os.getpid(),
                "process_identity": process_identity(os.getpid()),
                "owner": self.owner,
            }
            os.ftruncate(descriptor, 0)
            os.write(descriptor, json.dumps(metadata).encode())
            os.fsync(descriptor)
            return self
        except BaseException:
            self.descriptor = None
            os.close(descriptor)
            raise

    def transfer(self, owner):
        if self.descriptor is None:
            raise Conflict("Transfer requires the exclusive workspace writer lock")
        checked = managed_owner(owner["data_batch_id"], owner["registry"])
        if owner != checked:
            raise IntegrityError("Unsupported workspace owner record")
        try:
            previous = read_json(self.owner_file)
        except FileNotFoundError:
            previous = None
        if previous is not None and previous != owner:
            raise Conflict("A different registry already owns this workspace")
        atomic_json(self.owner_file, owner)
        sync_directory(self.directory)
        self.owner = owner

    def __exit__(self, *_):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def guard_construction(factory):
    @wraps(factory)
    def create(args):
        if args.workers != 1:
            owner_file = Path(args.working_dir) / args.workspace / ".batch-owner.json"
            if (
                owner_file.exists()
                or os.getenv("LIGHTRAG_MANAGED_BATCH_ID")
                or os.getenv("LIGHTRAG_BATCH_REGISTRY")
            ):
                raise Conflict("Managed workspaces require one verified writer process")
            return factory(args)
        with WorkspaceClaim.from_environment(args.working_dir, args.workspace):
            return factory(args)

    return create
