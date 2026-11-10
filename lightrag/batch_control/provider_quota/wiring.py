"""Compile server-owned provider routes and the portable workflow launcher."""

from pathlib import Path
import fcntl
import os

from .core import QuotaStore, endpoint


def registry_path(paths):
    return paths.under(paths.runtime_root, "batch-control", "provider-quota.sqlite3")


def ensure(control):
    store = QuotaStore(registry_path(control.paths))
    store.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        store.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if not store.path.exists():
            store.configure(control.provider_limits)
        store.ready()
    finally:
        os.close(descriptor)
    return store


def runtime_spec(control, batch, urls):
    ensure(control)
    return {
        "registry": str(registry_path(control.paths)),
        "batch_id": batch,
        "endpoints": sorted({endpoint(url) for url in urls}),
    }


def workflow_launcher(paths, batch, task, recipe, directory):
    from ..runtime_process import atomic_json

    if not recipe.get("models"):
        return None
    spec = {
        "registry": str(registry_path(paths)),
        "batch_id": batch,
        "task_id": task,
        "endpoints": sorted(
            {endpoint(value["api_url"]) for value in recipe["models"].values()}
        ),
    }
    target = directory / "provider-admission.json"
    atomic_json(target, spec)
    return [str(Path(__file__).with_name("bootstrap.py")), "--spec", str(target), "--"]


def business_workflow_launcher(recipe, directory, job_id):
    """Freeze quota routing for the original batch-owned distillation endpoint."""
    from ..runtime_process import atomic_json

    registry = Path(os.environ["LIGHTRAG_BATCH_REGISTRY"])
    atomic_json(
        directory / "provider-admission.json",
        {
            "registry": str(registry.with_name("provider-quota.sqlite3")),
            "batch_id": os.environ["LIGHTRAG_MANAGED_BATCH_ID"],
            "task_id": job_id,
            "endpoints": sorted(
                {endpoint(value["api_url"]) for value in recipe["models"].values()}
            ),
        },
    )
