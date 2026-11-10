"""Build and check a local wheel in fresh staging; never publish or replace live assets."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def clean_environment():
    return {
        **{
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL")
            if key in os.environ
        },
        "PYTHONDONTWRITEBYTECODE": "1",
        "EMBEDDING_BINDING_HOST": "",
        "ENTITY_TYPE_PROMPT_FILE": "",
        "ENTERPRISE_KNOWLEDGE_ENABLED": "false",
    }


def smoke_installed(installed, directory):
    # -I and an unrelated cwd exclude this checkout and editable-install shortcuts.
    sys.path.insert(0, str(installed))
    sys.argv = ["lightrag-server"]
    import httpx
    import yaml
    import lightrag.batch_control.app as manager
    from lightrag.batch_control.deployment import DeploymentSettings

    origin = Path(manager.__file__).resolve()
    if not origin.is_relative_to(installed):
        raise RuntimeError("Smoke imported the source checkout instead of the wheel")
    directory.mkdir(mode=0o700)
    recipe = directory / "recipe.yaml"
    recipe.write_text(
        yaml.safe_dump(
            {
                "schema_version": 3,
                "models": {},
                "workflow": {"name": "difficulty_routed_qa", "parameters": {}},
            }
        )
    )
    config = {
        "version": 1,
        "config_root": str(directory / "config"),
        "runtime_root": str(directory / "runtime"),
        "recipe_file": str(recipe),
        "rag": {"embedding_profile": "embedding"},
        "retrieval": {},
        "bridge": {"environment_id": "offline"},
        "runtime": {
            "project_directory": str(installed),
            "python": sys.executable,
            "storage_mode": "local",
        },
        "environments": {
            "offline": {
                "project_directory": str(directory),
                "python": sys.executable,
                "code_revision": "smoke-only",
                "dependency_revision": "smoke-only",
            }
        },
        "profiles": {"embedding": {"model_name": "offline", "dimensions": 8}},
        "grants": [{"actor": "api-key", "actions": ["read"], "resources": ["*"]}],
    }
    DeploymentSettings.model_validate(config)
    deployment = directory / "deployment.yaml"
    deployment.write_text(yaml.safe_dump(config))
    app = manager.create_manager_app(deployment, api_key="local-release-smoke")
    app.root_path = "/release"
    assert not hasattr(app.state, "rag")

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test/release"
            ) as client:
                unauthorized = await client.get("/data-batches")
                assert unauthorized.status_code in {401, 403}
                authorized = await client.get(
                    "/data-batches", headers={"X-API-Key": "local-release-smoke"}
                )
                assert authorized.status_code == 200 and authorized.json() == {
                    "items": []
                }
                response = await client.get("/batch-settings/")
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert '"apiPrefix": "/release"' in response.text
                assets = re.findall(
                    r'(?:src|href)="([^\"]+\.(?:js|css))"', response.text
                )
                assert len(assets) >= 2
                for asset in assets:
                    result = await client.get(
                        "/batch-settings/" + asset.removeprefix("./")
                    )
                    assert result.status_code == 200, asset
                workbench = await client.get("/webui/")
                assert workbench.status_code == 200
                assert workbench.headers["cache-control"] == "no-store"
                assert '"apiPrefix": "/release"' in workbench.text
                assert '"batchOnly": true' in workbench.text
                workbench_assets = re.findall(
                    r'(?:src|href)="([^\"]+\.(?:js|css))"', workbench.text
                )
                assert len(workbench_assets) >= 2
                for asset in workbench_assets:
                    result = await client.get("/webui/" + asset.removeprefix("./"))
                    assert result.status_code == 200, asset
                health = await client.get("/health")
                assert health.status_code == 200 and health.json()["batch_only"]
                assert health.json()["workspace_available"] is False
                assert (await client.get("/webui/workspace.html")).status_code == 404
        assert app.state.batch_deployment.background is None
        assert app.state.batch_deployment.application_background is None
        assert app.state.batch_deployment.rebuild_background is None
        assert not app.state.batch_deployment.export_background
        return {
            "module_origin": str(origin),
            "manager_has_default_rag": False,
            "authenticated_api_status": 200,
            "unauthenticated_api_status": unauthorized.status_code,
            "html_status": 200,
            "root_path": "/release",
            "assets_checked": len(assets),
            "workbench_html_status": workbench.status_code,
            "workbench_assets_checked": len(workbench_assets),
            "manager_health_has_no_default_workspace": True,
            "legacy_workspace_html_unavailable": True,
            "coordinators_closed": True,
            "external_model_requests": 0,
        }

    # Keep the sandbox workaround through asyncio.run's executor shutdown too.
    import selectors

    original = selectors.EpollSelector.select
    selectors.EpollSelector.select = lambda self, timeout=None: original(
        self, 0.01 if timeout is None else min(timeout, 0.01)
    )
    try:
        result = asyncio.run(check())
    finally:
        selectors.EpollSelector.select = original
    (directory / "smoke.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


def validate(source, webui, output):
    if (
        not (webui / "batch-settings.html").is_file()
        or not (webui / "index.html").is_file()
    ):
        raise ValueError(
            "Build both admin and batch-settings entries before validation"
        )
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    stage = output / "source"
    stage.mkdir(mode=0o700)
    for name in (
        "pyproject.toml",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
    ):
        shutil.copyfile(source / name, stage / name)
    # Older manifests mention requirements files absent from current checkouts.
    for name in ("requirements.txt",):
        if (source / name).is_file():
            shutil.copyfile(source / name, stage / name)
    shutil.copytree(source / "prompts/samples", stage / "prompts/samples")
    shutil.copytree(
        source / "lightrag",
        stage / "lightrag",
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.log", "webui", ".env*"
        ),
    )
    shutil.copytree(webui, stage / "lightrag/api/webui")
    log = output / "build.log"
    with log.open("w") as stream:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; from setuptools.build_meta import build_wheel; "
                "os.chdir(sys.argv[2]); build_wheel(sys.argv[1])",
                str(output / "dist"),
                str(stage),
            ],
            cwd=output,
            env=clean_environment(),
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=180,
        )
    wheels = list((output / "dist").glob("*.whl"))
    assert len(wheels) == 1
    expected = {
        str(path.relative_to(stage)): sha256(path.read_bytes())
        for path in (stage / "lightrag").rglob("*.py")
    }
    expected.update(
        {
            str(path.relative_to(stage)): sha256(path.read_bytes())
            for path in (stage / "lightrag/api/webui").rglob("*")
            if path.is_file()
        }
    )
    installed = output / "installed"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
        for name, expected_hash in expected.items():
            assert name in names and sha256(wheel.read(name)) == expected_hash, name
        assert not any(
            "/tests/" in name
            or Path(name).name.startswith(".env")
            or "batch-control/control.sqlite3" in name
            for name in names
        )
        wheel.extractall(installed)
    with (output / "smoke.log").open("w") as stream:
        subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--smoke-installed",
                str(installed),
                "--output-dir",
                str(output / "smoke"),
            ],
            cwd=output,
            env=clean_environment(),
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=30,
        )
    cli = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); sys.argv=['batch-control','--help']; runpy.run_module('lightrag.batch_control',run_name='__main__')",
            str(installed),
        ],
        cwd=output,
        env=clean_environment(),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert "--deployment" in cli.stdout and "--ui-directory" in cli.stdout
    (output / "cli-help.txt").write_text(cli.stdout)
    record = {
        "schema_version": 1,
        "kind": "local_wheel_release_validation",
        "wheel": str(wheels[0]),
        "wheel_sha256": sha256(wheels[0].read_bytes()),
        "wheel_bytes": wheels[0].stat().st_size,
        "verified_package_files": expected,
        "installed_smoke": json.loads((output / "smoke/smoke.json").read_text()),
        "cli_help_verified": True,
        "published": False,
        "existing_services_restarted": False,
        "dependency_resolution_tested": False,
    }
    (output / "release.json").write_text(json.dumps(record, indent=2) + "\n")
    for path in output.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
        elif path.is_dir():
            path.chmod(0o700)
    print(
        json.dumps(
            {
                key: value
                for key, value in record.items()
                if key != "verified_package_files"
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webui-directory", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--smoke-installed", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.smoke_installed:
        smoke_installed(args.smoke_installed.resolve(), args.output_dir.resolve())
    elif args.webui_directory:
        validate(
            Path(__file__).resolve().parents[2],
            args.webui_directory.resolve(),
            args.output_dir.resolve(),
        )
    else:
        parser.error("--webui-directory is required")


if __name__ == "__main__":
    main()
