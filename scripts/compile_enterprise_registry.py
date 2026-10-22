"""Compile a private glossary and optional reviewed aliases into one identity roster."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re


KINDS = {
    "公司名": "企业主体",
    "品牌名": "品牌",
    "组织架构": "组织与部门",
    "管理工具": "资源、系统与工具",
    "内部系统": "资源、系统与工具",
    "知识体系": "资源、系统与工具",
    "内部项目": "项目与重要事件",
    "企业文化": "文化与管理理念",
    "核心价值观": "文化与管理理念",
    "内部产品": "产品与品类",
    "自研成分": "资源、系统与工具",
    "管理机制": "管理机制",
    "人才机制": "管理机制",
    "决策机制": "管理机制",
    "管理方法": "业务知识与专业方法",
    "产品方法": "业务知识与专业方法",
    "营销指标": "指标与经营事实",
    "产品指标": "指标与经营事实",
}


def compile_registry(glossary: dict, supplemental: dict | None = None) -> dict:
    """Primary canonical names win; supplemental file-scoped rules stay file-scoped."""
    entries = {}
    alias_targets = defaultdict(set)
    for row in glossary["entities"]:
        kind = "人物" if row["is_person"] else KINDS.get(row["kind"], "*")
        name = row["canonical"]
        key = (name, kind)
        entry = entries.setdefault(
            key,
            {
                "id": row["id"],
                "canonical_name": name,
                "entity_type": kind,
                "aliases": [],
                "verified": True,
                "scope": {},
                "evidence": [],
            },
        )
        entry["aliases"] = sorted(set(entry["aliases"]) | {name, *row["aliases"]})
        entry["evidence"].append(
            {
                "type": "user_glossary",
                "cell": row["source_cell"],
                "source_sha256": glossary["source_workbook_sha256"],
            }
        )
        for alias in entry["aliases"]:
            alias_targets[alias.casefold()].add(key)
    for row in (supplemental or {}).get("entries", []):
        if row.get("verified") is not True:
            continue
        # Local hash suffixes identify a pilot observation, not a reusable name.
        if re.search(r"\[[a-f0-9]{10}\]$", row["canonical_name"]):
            continue
        aliases = {row["canonical_name"], *row.get("aliases", [])}
        possible = set().union(
            *(alias_targets.get(a.casefold(), set()) for a in aliases)
        )
        compatible = [key for key in possible if key[1] in (row["entity_type"], "*")]
        if len(compatible) == 1:
            entry = entries[compatible[0]]
            if row.get("source_files") or row.get("source_scope_corrections"):
                # Do not turn a case-specific correction into a global rename.
                continue
            entry["aliases"] = sorted(set(entry["aliases"]) | aliases)
            if not entry.get("scope") and row.get("scope"):
                entry["scope"] = row["scope"]
            entry["evidence"].append(
                {"type": "reviewed_alias_registry", "id": row.get("id")}
            )
        elif not possible:
            entry = {**row, "aliases": sorted(aliases)}
            entries[(entry["canonical_name"], entry["entity_type"])] = entry
    ordered = sorted(
        entries.values(), key=lambda e: (e["entity_type"], e["canonical_name"])
    )
    return {
        "version": "enterprise-v1-" + str(glossary["version"]),
        "source_glossary_sha256": glossary["content_sha256"],
        "source_workbook_sha256": glossary["source_workbook_sha256"],
        "entries": ordered,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glossary", type=Path, required=True)
    parser.add_argument("--supplemental", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--context-company",
        help="Corpus boundary for unscoped aliases; never implies employment",
    )
    args = parser.parse_args()
    glossary = json.loads(args.glossary.read_text(encoding="utf-8"))
    actual = hashlib.sha256(
        json.dumps(
            {k: v for k, v in glossary.items() if k != "content_sha256"},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if actual != glossary["content_sha256"]:
        raise ValueError("Glossary content hash mismatch")
    supplemental = (
        json.loads(args.supplemental.read_text(encoding="utf-8"))
        if args.supplemental
        else None
    )
    result = compile_registry(glossary, supplemental)
    if args.context_company:
        result["context_scope"] = {"company": args.context_company}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"entries": len(result["entries"]), "version": result["version"]}))


if __name__ == "__main__":
    main()
