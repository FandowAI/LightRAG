#!/usr/bin/env python3
"""Preview or reset every workspace in this deployment; never execute on import."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]
CONFIRMATION = "DELETE_ALL_LIGHTRAG_DATA"


class ResetError(ValueError):
    """A safe, operator-facing reset diagnostic without credential values."""


MILVUS_NAMESPACES = {
    "LightRAG chunks vector storage": "chunks",
    "LightRAG entities vector storage": "entities",
    "LightRAG relationships vector storage": "relationships",
    "LightRAG entity name embeddings": "name_entities",
}


def absolute(value: str | Path, base: Path) -> Path:
    return Path(os.path.abspath(base / Path(value).expanduser()))


def overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def public_endpoint(uri: str) -> str:
    parsed = urlsplit(uri)
    if not parsed.scheme or not parsed.hostname:
        return "local endpoint (not displayed)"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")


def validate_root(path: Path, protected: list[Path]) -> None:
    if path.is_symlink() or path.resolve() != path:
        raise ResetError(f"Data root must not traverse a symlink: {path}")
    anchors = {
        Path(p) for p in ("/", "/root", "/data", "/data_new", "/tmp", "/home", "/var")
    }
    if path in anchors or any(overlaps(path, item.resolve()) for item in protected):
        raise ResetError(f"Refusing an unsafe or protected data root: {path}")


@dataclass
class Deployment:
    project: Path
    config: Path
    workflow_project: Path
    environment: dict
    roots: dict[str, Path]
    protected: list[Path]

    @classmethod
    def load(cls, project: Path, env_file: Path, include_workflow_runs: bool = False):
        from dotenv import dotenv_values
        import yaml

        # Explicit configuration only: a caller's test shell must not redirect a reset.
        values = dict(dotenv_values(env_file))
        required = {
            "LIGHTRAG_KV_STORAGE": "JsonKVStorage",
            "LIGHTRAG_DOC_STATUS_STORAGE": "JsonDocStatusStorage",
            "LIGHTRAG_GRAPH_STORAGE": "Neo4JStorage",
            "LIGHTRAG_VECTOR_STORAGE": "MilvusVectorDBStorage",
        }
        for key, backend in required.items():
            if values.get(key, backend) != backend:
                raise ResetError(f"Unsupported backend for this reset script: {key}")
        for key in (
            "NEO4J_URI",
            "NEO4J_USERNAME",
            "NEO4J_PASSWORD",
            "NEO4J_DATABASE",
            "MILVUS_URI",
            "MILVUS_DB_NAME",
        ):
            if not values.get(key):
                raise ResetError(f"Missing explicit configuration: {key}")
        if values["NEO4J_DATABASE"] == "system":
            raise ResetError("Refusing the Neo4j system database")
        config_file = absolute(
            values.get("LIGHTRAG_DATA_WORKFLOW_CONFIG")
            or "../config/data_workflow.yaml",
            project,
        )
        bridge = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        config = config_file.parent
        workflow = absolute(bridge["project_directory"], config)
        inputs = absolute(values.get("INPUT_DIR") or "inputs", project)
        storage = absolute(values.get("WORKING_DIR") or "rag_storage", project)
        roots = {
            "inputs": inputs,
            "storage": storage,
            "scan_spool": absolute(
                values.get("SCAN_SPOOL_DIR") or str(storage / "scan_spool"), project
            ),
            "distillation_jobs": absolute(bridge["jobs_directory"], config),
            "temporary_files": project / "temp",
        }
        if values.get("LIGHTRAG_BATCH_OUTPUT_DIR"):
            roots["batch_outputs"] = absolute(
                values["LIGHTRAG_BATCH_OUTPUT_DIR"], project
            )
        if include_workflow_runs:
            roots["standalone_workflow_runs"] = workflow / "runs"
        # Older local default stores may coexist with the currently selected roots.
        for key, candidate in (
            ("legacy_inputs", project / "inputs"),
            ("legacy_storage", project / "rag_storage"),
        ):
            if candidate.exists() and candidate not in roots.values():
                roots[key] = candidate
        protected = [config, env_file, project / "config", project / "config.ini"]
        for repo in (project, workflow):
            protected += [
                repo / name
                for name in (
                    ".git",
                    ".venv",
                    ".venv-qa",
                    "src",
                    "lightrag",
                    "lightrag_webui",
                    "scripts",
                    "tests",
                    "docs",
                    "examples",
                )
            ]
        protected += [
            Path("/data/lightrag") / name
            for name in ("neo4j", "milvus", "minio", "etcd", "config", "deployment")
        ]
        imports = json.loads(values.get("LIGHTRAG_SERVER_IMPORT_ROOTS") or "[]")
        protected += [absolute(value, project) for value in imports]
        for path in roots.values():
            if path in {project, workflow, project.parent}:
                raise ResetError(f"Refusing a project directory: {path}")
            validate_root(path, protected)
        return cls(project, config, workflow, values, roots, protected)


def storage_targets(root: Path) -> list[Path]:
    if not root.exists():
        return []
    preserved = {
        "prompt_admin",
        "entity_merge_settings.sqlite3",
        "entity_merge_settings.sqlite3-wal",
        "entity_merge_settings.sqlite3-shm",
    }
    return [path for path in sorted(root.iterdir()) if path.name not in preserved]


def file_plan(deployment: Deployment) -> dict:
    targets = []
    preserved = [str(deployment.config), str(deployment.project / ".env")]
    sql = []
    for kind, root in deployment.roots.items():
        if not root.exists():
            continue
        if kind in {"storage", "legacy_storage"}:
            targets.extend(storage_targets(root))
            preserved.extend(
                [
                    str(root / "prompt_admin"),
                    str(root / "entity_merge_settings.sqlite3"),
                ]
            )
            database = root / "entity_merge_settings.sqlite3"
            if database.exists():
                with sqlite3.connect(
                    f"file:{database}?mode=ro", uri=True
                ) as connection:
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if "enterprise_aliases" in tables:
                        count = connection.execute(
                            "SELECT count(*) FROM enterprise_aliases"
                        ).fetchone()[0]
                        sql.append(
                            {
                                "path": str(database),
                                "table": "enterprise_aliases",
                                "rows": count,
                            }
                        )
        else:
            targets.extend(sorted(root.iterdir()))
    # Only recognized application log files, never delete the LOG_DIR itself.
    log_root = absolute(
        deployment.environment.get("LOG_DIR") or ".", deployment.project
    )
    for name in ("lightrag.log", "lightrag-server.log"):
        for root in {log_root, deployment.project}:
            targets.extend(root.glob(name + "*"))
    unique = sorted(set(targets), key=lambda p: (len(p.parts), str(p)))
    selected = []
    for path in unique:
        # An external symlink inside a data directory is unlinked, never followed.
        if any(path.is_relative_to(parent) for parent in selected):
            continue
        if any(overlaps(path, protected) for protected in deployment.protected):
            raise ResetError(
                f"Deletion target overlaps preserved configuration/source: {path}"
            )
        selected.append(path)
    return {
        "delete_paths": [str(path) for path in selected],
        "clear_sql_tables": sql,
        "preserved": sorted(set(preserved)),
    }


class Databases:
    def __init__(self, values: dict):
        from neo4j import GraphDatabase
        from pymilvus import MilvusClient

        self.name = values["NEO4J_DATABASE"]
        self.milvus_name = values["MILVUS_DB_NAME"]
        self.graph = GraphDatabase.driver(
            values["NEO4J_URI"],
            auth=(values["NEO4J_USERNAME"], values["NEO4J_PASSWORD"]),
            connection_timeout=10,
        )
        try:
            options = {
                "uri": values["MILVUS_URI"],
                "db_name": self.milvus_name,
                "timeout": 30,
            }
            options.update(
                {
                    key: values[source]
                    for key, source in (
                        ("user", "MILVUS_USER"),
                        ("password", "MILVUS_PASSWORD"),
                        ("token", "MILVUS_TOKEN"),
                    )
                    if values.get(source)
                }
            )
            self.vectors = MilvusClient(**options)
        except BaseException:
            self.graph.close()
            raise

    def inventory(self, deployment: Deployment) -> dict:
        workspaces = {deployment.environment.get("WORKSPACE") or "default"}
        workspaces.update(
            value
            for key in ("NEO4J_WORKSPACE", "MILVUS_WORKSPACE")
            if (value := deployment.environment.get(key))
        )
        collections = []
        for name in sorted(self.vectors.list_collections()):
            info = self.vectors.describe_collection(name)
            namespace = MILVUS_NAMESPACES.get(info.get("description"))
            if namespace is None or "_" + namespace not in name:
                raise ResetError(
                    f"Unrecognized Milvus collection; refusing to delete it: {name}"
                )
            workspaces.add(name.rsplit("_" + namespace, 1)[0])
            collections.append(
                {
                    "name": name,
                    "rows": int(
                        self.vectors.get_collection_stats(name).get("row_count", 0)
                    ),
                }
            )
        for kind in ("storage", "legacy_storage", "inputs", "legacy_inputs"):
            root = deployment.roots.get(kind)
            if root and root.exists():
                workspaces.update(
                    path.name
                    for path in root.iterdir()
                    if path.is_dir()
                    and not path.is_symlink()
                    and path.name not in {"prompt_admin", "scan_spool"}
                )
        with self.graph.session(database=self.name) as session:
            rows = session.run(
                "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS nodes ORDER BY label"
            ).data()
            unknown = session.run(
                "MATCH (n) WHERE NOT any(label IN labels(n) WHERE label IN $workspaces) RETURN count(n) AS n",
                workspaces=sorted(workspaces),
            ).single()["n"]
            if unknown:
                raise ResetError(
                    f"Neo4j contains {unknown} nodes outside recognized LightRAG workspaces; refusing a database-wide reset"
                )
            counts = session.run("MATCH (n) RETURN count(n) AS nodes").single()
            edges = session.run(
                "MATCH ()-[r]->() RETURN count(r) AS relationships"
            ).single()["relationships"]
        return {
            "neo4j_database": self.name,
            "milvus_database": self.milvus_name,
            "workspaces": sorted(workspaces),
            "neo4j_labels": rows,
            "neo4j_nodes": counts["nodes"],
            "neo4j_relationships": edges,
            "milvus_collections": collections,
        }

    def clear_graph(self, inventory: dict) -> None:
        with self.graph.session(database=self.name) as session:
            # Bounded autocommit batches; an uncertain failure is reported, not hidden.
            for _ in range(inventory["neo4j_nodes"] // 1000 + 2):
                count = session.run(
                    "MATCH (n) WHERE any(label IN labels(n) WHERE label IN $workspaces) WITH n LIMIT 1000 DETACH DELETE n RETURN count(*) AS deleted",
                    workspaces=inventory["workspaces"],
                ).single()["deleted"]
                if count == 0:
                    return
            raise ResetError(
                "Neo4j kept changing during deletion; stop every external writer before retrying"
            )

    def clear_vectors(self, inventory: dict) -> None:
        for collection in inventory["milvus_collections"]:
            self.vectors.drop_collection(collection["name"], timeout=60)

    def verify_empty(self) -> None:
        with self.graph.session(database=self.name) as session:
            if session.run("MATCH (n) RETURN count(n) AS n").single()["n"]:
                raise ResetError("Neo4j is not empty after reset")
        if self.vectors.list_collections():
            raise ResetError("Milvus is not empty after reset")

    def close(self):
        self.vectors.close()
        self.graph.close()


def processes(deployment: Deployment) -> list[dict]:
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            cwd = (entry / "cwd").resolve(strict=True)
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            role = None
            belongs = (
                cwd.is_relative_to(deployment.project)
                or cwd.is_relative_to(deployment.workflow_project)
                or any(arg.startswith(str(deployment.project) + "/") for arg in args)
            )
            if belongs:
                if any(
                    Path(arg).name in {"lightrag-server", "lightrag-gunicorn"}
                    or arg.startswith("lightrag.api.lightrag_server")
                    for arg in args
                ):
                    role = "LightRAG server"
                elif "lightrag.integrations.data_workflow.worker" in args:
                    role = "distillation worker"
                elif "run" in args and (
                    "data_studio" in args
                    or any(Path(arg).name == "data-studio" for arg in args)
                ):
                    role = "data-work-flow CLI"
            found[int(entry.name)] = {
                "pid": int(entry.name),
                "ppid": int(stat[1]),
                "start": stat[19],
                "role": role,
            }
        except (OSError, ValueError, IndexError):
            continue
    selected = {pid for pid, row in found.items() if row["role"]}
    while True:
        expanded = selected | {
            pid for pid, row in found.items() if row["ppid"] in selected
        }
        if expanded == selected:
            break
        selected = expanded
    return [
        {**found[pid], "role": found[pid]["role"] or "owned child process"}
        for pid in sorted(selected)
    ]


def same_process(row: dict) -> bool:
    try:
        fields = (
            (Path("/proc") / str(row["pid"]) / "stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()
        )
        return fields[19] == row["start"] and fields[0] != "Z"
    except (OSError, IndexError):
        return False


def stop_writers(deployment: Deployment, timeout: int) -> None:
    rows = processes(deployment)
    for row in rows:
        if same_process(row):
            os.kill(row["pid"], signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while any(same_process(row) for row in rows):
        if time.monotonic() >= deadline:
            raise ResetError(
                "Some writers did not stop; no data deletion started. Stop them manually and retry"
            )
        time.sleep(0.2)
    if any(same_process(row) for row in processes(deployment)):
        raise ResetError(
            "A service restarted automatically. Stop its supervisor before retrying"
        )


def clear_files(plan: dict) -> None:
    for operation in plan["clear_sql_tables"]:
        if operation["table"] != "enterprise_aliases":
            raise ResetError("Unexpected SQLite deletion table")
        with sqlite3.connect(operation["path"]) as connection:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("DELETE FROM enterprise_aliases")
            connection.commit()
            connection.execute("VACUUM")
    for value in plan["delete_paths"]:
        path = Path(value)
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def archive_task_configs(deployment: Deployment, audit: Path) -> None:
    # Preserve small old job configurations, not datasets or model responses.
    root = deployment.roots["distillation_jobs"]
    if root.exists():
        for namespace in root.iterdir():
            if not namespace.is_dir() or namespace.is_symlink():
                continue
            for job in namespace.glob("distill-*"):
                if not job.is_dir() or job.is_symlink():
                    continue
                for name in ("job.yaml", "settings.json"):
                    source = job / name
                    if source.is_file() and not source.is_symlink():
                        destination = (
                            audit.parent
                            / "task-configs"
                            / namespace.name
                            / job.name
                            / name
                        )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, destination)
                        destination.chmod(0o600)


def write_audit(path: Path, report: dict) -> None:
    temporary = path.with_suffix(".part")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear ALL LightRAG workspaces in the configured databases. Default: preview only."
    )
    parser.add_argument("--env-file", type=Path, default=PROJECT / ".env")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Stop owned writers and permanently delete the previewed application data",
    )
    parser.add_argument(
        "--confirm", default="", help=f"Required literal: {CONFIRMATION}"
    )
    parser.add_argument("--stop-timeout", type=int, default=45)
    parser.add_argument(
        "--include-standalone-workflow-runs",
        action="store_true",
        help="Also clear standalone runs/ in the configured data-work-flow project",
    )
    args = parser.parse_args(argv)
    if args.execute and args.confirm != CONFIRMATION:
        parser.error(f"--execute requires --confirm {CONFIRMATION}")
    if args.stop_timeout < 1:
        parser.error("--stop-timeout must be positive")
    databases = None
    audit = None
    report = {}
    try:
        deployment = Deployment.load(
            PROJECT, args.env_file.resolve(), args.include_standalone_workflow_runs
        )
        databases = Databases(deployment.environment)
        report = {
            "mode": "execute" if args.execute else "preview",
            "status": "planned",
            "environment_file": str(args.env_file.resolve()),
            "connections": {
                key: public_endpoint(deployment.environment[key])
                for key in ("NEO4J_URI", "MILVUS_URI")
                if deployment.environment.get(key)
            },
            "databases": databases.inventory(deployment),
            "files": file_plan(deployment),
            "processes_to_stop": processes(deployment),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not args.execute:
            print(f"\nPREVIEW ONLY. Execute with --execute --confirm {CONFIRMATION}")
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        audit = deployment.config / "history" / ("reset-data-" + stamp) / "report.json"
        audit.parent.mkdir(parents=True, exist_ok=False)
        write_audit(audit, report)
        stop_writers(deployment, args.stop_timeout)
        report["status"] = "writers_stopped"
        # Re-read after graceful shutdown, which can flush queued writes.
        report["databases"] = databases.inventory(deployment)
        report["files"] = file_plan(deployment)
        archive_task_configs(deployment, audit)
        write_audit(audit, report)
        databases.clear_graph(report["databases"])
        report["status"] = "graph_cleared"
        write_audit(audit, report)
        databases.clear_vectors(report["databases"])
        report["status"] = "vectors_cleared"
        write_audit(audit, report)
        if processes(deployment):
            raise ResetError("A writer restarted; refusing to remove local files")
        clear_files(report["files"])
        databases.verify_empty()
        remaining = file_plan(deployment)
        if remaining["delete_paths"] or any(
            row["rows"] for row in remaining["clear_sql_tables"]
        ):
            raise ResetError("Some local application data remains")
        report["status"] = "completed"
        write_audit(audit, report)
        print(f"\nCompleted. Services remain stopped. Audit: {audit}")
        return 0
    except Exception as error:
        # SDK exception messages may contain connection credentials; do not echo them.
        message = (
            str(error)
            if isinstance(error, ResetError)
            else f"{type(error).__name__}; inspect the failing service without publishing credentials"
        )
        print(f"Reset stopped: {message}", file=sys.stderr)
        if audit:
            report.update(
                last_completed_phase=report.get("status"),
                status="failed",
                error=message,
            )
            write_audit(audit, report)
            print(f"Partial progress recorded in {audit}", file=sys.stderr)
        return 1
    finally:
        if databases is not None:
            databases.close()


if __name__ == "__main__":
    raise SystemExit(main())
