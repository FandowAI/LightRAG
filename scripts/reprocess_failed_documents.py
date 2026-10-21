"""Ask the local LightRAG server to retry failed documents once."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.request import Request, urlopen

from dotenv import load_dotenv


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)
    api_key = os.environ["LIGHTRAG_API_KEY"]
    port = os.getenv("PORT", "9621")
    request = Request(
        f"http://127.0.0.1:{port}/documents/reprocess_failed",
        method="POST",
        headers={"X-API-Key": api_key},
    )
    with urlopen(request, timeout=30) as response:
        print(response.status, response.read().decode())


if __name__ == "__main__":
    main()
