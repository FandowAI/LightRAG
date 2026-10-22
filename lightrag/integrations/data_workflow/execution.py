"""Run the unmodified data_studio CLI with a job-local immutable recipe."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from .config import BridgeSettings, read_yaml


def execute(settings: BridgeSettings, directory: Path) -> dict:
    """Accept completion only when the CLI's receipt agrees with its exit status."""
    if (
        not settings.project_directory.is_dir()
        or not (settings.project_directory / "src/data_studio").is_dir()
    ):
        raise ValueError("project_directory must contain src/data_studio")
    if not settings.python.is_file() or not os.access(settings.python, os.X_OK):
        raise ValueError("Configure an executable data-work-flow Python interpreter")
    recipe = read_yaml(directory / "job.yaml")
    for model in recipe.get("models", {}).values():
        if "example.com" in model.get("api_url", "") or model.get(
            "model_name", ""
        ).startswith("replace-with-"):
            raise ValueError(
                "Configure real model endpoints and names in workflow_config before starting"
            )
    command = [
        str(settings.python),
        "-m",
        "data_studio",
        "run",
        "--config",
        str(directory / "job.yaml"),
    ]
    if settings.credentials_file is not None:
        if not settings.credentials_file.is_file():
            raise ValueError("Configured credentials_file does not exist")
        command.extend(["--env-file", str(settings.credentials_file)])
    environment = dict(os.environ)
    # Import the selected checkout, never an unrelated installed copy or LightRAG's path.
    environment["PYTHONPATH"] = str(settings.project_directory / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with (
        (directory / "stdout.log").open("wb") as stdout,
        (directory / "stderr.log").open("wb") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=settings.project_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=settings.limits.timeout_seconds)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
    with (directory / "stdout.log").open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 131_072))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            receipt = json.loads(line)
        except ValueError:
            continue
        if (
            not isinstance(receipt, dict)
            or not {"run_id", "directory", "status", "summary"} <= receipt.keys()
        ):
            continue
        expected = {"completed": 0, "completed_with_errors": 1, "failed": 2}
        if code != expected.get(receipt["status"]) or not isinstance(
            receipt["summary"], dict
        ):
            break
        output = Path(receipt["directory"]).resolve()
        if (
            output.parent != (directory / "outputs").resolve()
            or output.name != receipt["run_id"]
        ):
            break
        manifest = json.loads((output / "run.json").read_text(encoding="utf-8"))
        if (
            manifest.get("run_id") != receipt["run_id"]
            or manifest.get("status") != receipt["status"]
        ):
            break
        return receipt
    raise RuntimeError(
        f"data-work-flow exited with code {code} without a valid terminal receipt; inspect stderr.log"
    )
