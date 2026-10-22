"""Durable sequential evidence jobs, independent of ingestion writes.

Retrieval adapters must return complete entity-package responses. See
docs/design/BatchRetrieval.md for progress, recovery and handoff semantics.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import shutil
import uuid
from urllib.parse import urlsplit

import httpx
import psutil

from lightrag.entity_packages import PackageSettings, from_rag, equivalence_judge
from lightrag.batch_dedup import deduplicate_result
from lightrag.evidence_export import record_for, publish


ACTIVE = {"queued", "running"}


def now():
    return datetime.now(timezone.utc).isoformat()


def owner_alive(pid, birth):
    try:
        return psutil.Process(pid).create_time() == birth
    except psutil.AccessDenied:
        return True
    except psutil.Error:
        return False


class BatchConflict(Exception):
    pass


class ModelUnavailable(Exception):
    pass


class BatchManager:
    def __init__(self, rag, root=None, retrieve=None, *, focus=True, selector=None):
        self.rag = rag
        workspace = getattr(rag, "workspace", "")
        if (
            Path(workspace).name != workspace
            or workspace in {".", ".."}
            or "/" in workspace
            or "\\" in workspace
        ):
            raise ValueError("Batch workspace must be a single directory name")
        configured = os.getenv("LIGHTRAG_BATCH_OUTPUT_DIR", "").strip()
        self.root = (
            Path(
                root
                or (
                    Path(configured) / (workspace or "default")
                    if configured
                    else Path(rag.working_dir) / workspace / "batch_retrieval"
                )
            )
            .expanduser()
            .resolve()
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "jobs.sqlite3"
        self.retrieve = retrieve or from_rag
        self.focus = focus
        self.selector = selector
        self.tasks = set()
        self.pid = os.getpid()
        self.birth = psutil.Process().create_time()
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL, pid INTEGER NOT NULL, birth REAL NOT NULL)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _save(self, db, job):
        job["updated_at"] = now()
        db.execute(
            "UPDATE jobs SET body=?,status=? WHERE id=?",
            (json.dumps(job, ensure_ascii=False), job["status"], job["id"]),
        )

    def _read(self, db, job_id):
        row = db.execute(
            "SELECT body,pid,birth FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        job = json.loads(row[0])
        if job["status"] in ACTIVE:
            alive = owner_alive(row[1], row[2])
            if not alive:
                job.update(status="interrupted", current_center=None, finished_at=now())
                for item in job["items"]:
                    if item["status"] == "running":
                        item["status"] = "interrupted"
                self._save(db, job)
        if job["handoff"]["status"] == "submitting" and not owner_alive(
            job["handoff"].get("pid", row[1]), job["handoff"].get("birth", row[2])
        ):
            job["handoff"] = {"status": "unconfirmed"}
            self._save(db, job)
        return job

    def get(self, job_id):
        with self.connect() as db:
            return self._read(db, job_id)

    def update(self, job_id, change):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._read(db, job_id)
            change(job)
            self._save(db, job)
        return job

    async def start(
        self, centers: list[str], settings: PackageSettings, *, reuse_from=None
    ):
        centers = list(dict.fromkeys(name.strip() for name in centers))
        if not centers or any(not name for name in centers):
            raise ValueError("Select at least one nonempty center")
        settings.validate()
        source_job = self.get(reuse_from) if reuse_from else None
        if source_job and (
            source_job["status"] in ACTIVE
            or source_job.get("policy") != "focused_topics_relevance_v1"
            or source_job["handoff"]["status"] != "not_submitted"
            or [i["center"] for i in source_job["items"]] != centers
            or source_job["settings"] != vars(settings)
        ):
            raise BatchConflict(
                "Only matching unsubmitted focused snapshots can be rebuilt"
            )
        if self.retrieve is from_rag:
            endpoint = urlsplit(os.getenv("EMBEDDING_BINDING_HOST", ""))
            if endpoint.hostname in {"127.0.0.1", "localhost", "::1"}:
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(
                            endpoint.hostname,
                            endpoint.port
                            or (443 if endpoint.scheme == "https" else 80),
                        ),
                        timeout=3,
                    )
                    writer.close()
                    await writer.wait_closed()
                except (OSError, TimeoutError) as exc:
                    raise ModelUnavailable("embedding_unavailable") from exc
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM jobs WHERE status IN ('queued','running')"
                )
            ]
            for job_id in ids:
                if self._read(db, job_id)["status"] in ACTIVE:
                    raise BatchConflict("A batch is already running in this workspace")
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "status": "queued",
                "created_at": now(),
                "updated_at": now(),
                "finished_at": None,
                "settings": vars(settings),
                "total": len(centers),
                "processed": 0,
                "failed": 0,
                "empty": 0,
                "package_count": 0,
                "duplicate_chunks": 0,
                "skipped": 0,
                "filtered": 0,
                "review_pending": 0,
                "policy": "focused_topics_relevance_v1"
                if self.focus
                else "legacy_first_committed_v1",
                "current_center": None,
                "cancel_requested": False,
                "handoff": {"status": "not_submitted"},
                "items": [
                    {"center": name, "status": "queued", "packages": 0}
                    for name in centers
                ],
            }
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?)",
                (
                    job_id,
                    json.dumps(job, ensure_ascii=False),
                    "queued",
                    self.pid,
                    self.birth,
                ),
            )
        (self.root / job_id).mkdir(exist_ok=True)
        if source_job:
            try:
                for path in (self.root / reuse_from).glob("*.json"):
                    if (
                        path.name.startswith("candidate-")
                        or path.name.startswith("focus-")
                        and path.name != "focus-dedup-audit.json"
                    ):
                        shutil.copyfile(path, self.root / job_id / path.name)
                job = self.update(job_id, lambda j: j.update(rebuild_of=reuse_from))
            except BaseException:
                self.update(
                    job_id, lambda j: j.update(status="interrupted", finished_at=now())
                )
                raise
        if self.focus:
            from lightrag.focused_batch import run_focused

            task = asyncio.create_task(run_focused(self, job_id, settings))
        else:
            task = asyncio.create_task(self._run(job_id, settings))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    def cancel(self, job_id):
        return self.update(
            job_id,
            lambda job: (
                job.update(cancel_requested=True) if job["status"] in ACTIVE else None
            ),
        )

    async def resume(self, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._read(db, job_id)
            if (
                job.get("policy") != "focused_topics_relevance_v1"
                or job["status"]
                not in {"cancelled", "interrupted", "completed_with_errors"}
                or job["package_count"]
                or job["handoff"]["status"] != "not_submitted"
            ):
                raise BatchConflict(
                    "Only an unsubmitted focused preparation with no published packages can resume"
                )
            active_ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM jobs WHERE status IN ('queued','running')"
                )
            ]
            if any(self._read(db, active)["status"] in ACTIVE for active in active_ids):
                raise BatchConflict("Another batch is already running")
            job.update(
                status="queued",
                cancel_requested=False,
                finished_at=None,
                failed=0,
                processed=0,
                empty=0,
                skipped=0,
                filtered=0,
                duplicate_chunks=0,
                review_pending=0,
            )
            for item in job["items"]:
                item.update(status="queued", packages=0)
                item.pop("error_type", None)
            self._save(db, job)
            db.execute(
                "UPDATE jobs SET pid=?,birth=? WHERE id=?",
                (self.pid, self.birth, job_id),
            )
        from lightrag.focused_batch import run_focused

        task = asyncio.create_task(
            run_focused(self, job_id, PackageSettings(**job["settings"]))
        )
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def _run(self, job_id, settings):
        try:
            previous = []
            self.update(job_id, lambda job: job.update(status="running"))
            for index, item in enumerate(self.get(job_id)["items"]):
                if self.get(job_id)["cancel_requested"]:
                    break
                center = item["center"]

                def begin(job):
                    job["current_center"] = center
                    job["items"][index]["status"] = "running"

                self.update(job_id, begin)
                try:
                    result = await asyncio.wait_for(
                        self.retrieve(self.rag, center, settings), timeout=600
                    )
                    packages = result["packages"]
                    if not isinstance(packages, list) or len(packages) > 1:
                        raise ValueError(
                            "One center must yield at most one complete package"
                        )
                    if packages:
                        if packages[0].get("center") != center:
                            raise ValueError(
                                "Adapter returned evidence for a different center"
                            )
                        record = packages[0]["distillation_input"]
                        if (
                            not isinstance(record.get("text"), str)
                            or not record["text"].strip()
                        ):
                            raise ValueError("Adapter returned an empty source record")
                        result = await asyncio.wait_for(
                            deduplicate_result(
                                result,
                                previous,
                                settings,
                                lambda text: len(self.rag.tokenizer.encode(text)),
                                equivalence_judge(self.rag)
                                if settings.near_dedup
                                and getattr(self.rag, "role_llm_funcs", None)
                                else None,
                            ),
                            timeout=600,
                        )
                        packages = result["packages"]
                    duplicate_chunks = result.get("summary", {}).get(
                        "cross_package_duplicates", 0
                    )
                    if packages or duplicate_chunks:
                        path = self.root / job_id / f"entity-{index:04d}.json"
                        temporary = path.with_suffix(".tmp")
                        await asyncio.to_thread(
                            temporary.write_text,
                            json.dumps(result, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        temporary.replace(path)
                    state = (
                        "completed"
                        if packages
                        else "skipped_duplicate"
                        if duplicate_chunks
                        else "empty"
                    )
                    error = None
                except Exception as exc:
                    packages, state, error = [], "failed", type(exc).__name__
                    duplicate_chunks = 0

                def finish(job):
                    row = job["items"][index]
                    row.update(status=state, packages=len(packages))
                    if error:
                        row["error_type"] = error
                    job["processed"] += 1
                    job["package_count"] += len(packages)
                    job["failed"] += int(state == "failed")
                    job["empty"] += int(state == "empty")
                    job["duplicate_chunks"] += duplicate_chunks
                    job["skipped"] += int(state == "skipped_duplicate")

                self.update(job_id, finish)
                # Failed/uncommitted results cannot claim evidence ownership.
                if packages:
                    previous.append(result)

            def complete(job):
                status = (
                    "cancelled"
                    if job["cancel_requested"] and job["processed"] < job["total"]
                    else "completed_with_errors"
                    if job["failed"]
                    else "completed"
                )
                job.update(status=status, current_center=None, finished_at=now())

            export_path = await asyncio.to_thread(self.persist_export, job_id)
            self.update(job_id, lambda j: j.update(export_path=export_path))
            self.update(job_id, complete)
        except BaseException:

            def interrupted(job):
                job.update(status="interrupted", current_center=None, finished_at=now())
                for item in job["items"]:
                    if item["status"] == "running":
                        item["status"] = "interrupted"

            self.update(job_id, interrupted)
            raise

    def records(self, job_id):
        job = self.get(job_id)
        related = {}
        for index, item in enumerate(job["items"]):
            if item["status"] in {"completed", "skipped_duplicate"}:
                result = json.loads(
                    (self.root / job_id / f"entity-{index:04d}.json").read_text(
                        encoding="utf-8"
                    )
                )
                for ref in result.get("shared_evidence", []):
                    related.setdefault(ref["owner_package_id"], set()).add(
                        item["center"]
                    )
        for index, item in enumerate(job["items"]):
            if item["status"] == "completed":
                result = json.loads(
                    (self.root / job_id / f"entity-{index:04d}.json").read_text(
                        encoding="utf-8"
                    )
                )
                for package in result["packages"]:
                    record = record_for(
                        package, job_id, getattr(self.rag, "workspace", "")
                    )
                    record["metadata"]["related_centers"] = sorted(
                        {
                            package["center"],
                            *related.get(package.get("package_id"), set()),
                        }
                    )
                    yield record

    def persist_export(self, job_id):
        return publish(self, job_id)

    def jsonl(self, job_id):
        for record in self.records(job_id):
            yield json.dumps(record, ensure_ascii=False) + "\n"

    async def handoff(self, job_id):
        url = os.getenv("DISTILLATION_INGEST_URL", "").strip()
        if not url:
            raise ValueError("Distillation endpoint is not configured")

        def reserve(job):
            if (
                job["status"] in ACTIVE
                or not job["package_count"]
                or not job.get("export_path")
            ):
                raise BatchConflict("Wait for a batch with complete packages")
            if job["handoff"]["status"] == "submitting":
                raise BatchConflict("Submission is already in progress")
            if job["handoff"]["status"] != "accepted":
                job["handoff"] = {
                    "status": "submitting",
                    "pid": self.pid,
                    "birth": self.birth,
                }

        job = self.update(job_id, reserve)
        if job["handoff"]["status"] == "accepted":
            return job
        headers = {"Idempotency-Key": "lightrag-" + job_id}
        key = os.getenv("DISTILLATION_INGEST_API_KEY", "")
        if key:
            headers["Authorization"] = "Bearer " + key
        try:
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                response = await client.post(
                    url,
                    json={"batch_id": job_id, "records": list(self.records(job_id))},
                    headers=headers,
                )
                response.raise_for_status()
                receipt = response.json()
                if receipt.get("accepted") is not True:
                    raise ValueError(
                        "Distillation system did not acknowledge acceptance"
                    )
            state = {
                "status": "accepted",
                "receipt_id": str(receipt.get("job_id", ""))[:200],
            }
        except Exception:
            self.update(
                job_id, lambda job: job.update(handoff={"status": "unconfirmed"})
            )
            raise
        return self.update(job_id, lambda job: job.update(handoff=state))
