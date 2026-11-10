"""Owned subprocess lifecycle and private, atomic runtime identity observations."""

from __future__ import annotations

import json
import hashlib
import re
import os
from pathlib import Path
import signal
import subprocess
from tempfile import NamedTemporaryFile
import time

import psutil


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with NamedTemporaryFile(
            "w", dir=path.parent, encoding="utf-8", delete=False
        ) as file:
            temporary = Path(file.name)
            os.chmod(temporary, 0o600)
            json.dump(value, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def process_identity(pid):
    return str(psutil.Process(pid).create_time())


def process_scope() -> dict:
    """Bind PID observations to their host, boot and procfs namespace."""
    namespace = os.readlink("/proc/self/ns/pid")
    visible = os.readlink("/proc/1/ns/pid")
    if namespace != visible:
        raise RuntimeError("Procfs does not represent this process's PID namespace")
    return {
        "schema_version": 1,
        "machine": hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest(),
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "namespace": namespace,
        "procfs_namespace": visible,
    }


def scope_disposition(recorded) -> str:
    """Unknown/foreign contexts cannot prove process absence or authorize a signal."""
    fields = {"schema_version", "machine", "boot", "namespace", "procfs_namespace"}
    if (
        not isinstance(recorded, dict)
        or set(recorded) != fields
        or type(recorded.get("schema_version")) is not int
        or recorded["schema_version"] != 1
        or any(
            type(recorded.get(key)) is not str or not recorded[key]
            for key in fields - {"schema_version"}
        )
        or recorded["namespace"] != recorded["procfs_namespace"]
        or re.fullmatch(r"[0-9a-f]{64}", recorded["machine"]) is None
        or re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", recorded["boot"]
        )
        is None
        or re.fullmatch(r"pid:\[\d+\]", recorded["namespace"]) is None
    ):
        return "unknown"
    try:
        current = process_scope()
    except (OSError, RuntimeError):
        return "unknown"
    if recorded["machine"] != current["machine"]:
        return "unknown"
    if recorded["boot"] != current["boot"]:
        return "rebooted"
    return "same" if recorded == current else "unknown"


class ProcessBackend:
    """Only a birth-time and exact command match authorize a process signal."""

    def __init__(self):
        self.children = {}

    def launch(self, spec, environment):
        if scope_disposition(spec.get("process_scope")) != "same":
            raise RuntimeError("Runtime launch cannot leave its reserved process scope")
        directory = Path(spec["attempt_directory"])
        log_path = directory / "worker.log"
        with log_path.open("ab") as log:
            os.chmod(log_path, 0o600)
            child = subprocess.Popen(
                [
                    spec["python"],
                    "-m",
                    "lightrag.batch_control.runtime_worker",
                    "--spec",
                    spec["spec_path"],
                ],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        self.children[child.pid] = child
        return child.pid, process_identity(child.pid)

    def ownership(self, record):
        disposition = scope_disposition(record.get("process_scope"))
        if disposition == "rebooted":
            return "gone"
        if disposition != "same":
            return "unknown"
        pid = record.get("pid")
        if pid is None:
            return "unknown"
        try:
            process = psutil.Process(pid)
            if str(process.create_time()) != record["process_identity"]:
                return "reused"
            if process.status() == psutil.STATUS_ZOMBIE:
                self._reap(pid)
                return "gone"
            expected = [
                record["python"],
                "-m",
                "lightrag.batch_control.runtime_worker",
                "--spec",
                record["spec_path"],
            ]
            command = process.cmdline()
            if not command and process.status() == psutil.STATUS_ZOMBIE:
                self._reap(pid)
                return "gone"
            if not command:
                return "unknown"
            if command != expected or os.getpgid(pid) != pid:
                return "foreign"
            return "owned"
        except psutil.NoSuchProcess:
            self._reap(pid)
            return "gone"
        except (psutil.AccessDenied, OSError):
            return "unknown"

    def _reap(self, pid):
        child = self.children.get(pid)
        if child is not None and child.poll() is not None:
            del self.children[pid]

    def terminate(self, record, timeout):
        ownership = self.ownership(record)
        if ownership == "gone":
            return
        if ownership != "owned":
            raise RuntimeError("Cannot prove process ownership; no signal sent")
        os.killpg(record["pid"], signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.ownership(record)
            if status in {"gone", "reused"}:
                return
            # Procfs can briefly expose an empty command during exit. Wait,
            # without another signal or prematurely releasing the reservation.
            time.sleep(0.05)
        # Preserve the reservation rather than killing a writer during a commit.
        raise TimeoutError("Managed runtime has not stopped; reservation remains held")

    def observe(self, record):
        path = Path(record["attempt_directory"]) / "health.json"
        if path.is_symlink():
            raise RuntimeError("Runtime observation must not be a symlink")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
