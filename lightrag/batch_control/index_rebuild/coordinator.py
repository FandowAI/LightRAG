"""Observe durable rebuilds independently across batches; join steps on shutdown."""

import asyncio

from ..runtime_process import ProcessBackend
from ..storage import Conflict
from .service import IndexRebuilds
from .store import RebuildStore


class RebuildCoordinator:
    def __init__(self, control, policy, *, backend=None):
        self.control, self.policy = control, policy
        self.backend = backend or ProcessBackend()
        self.store = RebuildStore(control.registry)
        self.stop_event = asyncio.Event()
        self.running = {}

    async def advance(self, record):
        batch = record["data_batch_id"]
        try:
            service = IndexRebuilds(
                self.control,
                self.policy,
                actor=record.get("executor_actor", record["actor"]),
                backend=self.backend,
            )
            await asyncio.to_thread(service.step, batch, record["operation_id"])
        except Conflict:
            pass  # Another manager owns the file lock; its durable phase remains authoritative.
        except Exception:
            from lightrag.utils import logger

            logger.exception(
                "Index rebuild coordinator step failed: %s", record["operation_id"]
            )
        finally:
            self.running.pop(batch, None)

    async def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    for record in await asyncio.to_thread(self.store.pending):
                        batch = record["data_batch_id"]
                        if batch not in self.running:
                            self.running[batch] = asyncio.create_task(
                                self.advance(record)
                            )
                except Exception:
                    from lightrag.utils import logger

                    logger.exception("Index rebuild coordinator inventory failed")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        finally:
            # A cancelled to_thread caller does not cancel its operating thread.
            # Join admitted steps before the manager can close its registry.
            await asyncio.gather(*list(self.running.values()), return_exceptions=True)
