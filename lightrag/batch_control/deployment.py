"""Opt-in deployment wiring; authenticated principals use explicit administrator grants."""

from __future__ import annotations

import asyncio
from fnmatch import fnmatchcase
from pathlib import Path

from pydantic import Field

from lightrag.integrations.data_workflow.config import read_yaml

from .chain_service import BatchDispatcher, ChainService
from .adoption import AdoptionService, LegacySource
from .applications import ApplicationCoordinator, ApplicationService
from .contracts import Configuration, Contract, Environment, Name
from .paths import ControlPaths
from .references import Catalog
from .runtime_config import RuntimePolicy
from .service import BatchControl
from .training_export.service import ExportLimits, ExportStore, TrainingExports
from .index_rebuild.coordinator import RebuildCoordinator
from .index_rebuild.service import IndexRebuilds


class WorkflowEnvironment(Contract):
    project_directory: str
    python: str
    code_revision: str
    dependency_revision: str


class Grant(Contract):
    actor: Name
    actions: list[str] = Field(min_length=1)
    resources: list[str] = Field(min_length=1)


class DeploymentSettings(Contract):
    version: int = Field(default=1, ge=1, le=1)
    config_root: str
    runtime_root: str
    recipe_file: str
    rag: dict
    retrieval: dict = Field(default_factory=dict)
    bridge: dict
    runtime: dict
    environments: dict[Name, WorkflowEnvironment]
    credentials: dict[Name, str] = Field(default_factory=dict)
    inputs: dict[Name, list[str]] = Field(default_factory=dict)
    profiles: dict[Name, dict]
    grants: list[Grant] = Field(min_length=1)
    ui_directory: str | None = None
    legacy_sources: dict[Name, LegacySource] = Field(default_factory=dict)
    provider_limits: dict = Field(default_factory=dict)
    training_export_limits: ExportLimits = Field(default_factory=ExportLimits)


class BatchDeployment:
    def __init__(self, control, policy):
        self.control, self.policy = control, policy
        self.dispatcher = BatchDispatcher(control, policy)
        self.background = None
        self.application_coordinator = ApplicationCoordinator(
            control, policy, backend=self.dispatcher.backend
        )
        self.application_background = None
        self.rebuild_coordinator = RebuildCoordinator(
            control, policy, backend=self.dispatcher.backend
        )
        self.rebuild_background = None
        self.ui_directory = None
        self.legacy_sources = {}
        self.export_background = {}

    def training_exports(self, actor):
        return TrainingExports(self.control, actor=actor)

    def index_rebuilds(self, actor):
        return IndexRebuilds(
            self.control, self.policy, actor=actor, backend=self.dispatcher.backend
        )

    def launch_export(self, record):
        identity = record["export_id"]
        if identity in self.export_background:
            return

        async def run():
            service = self.training_exports(record["actor"])
            try:
                await asyncio.to_thread(
                    service.run, record["data_batch_id"], record["task_id"], identity
                )
            except Exception:
                from lightrag.utils import logger

                logger.exception("Training export could not start: %s", identity)
                service.store.update(
                    record["data_batch_id"],
                    record["task_id"],
                    identity,
                    state="failed",
                    error="Export could not start; inspect server logs",
                )
            finally:
                self.export_background.pop(identity, None)

        self.export_background[identity] = asyncio.create_task(run())

    def adoption(self, actor):
        return AdoptionService(
            self.control,
            self.policy,
            self.legacy_sources,
            actor=actor,
            backend=self.dispatcher.backend,
        )

    def service(self, actor):
        return ChainService(
            self.control, self.policy, actor=actor, backend=self.dispatcher.backend
        )

    async def start(self):
        if self.background is None:
            from .provider_quota.core import QuotaStore
            from .provider_quota.wiring import registry_path

            QuotaStore(registry_path(self.control.paths)).configure(
                self.control.provider_limits
            )
            self.background = asyncio.create_task(self.dispatcher.run())
            self.application_background = asyncio.create_task(
                self.application_coordinator.run()
            )
            self.rebuild_background = asyncio.create_task(
                self.rebuild_coordinator.run()
            )
            for record in ExportStore(self.control.registry).list(queued=True):
                self.launch_export(record)

    async def close(self):
        await self.dispatcher.close()
        self.application_coordinator.stop_event.set()
        self.rebuild_coordinator.stop_event.set()
        if self.background is not None:
            await self.background
            self.background = None
        if self.application_background is not None:
            await self.application_background
            self.application_background = None
        if self.rebuild_background is not None:
            await self.rebuild_background
            self.rebuild_background = None
        if self.export_background:
            await asyncio.gather(*list(self.export_background.values()))

    def application(self, actor):
        return ApplicationService(
            self.control, self.policy, actor=actor, backend=self.dispatcher.backend
        )


def load_deployment(path: Path):
    settings = DeploymentSettings.model_validate(read_yaml(path))

    def absolute(value):
        # Keep the virtual-environment interpreter symlink rather than resolving it.
        import os

        return Path(os.path.abspath(path.parent / Path(value).expanduser()))

    recipe_file = absolute(settings.recipe_file)
    recipe = read_yaml(recipe_file)

    def rebase(value):
        if isinstance(value, dict):
            return {
                key: str(recipe_file.parent / Path(item).expanduser())
                if key in {"prompt_file", "topic_catalog_file"}
                and isinstance(item, str)
                and item
                else rebase(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rebase(item) for item in value]
        return value

    defaults = Configuration.model_validate(
        {
            "rag": settings.rag,
            "retrieval": settings.retrieval,
            "bridge": settings.bridge,
            "recipe": rebase(recipe),
        }
    )
    catalog = Catalog(
        environments={
            key: Environment(
                str(absolute(env.project_directory)),
                str(absolute(env.python)),
                env.code_revision,
                env.dependency_revision,
            )
            for key, env in settings.environments.items()
        },
        credentials={
            key: absolute(value) for key, value in settings.credentials.items()
        },
        inputs={
            key: [absolute(value) for value in values]
            for key, values in settings.inputs.items()
        },
        profiles=settings.profiles,
    )
    from .bindings import BindingStore
    from .storage import Registry

    registry_path = ControlPaths(
        absolute(settings.config_root), absolute(settings.runtime_root)
    ).registry
    if registry_path.is_file():
        for name, profile in BindingStore(Registry(registry_path)).profiles().items():
            if name in catalog.profiles and catalog.profiles[name] != profile:
                raise ValueError("Adopted profile conflicts with a deployment profile")
            catalog.profiles[name] = profile
    runtime = dict(settings.runtime)
    for key in ("project_directory", "python", "storage_credentials_file"):
        if runtime.get(key) is not None:
            runtime[key] = absolute(runtime[key])
    policy = RuntimePolicy(**runtime)

    def authorize(actor, action, resource):
        return any(
            grant.actor == actor
            and any(fnmatchcase(action, pattern) for pattern in grant.actions)
            and any(fnmatchcase(resource, pattern) for pattern in grant.resources)
            for grant in settings.grants
        )

    control = BatchControl(
        ControlPaths(absolute(settings.config_root), absolute(settings.runtime_root)),
        defaults,
        catalog,
        authorize,
    )
    deployment = BatchDeployment(control, policy)
    from .provider_quota.core import configuration

    control.provider_limits = configuration(settings.provider_limits)
    control.training_export_limits = settings.training_export_limits
    deployment.legacy_sources = settings.legacy_sources
    deployment.ui_directory = (
        absolute(settings.ui_directory) if settings.ui_directory else None
    )
    return deployment
