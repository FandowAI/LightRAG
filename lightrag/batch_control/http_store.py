"""Generation-owned HTTP mailboxes; interrupted requests are never replayed."""

from contextlib import contextmanager
import json
import time
import uuid
import hashlib

from .runtime_process import atomic_json
from .storage import Conflict, IntegrityError
from .resources import digest

TERMINAL = {"completed", "failed", "cancelled"}
PREFIXES = (
    "documents",
    "graph",
    "graphs",
    "query",
    "entity-centers",
    "entity-merge",
    "prompt-admin",
    "batch-retrieval",
)


def business_action(method, path):
    parts = path.split("/")
    if (
        method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
        or not path.startswith("/")
        or any(part in {".", ".."} for part in parts)
        or "\\" in path
        or "\x00" in path
        or (
            path != "/health"
            and parts[1] not in PREFIXES
            and not path.startswith("/integrations/data-work-flow/")
        )
    ):
        raise ValueError("Unsupported batch business endpoint")
    if method in {"GET", "HEAD"}:
        return "read"
    if method == "POST" and (
        path == "/entity-merge/preview"
        or path.startswith("/integrations/data-work-flow/submit")
        or "/jobs/" in path
    ):
        return "submit"
    if parts[1] in {"query", "entity-centers", "batch-retrieval"}:
        return "submit"
    return "edit"


class HttpStore:
    def __init__(self, registry, paths):
        self.registry, self.paths = registry, paths

    @contextmanager
    def transaction(self):
        with self.registry.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS business_http (id TEXT PRIMARY KEY, batch TEXT NOT NULL, state TEXT NOT NULL, record TEXT NOT NULL)"
            )
            yield db

    @staticmethod
    def write(db, record):
        record["updated_at"] = time.time()
        db.execute(
            "INSERT INTO business_http VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,record=excluded.record",
            (record["id"], record["batch"], record["state"], json.dumps(record)),
        )
        return record

    @staticmethod
    def read(db, batch, identity):
        row = db.execute(
            "SELECT record FROM business_http WHERE batch=? AND id=?", (batch, identity)
        ).fetchone()
        if row is None:
            raise KeyError(identity)
        return json.loads(row[0])

    def directory(self, batch, identity):
        owner = self.registry.get(batch)
        return self.paths.under(
            self.paths.runtime_root, "business-http", owner["namespace"], identity
        )

    def submit(self, actor, batch, lease, method, path, query, headers, body):
        business_action(method, path)
        identity = "http-" + uuid.uuid4().hex
        directory = self.directory(batch, identity)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        metadata = {
            "method": method,
            "path": path,
            "query": query,
            "headers": headers,
        }
        atomic_json(directory / "request.json", metadata)
        request = directory / "request.bin"
        with request.open("xb") as handle:
            request.chmod(0o600)
            handle.write(body)
        with self.transaction() as db:
            owner = self.registry.row(db, batch)
            runtime_row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(runtime_row[0]) if runtime_row else {}
            if (
                owner["pending"]
                or runtime.get("state") != "ready"
                or runtime.get("lease_id") != lease.lease_id
                or runtime.get("generation") != lease.generation
            ):
                raise Conflict("Batch runtime changed before HTTP admission")
            return self.write(
                db,
                {
                    "id": identity,
                    "batch": batch,
                    "actor": actor,
                    "generation": lease.generation,
                    "lease_id": lease.lease_id,
                    "state": "queued",
                    "created_at": time.time(),
                    "bytes_written": 0,
                    "status": None,
                    "headers": [],
                    "error": None,
                    "request_digest": digest(
                        {**metadata, "body_sha256": hashlib.sha256(body).hexdigest()}
                    ),
                },
            )

    def get(self, batch, identity):
        with self.transaction() as db:
            self.reconcile(db, batch)
            return self.read(db, batch, identity)

    def reconcile(self, db, batch):
        """Retire only requests whose owning lease is authoritatively gone."""
        row = db.execute(
            "SELECT record FROM runtimes WHERE batch=?", (batch,)
        ).fetchone()
        runtime = json.loads(row[0]) if row else None
        for row in db.execute(
            "SELECT record FROM business_http WHERE batch=? AND state IN ('queued','running')",
            (batch,),
        ).fetchall():
            record = json.loads(row[0])
            if (
                runtime is None
                or runtime.get("state") in {"failed", "stopped"}
                or any(
                    runtime.get(key) != record[key]
                    for key in ("generation", "lease_id")
                )
            ):
                record.update(
                    state="failed",
                    error="Owning runtime was lost or retired; no request replayed",
                )
                self.write(db, record)

    def active(self, batch):
        with self.transaction() as db:
            self.reconcile(db, batch)
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT record FROM business_http WHERE batch=? AND state IN ('queued','running') ORDER BY rowid",
                    (batch,),
                )
            ]

    def claim(self, spec):
        with self.transaction() as db:
            batch = spec["data_batch_id"]
            row = db.execute(
                "SELECT record FROM runtimes WHERE batch=?", (batch,)
            ).fetchone()
            runtime = json.loads(row[0]) if row else {}
            if (
                any(
                    runtime.get(key) != spec.get(key)
                    for key in ("generation", "lease_id", "pid", "process_identity")
                )
                or runtime.get("state") != "ready"
            ):
                return None
            for row in db.execute(
                "SELECT record FROM business_http WHERE batch=? AND state='queued' ORDER BY rowid",
                (batch,),
            ):
                record = json.loads(row[0])
                if (
                    record["generation"] != spec["generation"]
                    or record["lease_id"] != spec["lease_id"]
                ):
                    record.update(
                        state="failed",
                        error="Owning runtime was replaced; resubmit explicitly",
                    )
                    self.write(db, record)
                    continue
                record["state"] = "running"
                return self.write(db, record)
        return None

    def update(self, batch, identity, generation, **values):
        with self.transaction() as db:
            record = self.read(db, batch, identity)
            if record["generation"] != generation:
                raise IntegrityError("HTTP response belongs to another generation")
            if record["state"] in TERMINAL:
                return record
            record.update(values)
            return self.write(db, record)

    def cancel(self, batch, identity):
        record = self.get(batch, identity)
        return self.update(batch, identity, record["generation"], state="cancelled")
