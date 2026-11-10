"""Read-only integrity checks for an explicitly selected test-service candidate."""

import hashlib
import json
from pathlib import Path
import re


def verify_candidate(project: Path, release: Path, *, manager_only=False) -> Path:
    project, release = project.resolve(), release.resolve()
    record = json.loads((release / "release.json").read_text())
    if (
        record.get("schema_version") != 1
        or record.get("kind") != "local_wheel_release_validation"
        or not record.get("cli_help_verified")
    ):
        raise RuntimeError("Candidate has no accepted local release receipt")
    smoke = record.get("installed_smoke", {})
    if (
        smoke.get("authenticated_api_status") != 200
        or smoke.get("html_status") != 200
        or smoke.get("manager_has_default_rag") is not False
        or not smoke.get("coordinators_closed")
    ):
        raise RuntimeError("Candidate has no successful isolated manager smoke")
    if manager_only and (
        smoke.get("workbench_html_status") != 200
        or smoke.get("manager_health_has_no_default_workspace") is not True
        or smoke.get("legacy_workspace_html_unavailable") is not True
    ):
        raise RuntimeError("Candidate has no accepted full batch workbench")
    installed = release / "installed"
    if installed.resolve() != installed:
        raise RuntimeError("Candidate installation must not be redirected")
    wheel = Path(record["wheel"])
    if (
        not wheel.is_absolute()
        or wheel.resolve() != wheel
        or not wheel.is_relative_to(release / "dist")
        or hashlib.sha256(wheel.read_bytes()).hexdigest() != record["wheel_sha256"]
    ):
        raise RuntimeError("Accepted wheel changed or left the release directory")
    files = record.get("verified_package_files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("Candidate has no verified package manifest")
    for name, expected in files.items():
        path = Path(name)
        if (
            not path.parts
            or path.parts[0] != "lightrag"
            or any(part in {".", ".."} for part in path.parts)
            or not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or not (path.suffix == ".py" or path.is_relative_to("lightrag/api/webui"))
        ):
            raise RuntimeError("Candidate has an invalid package manifest entry")
        candidate = installed / path
        if (
            candidate.resolve() != candidate
            or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected
        ):
            raise RuntimeError("Accepted candidate changed: " + name)
        if path.suffix == ".py":
            source = project / path
            if (
                not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest() != expected
            ):
                raise RuntimeError(
                    "Source changed since acceptance; rebuild candidate: " + name
                )
    expected_python = {name for name in files if name.endswith(".py")}
    for root in (project, installed):
        actual = {
            str(path.relative_to(root)) for path in (root / "lightrag").rglob("*.py")
        }
        if actual != expected_python:
            raise RuntimeError("Python source inventory changed since acceptance")
    actual_ui = {
        str(path.relative_to(installed))
        for path in (installed / "lightrag/api/webui").rglob("*")
        if path.is_file()
    }
    if actual_ui != {name for name in files if name.startswith("lightrag/api/webui/")}:
        raise RuntimeError("WebUI inventory changed since acceptance")
    return installed
