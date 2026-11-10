"""Run or resume an administrator-declared transfer; retain all evidence on abort."""

import argparse
import json
from pathlib import Path
import time

from .deployment import load_deployment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True, type=Path)
    parser.add_argument("--actor", default="api-key")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--source")
    action.add_argument("--resume", metavar="OPERATION")
    action.add_argument("--status", metavar="OPERATION")
    action.add_argument("--abort", metavar="OPERATION")
    action.add_argument("--retry-stop", metavar="OPERATION")
    parser.add_argument("--name")
    parser.add_argument("--request-id")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.source and (not args.name or not args.request_id):
        parser.error("--source requires --name and --request-id")
    service = load_deployment(args.deployment.resolve()).adoption(args.actor)
    if args.status:
        print(json.dumps(service.public(args.status), ensure_ascii=False))
        return 0
    if args.abort:
        service.abort(args.abort)
        print(json.dumps(service.public(args.abort), ensure_ascii=False))
        return 0
    if args.retry_stop:
        service.retry_stop(args.retry_stop)
        print(json.dumps(service.public(args.retry_stop), ensure_ascii=False))
        return 0
    operation = (
        args.resume
        or service.request(args.source, args.name, args.request_id)["operation_id"]
    )
    print(json.dumps(service.public(operation), ensure_ascii=False), flush=True)
    while True:
        record = service.step(operation)
        print(json.dumps(service.public(operation), ensure_ascii=False), flush=True)
        if record["phase"] in {"ready", "aborted"}:
            return 0
        if record.get("error"):
            return 1
        if args.once:
            return 0
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
