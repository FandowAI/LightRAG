"""Persist admission across processes; elapsed time alone never releases a writer.

See docs/design/BatchProviderAdmission.md for charges, unknown outcomes and FIFO.
This module is also loaded by the unmodified workflow's standalone launcher.
"""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit
import uuid


class QuotaError(RuntimeError):
    pass


def endpoint(value):
    parsed = urlsplit(str(value))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Provider endpoints must be credential-free HTTP URLs")
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/embeddings", "/responses"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if parsed.port is not None and (parsed.scheme, parsed.port) not in {
        ("http", 80),
        ("https", 443),
    }:
        host += ":" + str(parsed.port)
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


@dataclass(frozen=True)
class Policy:
    max_concurrent: int = 32
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    default_output_tokens: int = 8192
    queue_timeout_seconds: float = 900

    def __post_init__(self):
        for value in (
            self.max_concurrent,
            self.default_output_tokens,
            self.requests_per_minute,
            self.tokens_per_minute,
        ):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError("Provider capacities must be positive integers")
        if (
            not isinstance(self.queue_timeout_seconds, (float, int))
            or isinstance(self.queue_timeout_seconds, bool)
            or not math.isfinite(self.queue_timeout_seconds)
            or self.queue_timeout_seconds <= 0
        ):
            raise ValueError("Provider queue deadline must be positive and finite")


def configuration(value):
    if set(value) - {"defaults", "endpoints"}:
        raise ValueError("Unknown provider admission settings")
    defaults = asdict(Policy(**value.get("defaults", {})))
    overrides = {}
    for url, data in value.get("endpoints", {}).items():
        normalized = endpoint(url)
        if normalized in overrides:
            raise ValueError("Provider endpoint has duplicate policy declarations")
        overrides[normalized] = asdict(Policy(**{**defaults, **data}))
    return {"defaults": defaults, "endpoints": overrides}


def process_owner():
    namespace = os.readlink("/proc/self/ns/pid")
    if namespace != os.readlink("/proc/1/ns/pid"):
        raise QuotaError("Procfs does not represent this process's PID namespace")
    stat = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    return {
        "pid": os.getpid(),
        "start": stat[19],
        "namespace": namespace,
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "machine": hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest(),
    }


def owner_gone(owner):
    try:
        current = process_owner()
    except (OSError, QuotaError):
        return False
    if owner["machine"] != current["machine"]:
        return False
    if owner["boot"] != current["boot"]:
        return True
    if owner["namespace"] != current["namespace"]:
        return False
    try:
        stat = Path("/proc") / str(owner["pid"]) / "stat"
        fields = stat.read_text().rsplit(")", 1)[1].split()
        return fields[0] == "Z" or fields[19] != owner["start"]
    except FileNotFoundError:
        return True
    except (OSError, IndexError):
        return False


class QuotaStore:
    def __init__(self, path, *, clock=time.time, gone=owner_gone):
        self.path, self.clock, self.gone = Path(path), clock, gone
        if not self.path.is_absolute() or self.path != self.path.resolve():
            raise ValueError(
                "Provider admission registry must be a canonical absolute path"
            )

    @contextmanager
    def database(self, *, write=False, create=False):
        if self.path != self.path.resolve():
            raise QuotaError("Provider admission registry was redirected")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(
            self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True,
            timeout=5,
        )
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def configure(self, value):
        value = configuration(value)
        with self.database(create=True) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS quota_settings (id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_requests (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, pool TEXT NOT NULL, endpoint TEXT NOT NULL, batch TEXT NOT NULL, task TEXT, owner TEXT NOT NULL, state TEXT NOT NULL, tokens INTEGER NOT NULL, created REAL NOT NULL, granted REAL, finished REAL, reason TEXT)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS provider_queue ON provider_requests(pool,state,sequence)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS provider_task ON provider_requests(batch,task)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS provider_grants ON provider_requests(pool,granted)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS provider_finishes ON provider_requests(pool,finished)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS provider_recovery (pool TEXT PRIMARY KEY, checked REAL NOT NULL)"
            )
            db.execute(
                "INSERT INTO quota_settings VALUES (1,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                (json.dumps(value),),
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def policy(db, url):
        row = db.execute("SELECT record FROM quota_settings WHERE id=1").fetchone()
        if row is None:
            raise QuotaError("Provider admission is not configured")
        value = json.loads(row[0])
        return Policy(**value["endpoints"].get(endpoint(url), value["defaults"]))

    def limits(self, url):
        with self.database() as db:
            return self.policy(db, url)

    def ready(self):
        with self.database() as db:
            row = db.execute("SELECT record FROM quota_settings WHERE id=1").fetchone()
            if row is None:
                raise QuotaError("Shared provider admission is not configured")
            return configuration(json.loads(row[0]))

    def enqueue(self, url, authentication, batch, task, tokens):
        if type(tokens) is not int or tokens < 0:
            raise ValueError("Token reservations must be nonnegative integers")
        url = endpoint(url)
        pool = (
            "provider-"
            + hashlib.sha256((url + "\0" + authentication).encode()).hexdigest()
        )
        identity, now = "request-" + uuid.uuid4().hex, self.clock()
        with self.database(write=True) as db:
            policy = self.policy(db, url)
            rejected = (
                policy.tokens_per_minute is not None
                and tokens > policy.tokens_per_minute
            )
            db.execute(
                "INSERT INTO provider_requests(id,pool,endpoint,batch,task,owner,state,tokens,created,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    pool,
                    url,
                    batch,
                    task,
                    json.dumps(process_owner()),
                    "rejected" if rejected else "queued",
                    tokens,
                    now,
                    "Request token reservation exceeds provider capacity"
                    if rejected
                    else None,
                ),
            )
        return identity

    def recover(self, db, pool, now):
        previous = db.execute(
            "SELECT checked FROM provider_recovery WHERE pool=?", (pool,)
        ).fetchone()
        if previous and 0 <= now - previous[0] < 1:
            return
        db.execute(
            "INSERT INTO provider_recovery VALUES (?,?) ON CONFLICT(pool) DO UPDATE SET checked=excluded.checked",
            (pool, now),
        )
        owners = {}
        for row in db.execute(
            "SELECT id,owner,state FROM provider_requests WHERE pool=? AND state IN ('queued','granted')",
            (pool,),
        ).fetchall():
            if row["owner"] not in owners:
                owners[row["owner"]] = self.gone(json.loads(row["owner"]))
            if owners[row["owner"]]:
                db.execute(
                    "UPDATE provider_requests SET state=?,finished=?,reason=? WHERE id=?",
                    (
                        "orphaned" if row["state"] == "granted" else "cancelled",
                        now,
                        "Confirmed owner exit; no request was replayed",
                        row["id"],
                    ),
                )

    def poll(self, identity):
        now = self.clock()
        with self.database(write=True) as db:
            row = db.execute(
                "SELECT * FROM provider_requests WHERE id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            if row["state"] != "queued":
                return dict(row)
            self.recover(db, row["pool"], now)
            row = db.execute(
                "SELECT * FROM provider_requests WHERE id=?", (identity,)
            ).fetchone()
            if row["state"] != "queued":
                return dict(row)
            policy = self.policy(db, row["endpoint"])
            reason = None
            if now - row["created"] >= policy.queue_timeout_seconds:
                reason = "Provider admission queue deadline exceeded"
            if (
                policy.tokens_per_minute is not None
                and row["tokens"] > policy.tokens_per_minute
            ):
                reason = "Request token reservation exceeds provider capacity"
            if reason:
                db.execute(
                    "UPDATE provider_requests SET state='rejected',reason=?,finished=? WHERE id=?",
                    (reason, now, identity),
                )
            else:
                db.execute(
                    "UPDATE provider_requests SET state='rejected',finished=?,reason='Provider admission queue deadline exceeded' WHERE pool=? AND state='queued' AND created<=?",
                    (now, row["pool"], now - policy.queue_timeout_seconds),
                )
                first = db.execute(
                    "SELECT id FROM provider_requests WHERE pool=? AND state='queued' ORDER BY sequence LIMIT 1",
                    (row["pool"],),
                ).fetchone()
                active = db.execute(
                    "SELECT COUNT(*) FROM provider_requests WHERE pool=? AND state='granted'",
                    (row["pool"],),
                ).fetchone()[0]
                requests = db.execute(
                    "SELECT COUNT(*) FROM provider_requests WHERE pool=? AND granted>=?",
                    (row["pool"], now - 60),
                ).fetchone()[0]
                tokens = (
                    db.execute(
                        "SELECT SUM(tokens) FROM provider_requests WHERE pool=? AND granted IS NOT NULL AND (state='granted' OR finished>=?)",
                        (row["pool"], now - 60),
                    ).fetchone()[0]
                    or 0
                )
                if (
                    first
                    and first[0] == identity
                    and active < policy.max_concurrent
                    and (
                        policy.requests_per_minute is None
                        or requests < policy.requests_per_minute
                    )
                    and (
                        policy.tokens_per_minute is None
                        or tokens + row["tokens"] <= policy.tokens_per_minute
                    )
                ):
                    db.execute(
                        "UPDATE provider_requests SET state='granted',granted=?,reason=NULL WHERE id=?",
                        (now, identity),
                    )
            return dict(
                db.execute(
                    "SELECT * FROM provider_requests WHERE id=?", (identity,)
                ).fetchone()
            )

    def finish(self, identity, *, tokens=None, reason=None):
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise ValueError("Observed token usage must be a nonnegative integer")
        with self.database(write=True) as db:
            row = db.execute(
                "SELECT * FROM provider_requests WHERE id=?", (identity,)
            ).fetchone()
            if row is None:
                raise KeyError(identity)
            if row["state"] in {"queued", "granted"}:
                db.execute(
                    "UPDATE provider_requests SET state=?,tokens=?,finished=?,reason=? WHERE id=?",
                    (
                        "finished" if row["state"] == "granted" else "cancelled",
                        row["tokens"] if tokens is None else tokens,
                        self.clock(),
                        reason,
                        identity,
                    ),
                )

    def summary(self, batch=None, task=None):
        with self.database(write=True) as db:
            where, args = [], []
            if batch is not None:
                where.append("batch=?")
                args.append(batch)
            if task is not None:
                where.append("task=?")
                args.append(task)
            clause = " WHERE " + " AND ".join(where) if where else ""
            for pool in db.execute(
                "SELECT DISTINCT pool FROM provider_requests" + clause, args
            ).fetchall():
                self.recover(db, pool[0], self.clock())
            rows = db.execute(
                "SELECT pool,endpoint,state,COUNT(*) AS count FROM provider_requests"
                + clause
                + " GROUP BY pool,endpoint,state",
                args,
            ).fetchall()
            result = {}
            for row in rows:
                item = result.setdefault(
                    row["pool"],
                    {
                        "pool_id": row["pool"],
                        "endpoint": row["endpoint"],
                        "queued": 0,
                        "active": 0,
                        "finished": 0,
                        "cancelled": 0,
                        "rejected": 0,
                        "orphaned": 0,
                        "limits": asdict(self.policy(db, row["endpoint"])),
                    },
                )
                item["active" if row["state"] == "granted" else row["state"]] = row[
                    "count"
                ]
            return {"items": list(result.values())}
