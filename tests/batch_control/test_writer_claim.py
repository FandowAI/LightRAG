"""An idle managed workspace retains ownership and rejects accidental legacy writers."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lightrag.batch_control.storage import Conflict, IntegrityError
from lightrag.batch_control.writer_claim import (
    WorkspaceClaim,
    guard_construction,
    managed_owner,
)

pytestmark = pytest.mark.offline


def test_exclusive_writer_and_failed_constructor_release(tmp_path):
    with WorkspaceClaim(tmp_path, "legacy"):
        with pytest.raises(Conflict, match="existing process"):
            with WorkspaceClaim(tmp_path, "legacy"):
                pytest.fail("A second writer was admitted")
    with WorkspaceClaim(tmp_path, "legacy"):
        pass

    @guard_construction
    def construct(args):
        raise ValueError("initialization failed")

    with pytest.raises(ValueError, match="initialization failed"):
        construct(SimpleNamespace(workers=1, working_dir=tmp_path, workspace="legacy"))
    with WorkspaceClaim(tmp_path, "legacy"):
        pass


def test_owner_transfer_survives_exit_and_foreign_registry_is_rejected(tmp_path):
    owner = managed_owner("batch-one", str(tmp_path / "registry.sqlite3"))
    with WorkspaceClaim(tmp_path, "legacy", owner=owner, publish=False) as claim:
        assert not claim.owner_file.exists()
        claim.transfer(owner)
    for other in (
        None,
        managed_owner("batch-two", owner["registry"]),
        managed_owner("batch-one", str(tmp_path / "other.sqlite3")),
    ):
        with pytest.raises(Conflict, match="different batch registry"):
            with WorkspaceClaim(tmp_path, "legacy", owner=other):
                pytest.fail("A foreign writer was admitted")
    with WorkspaceClaim(tmp_path, "legacy", owner=owner):
        assert json.loads((tmp_path / "legacy/.batch-owner.json").read_text()) == owner


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_redirected_lock_does_not_authorize_a_writer(tmp_path, kind):
    directory = tmp_path / "legacy"
    directory.mkdir()
    other = tmp_path / "other"
    other.write_text("do not replace")
    lock = directory / ".batch-writer.lock"
    if kind == "symlink":
        lock.symlink_to(other)
    else:
        os.link(other, lock)
    with pytest.raises((OSError, IntegrityError)):
        with WorkspaceClaim(tmp_path, "legacy"):
            pytest.fail("A redirected lock was accepted")
    assert other.read_text() == "do not replace"


def test_partial_environment_never_downgrades_to_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("LIGHTRAG_MANAGED_BATCH_ID", "batch-one")
    monkeypatch.delenv("LIGHTRAG_BATCH_REGISTRY", raising=False)
    with pytest.raises(IntegrityError, match="Both managed"):
        WorkspaceClaim.from_environment(tmp_path, "legacy")
    # The existing upstream empty workspace remains valid.
    with WorkspaceClaim(Path(tmp_path), ""):
        pass


def test_multiple_workers_cannot_bypass_a_managed_owner(tmp_path):
    owner = managed_owner("batch-one", str(tmp_path / "registry.sqlite3"))
    with WorkspaceClaim(tmp_path, "legacy", owner=owner):
        pass

    @guard_construction
    def factory(args):
        pytest.fail("A multiple-worker service bypassed managed ownership")

    with pytest.raises(Conflict, match="one verified writer"):
        factory(SimpleNamespace(workers=2, working_dir=tmp_path, workspace="legacy"))
