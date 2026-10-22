"""Execute retrieval, grounded selection and relevance-owned packaging."""

import asyncio
import hashlib
import json

from lightrag.entity_packages import from_rag, equivalence_judge
from lightrag.focused_evidence import (
    build_packages,
    model_selector,
    selection_payload,
    tasks_from_results,
    validate_selection,
)


async def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    await asyncio.to_thread(
        temporary.write_text, json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)


async def run_focused(manager, job_id, settings):
    from lightrag.batch_retrieval import now

    out = manager.root / job_id
    results = []
    try:
        manager.update(
            job_id,
            lambda j: j.update(
                status="running",
                phase="retrieving",
                phase_done=0,
                phase_total=j["total"],
            ),
        )
        for index, item in enumerate(manager.get(job_id)["items"]):
            if manager.get(job_id)["cancel_requested"]:
                break
            center = item["center"]
            manager.update(job_id, lambda j: j.update(current_center=center))
            try:
                cached = out / f"candidate-{index:04d}.json"
                call = (
                    manager.retrieve(
                        manager.rag, center, settings, relation_evidence_only=True
                    )
                    if manager.retrieve is from_rag
                    else manager.retrieve(manager.rag, center, settings)
                )
                if cached.exists():
                    # Explicit resume uses the frozen candidate snapshot.
                    if hasattr(call, "close"):
                        call.close()
                    result = json.loads(cached.read_text(encoding="utf-8"))
                else:
                    result = await asyncio.wait_for(call, timeout=600)
                packages = result["packages"]
                if (
                    result.get("center") != center
                    or len(packages) > 1
                    or any(
                        p["center"] != center or not p.get("chunks") for p in packages
                    )
                ):
                    raise ValueError("Invalid grounded retrieval adapter result")
                await write_json(out / f"candidate-{index:04d}.json", result)
                results.append(result)
                error = None
            except Exception as exc:
                results.append(None)
                error = type(exc).__name__

            def retrieved(job):
                row = job["items"][index]
                row["status"] = "failed" if error else "retrieved"
                if error:
                    row["error_type"] = error
                    job["failed"] += 1
                job["phase_done"] += 1

            manager.update(job_id, retrieved)

        async def stopped():
            if not manager.get(job_id)["cancel_requested"]:
                return False
            # Preparation is deliberately not exported as a half-selected pack.
            if manager.get(job_id)["package_count"]:
                export_path = await asyncio.to_thread(manager.persist_export, job_id)
                manager.update(job_id, lambda j: j.update(export_path=export_path))
            manager.update(
                job_id,
                lambda j: j.update(
                    status="cancelled",
                    phase=None,
                    current_center=None,
                    finished_at=now(),
                ),
            )
            return True

        if await stopped():
            return
        tasks = tasks_from_results(results)
        manager.update(
            job_id,
            lambda j: j.update(
                phase="selecting",
                phase_done=0,
                phase_total=len(tasks),
                current_center=None,
            ),
        )
        select = manager.selector or model_selector(manager.rag)
        semaphore = asyncio.Semaphore(4)

        async def refine(task):
            async with semaphore:
                if manager.get(job_id)["cancel_requested"]:
                    return None
                audit = {
                    "source_chunk_id": task["chunk"]["chunk_id"],
                    "centers": task["centers"],
                }
                filename = (
                    "focus-"
                    + hashlib.sha256(audit["source_chunk_id"].encode()).hexdigest()[:20]
                    + ".json"
                )
                cached = out / filename
                payload, units = None, None
                if cached.exists():
                    try:
                        payload, units = await selection_payload(task, manager.rag)
                        saved = json.loads(cached.read_text(encoding="utf-8"))
                        if (
                            manager.get(job_id).get("rebuild_of")
                            and saved.get("status") == "failed"
                            and saved.get("centers") == task["centers"]
                        ):
                            # A packaging-only rebuild must not silently repeat
                            # failed paid judgments. Explicit resume can retry.
                            saved["reused"] = True
                            audit = saved
                        elif (
                            saved.get("status") in {"selected", "review"}
                            and saved.get("centers") == task["centers"]
                            and saved.get("units") == units
                        ):
                            saved["topics"] = validate_selection(
                                saved["response"], units, task["centers"]
                            )
                            saved["reused"] = True
                            audit = saved
                    except (ValueError, OSError, KeyError):
                        pass
                for attempt in range(2):
                    if audit.get("reused"):
                        break
                    try:
                        if attempt and manager.get(job_id)["cancel_requested"]:
                            break
                        if payload is None or units is None:
                            payload, units = await selection_payload(task, manager.rag)
                        if attempt:
                            payload = {
                                **payload,
                                "retry_instruction": "上次输出未通过格式、连续原文或评分校验。请完整重审，严格引用给定连续unit_id并为所有中心评分。",
                            }
                        response = await asyncio.wait_for(select(payload), timeout=150)
                        topics = validate_selection(response, units, task["centers"])
                        audit.update(
                            status="review" if response["review"] else "selected",
                            units=units,
                            source_snapshots=task.get("source_snapshots", []),
                            response=response,
                            topics=topics,
                            attempts=attempt + 1,
                        )
                        break
                    except Exception as exc:
                        audit.update(
                            status="failed",
                            error_type=type(exc).__name__,
                            attempts=attempt + 1,
                        )
                filename = (
                    "focus-"
                    + hashlib.sha256(audit["source_chunk_id"].encode()).hexdigest()[:20]
                    + ".json"
                )
                await write_json(out / filename, audit)

                def progress(job):
                    job["phase_done"] += 1
                    if audit["status"] in {"failed", "review"}:
                        job["review_pending"] += 1

                manager.update(job_id, progress)
                return audit

        audits = [
            a for a in await asyncio.gather(*(refine(task) for task in tasks)) if a
        ]
        if await stopped():
            return
        valid = [r for r in results if r is not None]
        manager.update(
            job_id,
            lambda j: j.update(phase="packaging", phase_done=0, phase_total=len(valid)),
        )
        outputs, dedup_audit = await asyncio.wait_for(
            build_packages(
                valid,
                audits,
                settings,
                lambda text: len(manager.rag.tokenizer.encode(text)),
                equivalence_judge(manager.rag)
                if settings.near_dedup and getattr(manager.rag, "role_llm_funcs", None)
                else None,
            ),
            timeout=600,
        )
        await write_json(out / "focus-dedup-audit.json", dedup_audit)
        by_center = {r["center"]: r for r in outputs}
        for index, item in enumerate(manager.get(job_id)["items"]):
            if await stopped():
                return
            result = by_center.get(item["center"])
            if result is not None:
                await write_json(out / f"entity-{index:04d}.json", result)

            def finish(job):
                row = job["items"][index]
                job["processed"] += 1
                if result is None:
                    return
                job["phase_done"] += 1
                duplicates = len(result["shared_evidence"])
                count = len(result["packages"])
                state = (
                    "completed"
                    if count
                    else "skipped_duplicate"
                    if duplicates
                    else "filtered_out"
                    if result["summary"]["retrieved_chunks"]
                    else "empty"
                )
                row.update(status=state, packages=count)
                job["package_count"] += count
                job["duplicate_chunks"] += duplicates
                job["skipped"] += int(state == "skipped_duplicate")
                job["filtered"] += int(state == "filtered_out")
                job["empty"] += int(state == "empty")

            manager.update(job_id, finish)
        export_path = await asyncio.to_thread(manager.persist_export, job_id)
        manager.update(
            job_id,
            lambda j: j.update(
                export_path=export_path,
                status="completed_with_errors"
                if j["failed"] or j["review_pending"]
                else "completed",
                phase=None,
                current_center=None,
                finished_at=now(),
            ),
        )
    except BaseException:
        manager.update(
            job_id,
            lambda j: j.update(
                status="interrupted", phase=None, current_center=None, finished_at=now()
            ),
        )
        raise
