"""Bounded, read-only previews of already published evidence packages."""

import json


def preview_packages(path, job_id, workspace, offset=0, limit=10):
    items = []
    count = 0
    with path.open("rb") as stream:
        while raw := stream.readline(64 * 1024 * 1024 + 1):
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError(
                    "Package exceeds preview limit; use the complete export"
                )
            if not raw.strip():
                continue
            if count < offset:
                count += 1
                continue
            if len(items) == limit:
                return {
                    "items": items,
                    "offset": offset,
                    "next_offset": offset + len(items),
                    "has_more": True,
                }
            item = json.loads(raw)
            if (
                not isinstance(item, dict)
                or item.get("batch_id") != job_id
                or item.get("workspace") != workspace
                or item.get("schema_version") != "enterprise-evidence/1.0"
            ):
                raise ValueError("Package identity does not match the batch workspace")
            items.append(item)
            count += 1
    return {
        "items": items,
        "offset": offset,
        "next_offset": offset + len(items),
        "has_more": False,
    }
