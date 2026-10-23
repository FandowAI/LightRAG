"""Remove repeated source text before a batch package is saved or exported.

The first successfully committed package owns a passage. Later centers keep
auditable references, never another copy in their distillation text.
"""

from copy import deepcopy
import hashlib

from lightrag.entity_packages import deduplicate, normalize, render, guard


def fingerprint(chunk):
    return hashlib.sha256(normalize(chunk["content"]).encode()).hexdigest()


async def deduplicate_result(result, previous, settings, token_count, judge=None):
    result = deepcopy(result)
    if not result["packages"]:
        return result
    package = result["packages"][0]
    if not package.get("chunks"):
        raise ValueError(
            "Retrieval adapter must provide source chunks for batch deduplication"
        )
    prior = [(p, c) for r in previous for p in r["packages"] for c in p["chunks"]]
    by_id, by_guard = {}, {}
    for owner, old in prior:
        for variant in [old, *old.get("variants", [])]:
            by_id.setdefault(variant["chunk_id"], []).append(
                (owner, old, fingerprint(variant))
            )
        by_guard.setdefault(tuple(tuple(v) for v in guard(old)), []).append(
            (owner, old)
        )
    kept, shared, audit = [], [], []
    for chunk in package["chunks"]:
        match = None
        # Same immutable source ID plus identical text is decisive even when
        # different retrieval paths supplied different provenance metadata.
        for owner, old, digest in by_id.get(chunk["chunk_id"], []):
            if fingerprint(chunk) == digest:
                match = owner, old, "same_chunk_id"
                break
        if match is None:
            for owner, old in by_guard.get(tuple(tuple(v) for v in guard(chunk)), []):
                # Reuse the scope/time/number guards and AI equivalence checks.
                reduced, checks = await deduplicate([old, chunk], settings, judge)
                audit.extend(checks)
                if len(reduced) == 1:
                    match = owner, old, checks[-1]["kind"]
                    break
        if match is None:
            kept.append(chunk)
            continue
        owner, canonical, reason = match
        shared.append(
            {
                "chunk_id": chunk["chunk_id"],
                "canonical_chunk_id": canonical["chunk_id"],
                "owner_center": owner["center"],
                "owner_package_id": owner["package_id"],
                "reason": reason,
                "normalized_content_sha256": fingerprint(chunk),
                "provenance": chunk["provenance"],
                "via_entities": chunk.get("via_entities", []),
                "variants": [
                    {"chunk_id": v["chunk_id"], "provenance": v["provenance"]}
                    for v in chunk.get("variants", [])
                ],
            }
        )
    result["shared_evidence"] = shared
    result["cross_package_dedup_audit"] = audit
    result.setdefault("summary", {}).update(
        cross_package_duplicates=len(shared),
        unique_chunks=len(kept),
        retrieved_chunks=len(package["chunks"]),
    )
    if not kept:
        result["packages"] = []
        result["summary"]["packages"] = 0
        return result
    if shared:
        # Only index paths needed by genuinely new evidence. Keep all original
        # graph matches on result.entities for audit, not repeated teacher input.
        needed = {
            package["center"],
            *(name for c in kept for name in c.get("via_entities", [])),
        }
        for entity in package["entities"]:
            if entity["name"] in needed:
                needed.update(entity["path"])
        entities = [e for e in package["entities"] if e["name"] in needed]
        owned_ids = {p["chunk_id"] for c in kept for p in c["provenance"]}
        relationships = [
            {
                **edge,
                "source_chunk_ids": [
                    cid for cid in edge["source_chunk_ids"] if cid in owned_ids
                ],
            }
            for edge in package["relationships"]
            if edge["source"] in needed
            and edge["target"] in needed
            and owned_ids.intersection(edge["source_chunk_ids"])
        ]
        paths = {e["name"]: e["path"] for e in entities}
        text = render(package["center"], list(paths), paths, kept, relationships)
        text = (
            "本包仅包含本批次首次收录的原文；跨包重复证据的归属记录见元数据。\n" + text
        )
        tokens = token_count(text)
        notice = {
            **package["length_notice"],
            "exceeds_review_threshold": tokens
            > package["length_notice"]["review_threshold"],
        }
        package.update(
            chunks=kept,
            entities=entities,
            relationships=relationships,
            text=text,
            tokens=tokens,
            length_notice=notice,
        )
        record = package["distillation_input"]
        record["text"] = text
        record["metadata"].update(
            entities=list(paths),
            relationships=relationships,
            provenance=[p for c in kept for p in c["provenance"]],
            tokens=tokens,
            length_notice=notice,
        )
    package["distillation_input"]["metadata"].update(
        evidence_scope="batch_unique_passages",
        shared_evidence=shared,
        dedup_ownership="first_successfully_committed_center",
    )
    return result
