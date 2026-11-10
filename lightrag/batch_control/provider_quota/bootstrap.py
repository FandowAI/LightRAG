"""Install admission, then invoke the selected unmodified data_studio CLI."""

import json
from pathlib import Path
import runpy
import sys


def main():
    if (
        len(sys.argv) < 6
        or sys.argv[1] != "--spec"
        or sys.argv[3:6] != ["--", "-m", "data_studio"]
    ):
        raise ValueError("The provider launcher only runs the data_studio module")
    spec = json.loads(Path(sys.argv[2]).read_text())
    from hooks import install_requests

    install_requests(spec)
    sys.argv = ["data_studio", *sys.argv[6:]]
    runpy.run_module("data_studio", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
