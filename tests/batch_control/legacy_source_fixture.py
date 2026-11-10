"""A test-owned legacy ASGI process; requests use private files, never a live port."""

import asyncio
import json
from pathlib import Path
import selectors
import signal
import sys

from httpx import ASGITransport, AsyncClient

from lightrag.batch_control.runtime_process import atomic_json


async def serve(directory):
    sys.argv = ["lightrag-server"]
    from lightrag.api.config import initialize_config
    from lightrag.api.lightrag_server import create_app

    app = create_app(initialize_config(force=True))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app), base_url="http://fixture"
        ) as client:
            atomic_json(directory / "ready.json", {"state": "ready"})
            while not stop.is_set():
                path = directory / "request.json"
                if path.exists():
                    request = json.loads(path.read_text())
                    path.unlink()
                    response = await client.request(
                        request["method"],
                        request["path"],
                        headers=request["headers"],
                        json=request.get("body"),
                    )
                    atomic_json(
                        directory / "response.json",
                        {"status": response.status_code, "body": response.text},
                    )
                await asyncio.sleep(0.01)
    atomic_json(directory / "stopped.json", {"state": "stopped"})


if __name__ == "__main__":
    original = selectors.EpollSelector.select
    selectors.EpollSelector.select = lambda self, timeout=None: original(
        self, 0.01 if timeout is None else min(timeout, 0.01)
    )
    asyncio.run(serve(Path(sys.argv[1])))
