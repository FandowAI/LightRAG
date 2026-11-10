"""Authorize independent non-model exports; retain uncertain and partial publications."""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

from pydantic import Field

from ..contracts import Contract, identifier
from ..resources import digest
from ..storage import Conflict, IntegrityError
from ..task_workflow import workflow_fingerprint
from ..runtime_config import clean_environment
from ..provider_quota.core import process_owner, owner_gone
from ...integrations.data_workflow.config import load_settings, read_yaml
from .core import FILES, atomic_json, snapshot, verify_publication


class ExportLimits(Contract):
    max_evidence_bytes: int = Field(default=1073741824, gt=0)
    max_row_bytes: int = Field(default=134217728, gt=0)
    max_output_bytes: int = Field(default=536870912, gt=0)
    max_samples: int = Field(default=1000000, gt=0)
    timeout_seconds: int = Field(default=600, gt=0)


class ExportStore:
    def __init__(self, registry):
        self.registry = registry

    @contextmanager
    def transaction(self):
        with self.registry.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS training_exports (id TEXT PRIMARY KEY, batch TEXT NOT NULL, task TEXT NOT NULL, record TEXT NOT NULL)"
            )
            yield db

    @staticmethod
    def read(db, batch, task, export):
        row = db.execute(
            "SELECT record FROM training_exports WHERE batch=? AND task=? AND id=?",
            (batch, task, export),
        ).fetchone()
        if row is None:
            raise KeyError(export)
        return json.loads(row[0])

    def get(self, batch, task, export):
        with self.transaction() as db:
            return self.read(db, batch, task, export)

    def update(self, batch, task, export, **values):
        with self.transaction() as db:
            record = self.read(db, batch, task, export)
            record.update(values, updated_at=time.time())
            db.execute(
                "UPDATE training_exports SET record=? WHERE id=?",
                (json.dumps(record), export),
            )
            return record

    def list(self, batch=None, task=None, *, queued=False):
        with self.transaction() as db:
            return [
                record
                for row in db.execute(
                    "SELECT record FROM training_exports ORDER BY rowid"
                )
                if (record := json.loads(row[0]))
                and (batch is None or record["data_batch_id"] == batch)
                and (task is None or record["task_id"] == task)
                and (not queued or record["state"] == "queued")
            ]


class TrainingExports:
    def __init__(self, control, *, actor):
        self.control, self.actor = control, actor
        self.store = ExportStore(control.registry)
        self.limits = getattr(control, "training_export_limits", ExportLimits())

    def task(self, batch, task, *, write=False):
        self.control.allow(self.actor, "read", batch)
        if write:
            self.control.allow(self.actor, "export_training", batch)
        record = self.control.registry.task(batch, task)
        if record["kind"] not in {"chain", "distillation"} or record["state"] not in {
            "completed",
            "completed_with_errors",
        }:
            raise Conflict("Training exports require a completed distillation task")
        owner, revision, document, _, _ = self.control.read(
            self.actor, batch, record["config_revision_id"]
        )
        if revision["digest"] != record["manifest_digest"]:
            raise IntegrityError("Export task configuration identity changed")
        receipt = ((record.get("result") or {}).get("distillation") or {}).get("result")
        if not receipt or receipt.get("status") not in {
            "completed",
            "completed_with_errors",
        }:
            raise Conflict("Task has no confirmed completed workflow receipt")
        root = self.control.paths.under(
            self.control.paths.runtime_root,
            "tasks",
            owner["namespace"],
            task,
            "workflow",
        )
        output = self.control.paths.under(root, "outputs", receipt["run_id"])
        if str(output) != receipt["directory"]:
            raise IntegrityError(
                "Workflow receipt belongs to a different task directory"
            )
        settings = load_settings(
            self.control.paths.revision(batch, revision["id"]) / "data_workflow.yaml"
        )
        return record, root, output, settings

    def directory(self, batch, task, export):
        owner = self.control.registry.get(batch)
        directory = self.control.paths.under(
            self.control.paths.runtime_root,
            "tasks",
            owner["namespace"],
            task,
            "training-exports",
            export,
        )
        if directory != directory.resolve():
            raise IntegrityError("Training export directory was redirected")
        return directory

    def request(self, batch, task, shape, selection, request_id):
        from pydantic import TypeAdapter
        from ..contracts import Name

        TypeAdapter(Name).validate_python(request_id, strict=True)
        if shape not in {"messages", "alpaca", "sharegpt"} or selection not in {
            "processed",
            "machine_passed",
        }:
            raise ValueError("Unsupported training export format or selection")
        record, root, output, settings = self.task(batch, task, write=True)
        fingerprint = digest({"task": task, "format": shape, "selection": selection})
        with self.store.transaction() as db:
            previous = self.control.registry.prior(
                db, self.actor, "training_export", batch, request_id, fingerprint
            )
            if previous:
                return self.store.read(db, batch, task, previous["export_id"])
        stamp = workflow_fingerprint(settings)
        if stamp != record.get("payload", {}).get("workflow_fingerprint"):
            raise Conflict("Selected workflow environment changed after distillation")
        recipe_path = self.control.paths.under(
            self.control.paths.config_root, "jobs", task, "workflow", "job.yaml"
        )
        if (root / "job.yaml").resolve() != recipe_path:
            raise IntegrityError(
                "Workflow recipe locator left its authoritative task configuration"
            )
        paths = {
            "input.jsonl": root / "input.jsonl",
            "job.yaml": recipe_path,
            **{
                name: output / name
                for name in ("run.json", "review.jsonl", "events.jsonl")
            },
        }
        document = self.control.read(self.actor, batch, record["config_revision_id"])[2]
        expected = deepcopy(document.recipe)
        expected["input"] = {"paths": [str(root / "input.jsonl")]}
        expected["output"] = {"directory": str(root / "outputs")}
        actual = read_yaml(root / "job.yaml")

        def normalized(recipe, capture):
            recipe = deepcopy(recipe)
            parameters = recipe["workflow"]["parameters"]
            resources = [("topic_catalog", parameters, "topic_catalog_file")]
            resources += [
                ("prompt_" + stage, parameters[stage], "prompt_file")
                for stage in ("scenario", "qa", "classification", "inspection")
            ]
            for name, target, key in resources:
                if target.get(key):
                    path = Path(target[key])
                    target[key] = {
                        "sha256": snapshot(path, self.limits.max_evidence_bytes)[
                            "sha256"
                        ]
                    }
                    if capture:
                        paths[name] = path
            return recipe

        if normalized(expected, False) != normalized(actual, True):
            raise IntegrityError(
                "Executed workflow recipe differs from the task configuration"
            )
        if (root / "batch-source.json").exists():
            paths["batch-source.json"] = root / "batch-source.json"
        inputs = {
            name: {
                "path": str(path),
                "snapshot": snapshot(path, self.limits.max_evidence_bytes),
            }
            for name, path in paths.items()
        }
        if (
            sum(info["snapshot"]["bytes"] for info in inputs.values())
            > self.limits.max_evidence_bytes
        ):
            raise ValueError("Training export evidence exceeds the configured limit")
        source = {"submission": record["source"]}
        retrieval = ((record.get("result") or {}).get("retrieval") or {}).get(
            "job_id"
        ) or record["source"].get("retrieval_job_id")
        if retrieval:
            source["retrieval_job_id"] = retrieval
        if "batch-source.json" in inputs:
            captured = json.loads(paths["batch-source.json"].read_text())
            workspace = self.control.registry.get(batch)["workspace"]
            if (
                captured["batch_id"] != retrieval
                or captured.get("workspace") != workspace
                or captured.get("manifest", {}).get("workspace") != workspace
                or captured.get("manifest", {}).get("batch_id") != retrieval
            ):
                raise IntegrityError(
                    "Workflow source snapshot refers to another retrieval"
                )
            source["retrieval_manifest_sha256"] = captured["manifest_sha256"]
        export = {
            "export_id": identifier("op"),
            "data_batch_id": batch,
            "task_id": task,
            "config_revision_id": record["config_revision_id"],
            "workflow_run_id": output.name,
            "format": shape,
            "selection": selection,
            "state": "queued",
            "actor": self.actor,
            "created_at": time.time(),
            "updated_at": time.time(),
            "error": None,
            "counts": {},
            "spec": {
                "inputs": inputs,
                "limits": self.limits.model_dump(),
                "configuration_manifest_sha256": record["manifest_digest"],
                "workflow_fingerprint": stamp,
                "source": source,
            },
        }
        with self.store.transaction() as db:
            previous = self.control.registry.prior(
                db, self.actor, "training_export", batch, request_id, fingerprint
            )
            if previous:
                return self.store.read(db, batch, task, previous["export_id"])
            db.execute(
                "INSERT INTO training_exports VALUES (?,?,?,?)",
                (export["export_id"], batch, task, json.dumps(export)),
            )
            self.control.registry.remember(
                db,
                self.actor,
                "training_export",
                batch,
                request_id,
                fingerprint,
                {"export_id": export["export_id"]},
            )
        return export

    @staticmethod
    def identity(record):
        return {
            key: record[key]
            for key in (
                "export_id",
                "data_batch_id",
                "task_id",
                "config_revision_id",
                "workflow_run_id",
                "format",
                "selection",
            )
        }

    def status(self, batch, task, export):
        self.control.allow(self.actor, "read", batch)
        self.control.registry.task(batch, task)
        record = self.store.get(batch, task, export)
        directory = self.directory(batch, task, export)
        marker = directory / "export.json"
        if marker.exists() or marker.is_symlink():
            try:
                manifest = verify_publication(
                    directory, self.identity(record), self.limits.max_output_bytes
                )
            except (ValueError, OSError, KeyError, TypeError) as exc:
                raise IntegrityError("Training export publication is invalid") from exc
            if record["state"] != "completed":
                record = self.store.update(
                    batch,
                    task,
                    export,
                    state="completed",
                    counts=manifest["counts"],
                    error=None,
                )
        elif record["state"] == "completed":
            raise IntegrityError(
                "Completed training export lost its publication marker"
            )
        elif record["state"] == "running":
            if record.get("worker_owner") and owner_gone(record["worker_owner"]):
                record = self.store.update(
                    batch,
                    task,
                    export,
                    state="interrupted",
                    error="Export worker exited without a complete publication; submit a new request to retry",
                )
            progress = directory / "progress.json"
            if progress.exists() and progress == progress.resolve():
                try:
                    record["counts"] = json.loads(progress.read_bytes())
                except (OSError, ValueError):
                    pass
        return {
            key: record.get(key)
            for key in (
                "export_id",
                "data_batch_id",
                "task_id",
                "config_revision_id",
                "format",
                "selection",
                "state",
                "counts",
                "error",
                "created_at",
                "updated_at",
            )
        }

    def list(self, batch, task):
        self.control.allow(self.actor, "read", batch)
        self.control.registry.task(batch, task)
        return {
            "items": [
                self.status(batch, task, row["export_id"])
                for row in self.store.list(batch, task)
            ]
        }

    def run(self, batch, task, export):
        self.task(batch, task, write=True)
        record = self.store.get(batch, task, export)
        directory = self.directory(batch, task, export)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            directory / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            record = self.store.get(batch, task, export)
            if record["state"] != "queued":
                return
            _, _, _, settings = self.task(batch, task, write=True)
            if workflow_fingerprint(settings) != record["spec"]["workflow_fingerprint"]:
                raise ValueError("Workflow environment changed before training export")
            spec = {**self.identity(record), **record["spec"]}
            spec_file = directory / "spec.json"
            if spec_file.exists():
                raise Conflict("An interrupted export attempt must not be replayed")
            atomic_json(spec_file, spec)
            self.store.update(
                batch, task, export, state="running", worker_owner=process_owner()
            )
            environment = clean_environment(settings.project_directory)
            environment["PYTHONPATH"] = str(settings.project_directory / "src")
            with (
                (directory / "stdout.log").open("xb") as stdout,
                (directory / "stderr.log").open("xb") as stderr,
            ):
                os.chmod(directory / "stdout.log", 0o600)
                os.chmod(directory / "stderr.log", 0o600)
                process = subprocess.Popen(
                    [
                        str(settings.python),
                        str(Path(__file__).with_name("worker.py")),
                        str(spec_file),
                    ],
                    cwd=settings.project_directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                try:
                    child = process_owner()
                    child["pid"] = process.pid
                    fields = (
                        (Path("/proc") / str(process.pid) / "stat")
                        .read_text()
                        .rsplit(")", 1)[1]
                        .split()
                    )
                    child["start"] = fields[19]
                    self.store.update(batch, task, export, worker_owner=child)
                    code = process.wait(timeout=self.limits.timeout_seconds)
                except BaseException:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    raise
            if code != 0:
                raise ValueError("Official workflow evidence validation failed")
            self.status(batch, task, export)
            if self.store.get(batch, task, export)["state"] != "completed":
                raise IntegrityError("Export worker exited without a valid publication")
        except Exception as exc:
            from lightrag.utils import logger

            logger.exception("Training export failed: %s", export)
            self.store.update(
                batch,
                task,
                export,
                state="failed",
                error=type(exc).__name__ + ": inspect the retained export log",
            )
        finally:
            os.close(descriptor)

    def artifact(self, batch, task, export, name):
        status = self.status(batch, task, export)
        if name not in FILES or name != "log" and status["state"] != "completed":
            raise Conflict("Wait for complete training export publication")
        path = self.directory(batch, task, export) / FILES[name]
        if path != path.resolve() or not path.is_file():
            raise KeyError(name)
        return path
