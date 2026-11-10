"""Start a separate batch manager without opening the existing RAG workspace."""

import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ui-directory", type=Path)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    # API authentication shares the existing config parser, not these CLI flags.
    sys.argv = ["lightrag-server"]
    from lightrag.api.config import initialize_config

    initialize_config(force=True)
    from .app import create_manager_app
    import uvicorn

    app = create_manager_app(arguments.deployment, ui_directory=arguments.ui_directory)
    uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
