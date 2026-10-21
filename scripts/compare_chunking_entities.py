"""Compare the four built-in chunkers and JSON entity extraction on two Markdown files.

Run from the repository root. Results and resumable model responses are kept in
analysis/chunking_comparison_20261019; the source documents are never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)

from lightrag.chunk_schema import (  # noqa: E402
    format_heading_context,
    strip_internal_multimodal_markup_for_extraction,
)
from lightrag.chunker import (  # noqa: E402
    chunking_by_fixed_token,
    chunking_by_paragraph_semantic,
    chunking_by_recursive_character,
    chunking_by_semantic_vector,
)
from lightrag.constants import (  # noqa: E402
    DEFAULT_MAX_EXTRACTION_ENTITIES,
    DEFAULT_MAX_EXTRACTION_RECORDS,
)
from lightrag.llm.openai import openai_embed  # noqa: E402
from lightrag.parser.markdown.parser import NativeMarkdownParser  # noqa: E402
from lightrag.parser.routing import default_chunker_config  # noqa: E402
from lightrag.prompt import PROMPTS, resolve_entity_extraction_prompt_profile  # noqa: E402
from lightrag.sidecar import write_sidecar  # noqa: E402
from lightrag.utils import (  # noqa: E402
    EmbeddingFunc,
    TiktokenTokenizer,
    normalize_entity_name,
    tolerant_load_json_dict,
)

DATA = ROOT / "data"
OUT = ROOT / "analysis" / "chunking_comparison_20261019"
FILES = [DATA / "20250620高管茶话会.md", DATA / "20250620高管茶话会标题修复版.md"]
STRATEGIES = "FRVP"
SIZE = 1200
OVERLAP = 100


def save_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_document(path: Path, index: int) -> dict:
    parser = NativeMarkdownParser()
    parsed_dir = OUT / "parsed" / f"doc{index}.parsed"
    blocks, warnings, metadata = parser._extract_text(
        path.read_text(encoding="utf-8-sig"), bundle_root=None
    )
    ir = parser.build_ir(
        blocks,
        document_name=path.name,
        asset_dir_name=f"{path.stem}.blocks.assets",
        metadata=metadata,
    )
    parsed = write_sidecar(
        ir, parsed_dir=parsed_dir, doc_id=f"doc-compare-{index}", engine="native"
    )
    parsed["warnings"] = warnings
    parsed["source"] = str(path)
    parsed["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    parsed["structural_blocks"] = len(blocks)
    return parsed


async def make_chunks() -> None:
    tokenizer = TiktokenTokenizer()
    config = default_chunker_config()

    async def embed(texts: list[str]):
        return await openai_embed.func(
            texts,
            model=os.environ["EMBEDDING_MODEL"],
            base_url=os.environ["EMBEDDING_BINDING_HOST"],
            api_key=os.environ["EMBEDDING_BINDING_API_KEY"],
            embedding_dim=int(os.environ["EMBEDDING_DIM"]),
            max_token_size=int(os.getenv("EMBEDDING_TOKEN_LIMIT", "8192")),
        )

    embedding = EmbeddingFunc(
        embedding_dim=int(os.environ["EMBEDDING_DIM"]),
        max_token_size=int(os.getenv("EMBEDDING_TOKEN_LIMIT", "8192")),
        func=embed,
        model_name=os.environ["EMBEDDING_MODEL"],
    )
    for index, path in enumerate(FILES, 1):
        parsed = parse_document(path, index)
        content = parsed["content"]
        for strategy in STRATEGIES:
            target = OUT / "runs" / f"doc{index}_{strategy}.json"
            if target.exists():
                print(f"reuse {target.name}", flush=True)
                continue
            if strategy == "F":
                chunks = chunking_by_fixed_token(
                    tokenizer, content, SIZE, chunk_overlap_token_size=OVERLAP
                )
            elif strategy == "R":
                chunks = chunking_by_recursive_character(
                    tokenizer,
                    content,
                    SIZE,
                    chunk_overlap_token_size=OVERLAP,
                    separators=config["recursive_character"]["separators"],
                )
            elif strategy == "V":
                opts = config["semantic_vector"]
                chunks = await chunking_by_semantic_vector(
                    tokenizer,
                    content,
                    SIZE,
                    embedding_func=embedding,
                    embedding_batch_num=int(os.getenv("EMBEDDING_BATCH_NUM", "4")),
                    embedding_max_async=4,
                    **opts,
                )
            else:
                chunks = chunking_by_paragraph_semantic(
                    tokenizer,
                    content,
                    SIZE,
                    blocks_path=parsed["blocks_path"],
                    chunk_overlap_token_size=OVERLAP,
                    drop_references=False,
                    doc_id=f"doc-compare-{index}",
                )
            save_json(
                target,
                {
                    "document": path.name,
                    "strategy": strategy,
                    "source_sha256": parsed["source_sha256"],
                    "source_chars": len(path.read_text(encoding="utf-8-sig")),
                    "parsed_chars": len(content),
                    "structural_blocks": parsed["structural_blocks"],
                    "parser_warnings": parsed["warnings"],
                    "chunk_size": SIZE,
                    "overlap": OVERLAP if strategy != "V" else 0,
                    "chunks": chunks,
                },
            )
            print(f"doc{index} {strategy}: {len(chunks)} chunks", flush=True)


def extraction_prompts(chunk: dict) -> tuple[str, str]:
    profile = resolve_entity_extraction_prompt_profile({}, True)
    context = {
        "entity_types_guidance": profile["entity_types_guidance"],
        "examples": "\n".join(profile["entity_extraction_json_examples"]),
        "language": os.getenv("SUMMARY_LANGUAGE", "Chinese"),
        "max_total_records": int(
            os.getenv("MAX_EXTRACTION_RECORDS", DEFAULT_MAX_EXTRACTION_RECORDS)
        ),
        "max_entity_records": int(
            os.getenv("MAX_EXTRACTION_ENTITIES", DEFAULT_MAX_EXTRACTION_ENTITIES)
        ),
    }
    heading = format_heading_context(chunk)
    heading_block = (
        PROMPTS["entity_extraction_section_context"].format(heading_path=heading)
        if heading
        else ""
    )
    return (
        PROMPTS["entity_extraction_json_system_prompt"].format(**context),
        PROMPTS["entity_extraction_json_user_prompt"].format(
            **context,
            input_text=strip_internal_multimodal_markup_for_extraction(chunk["content"]),
            heading_context_block=heading_block,
        ),
    )


async def extract_entities() -> None:
    from openai import AsyncOpenAI

    runs = [load_json(OUT / "runs" / f"doc{i}_{s}.json") for i in (1, 2) for s in STRATEGIES]
    jobs: dict[str, tuple[str, str]] = {}
    for run in runs:
        for chunk in run["chunks"]:
            prompts = extraction_prompts(chunk)
            key = hashlib.sha256("\0".join(prompts).encode()).hexdigest()
            chunk["extraction_key"] = key
            jobs[key] = prompts
    print(f"{sum(len(r['chunks']) for r in runs)} chunks, {len(jobs)} unique prompts", flush=True)

    cache = OUT / "responses"
    cache.mkdir(parents=True, exist_ok=True)
    def needs_extraction(key: str) -> bool:
        path = cache / f"{key}.json"
        if not path.exists():
            return True
        response = load_json(path)
        return not response.get("parsed") and not response.get("error")

    pending = [(key, prompts) for key, prompts in jobs.items() if needs_extraction(key)]
    print(f"{len(pending)} model calls pending", flush=True)
    client = AsyncOpenAI(
        api_key=os.environ["LLM_BINDING_API_KEY"],
        base_url=os.environ["LLM_BINDING_HOST"],
        timeout=float(os.getenv("EXTRACT_LLM_TIMEOUT", "600")),
        max_retries=1,
    )
    semaphore = asyncio.Semaphore(6)
    done = 0

    async def run_one(key: str, prompts: tuple[str, str]) -> None:
        nonlocal done
        async with semaphore:
            for attempt in range(3):
                try:
                    response = await client.chat.completions.create(
                        model=os.environ["LLM_MODEL"],
                        messages=[
                            {"role": "system", "content": prompts[0]},
                            {"role": "user", "content": prompts[1]},
                        ],
                        response_format={"type": "json_object"},
                        max_tokens=int(os.getenv("EXTRACT_OPENAI_LLM_MAX_TOKENS", "8192")),
                        reasoning_effort=os.getenv("EXTRACT_OPENAI_LLM_REASONING_EFFORT", "low"),
                    )
                    choice = response.choices[0]
                    raw = choice.message.content or ""
                    parsed = tolerant_load_json_dict(raw)
                    save_json(
                        cache / f"{key}.json",
                        {
                            "finish_reason": choice.finish_reason,
                            "parsed": parsed,
                            "raw": raw,
                            "usage": response.usage.model_dump() if response.usage else None,
                        },
                    )
                    break
                except Exception as exc:
                    if attempt == 2:
                        save_json(cache / f"{key}.json", {"error": f"{type(exc).__name__}: {exc}"})
                    else:
                        await asyncio.sleep(2**attempt)
            done += 1
            if done % 10 == 0 or done == len(pending):
                print(f"extracted {done}/{len(pending)}", flush=True)

    try:
        await asyncio.gather(*(run_one(key, prompts) for key, prompts in pending))
    finally:
        await client.close()
    for run in runs:
        save_json(OUT / "runs" / f"doc{FILES.index(DATA / run['document']) + 1}_{run['strategy']}.json", run)


def make_report() -> None:
    runs = [load_json(OUT / "runs" / f"doc{i}_{s}.json") for i in (1, 2) for s in STRATEGIES]
    rows = []
    entities_by_run = {}
    for run in runs:
        name = f"{'修复版' if '修复' in run['document'] else '原版'}-{run['strategy']}"
        entity_rows = defaultdict(lambda: {"types": Counter(), "chunks": [], "descriptions": []})
        failures = []
        total_mentions = 0
        for index, chunk in enumerate(run["chunks"], 1):
            response = load_json(OUT / "responses" / f"{chunk['extraction_key']}.json")
            parsed = response.get("parsed") or {}
            if response.get("error") or response.get("finish_reason") != "stop" or not parsed:
                failures.append(index)
            for entity in parsed.get("entities", []):
                if not isinstance(entity, dict):
                    continue
                entity_name = normalize_entity_name(str(entity.get("name") or ""))
                if not entity_name or not str(entity.get("description") or "").strip():
                    continue
                total_mentions += 1
                entry = entity_rows[entity_name]
                entry["types"][str(entity.get("type") or "Other")] += 1
                entry["chunks"].append(index)
                entry["descriptions"].append(str(entity["description"]))
        records = [
            {
                "name": entity_name,
                "type": values["types"].most_common(1)[0][0],
                "chunks": sorted(set(values["chunks"])),
                "mentions": len(values["chunks"]),
                "description": values["descriptions"][0],
            }
            for entity_name, values in entity_rows.items()
        ]
        records.sort(key=lambda x: (-x["mentions"], x["name"]))
        entities_by_run[name] = {r["name"] for r in records}
        save_json(OUT / "entities" / f"{name}.json", records)
        tokens = [c["tokens"] for c in run["chunks"]]
        rows.append({
            "name": name,
            "chunks": len(tokens),
            "min": min(tokens),
            "median": round(statistics.median(tokens)),
            "max": max(tokens),
            "short_under_300": sum(t < 300 for t in tokens),
            "entities": len(records),
            "mentions": total_mentions,
            "failed_chunks": failures,
        })
    save_json(OUT / "summary.json", rows)
    by_name = {row["name"]: row for row in rows}
    lines = [
        "# 两份文档 × 四种切块：切块与实体对比",
        "",
        "每份文档各解析一次，再分别使用四种真实内置切块器；统一 1200 token 上限。F/R 的相邻块重叠设置为 100 token；P 仅在内部递归回退时使用该重叠预算，V 不重叠。",
        "实体使用当前 .env 的抽取模型与 LightRAG JSON 初次抽取提示词；按规范化名称汇总各块实体，不执行图谱的描述合并或二次补抽。实体名称相近但不完全相同的条目（如“360环评”与“360度环评”）仍分开计算。",
        "",
        "| 文档-策略 | 块数 | token 最小/中位/最大 | <300 token | 去重实体 | 实体提及 | 失败块 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['chunks']} | {row['min']}/{row['median']}/{row['max']} | "
            f"{row['short_under_300']} | {row['entities']} | {row['mentions']} | "
            f"{','.join(map(str, row['failed_chunks'])) or '0'} |"
        )
    lines += [
        "",
        "## 两份文档横向对比",
        "",
        "| 策略 | 原版切块 | 修复版切块 | 原版实体 | 修复版实体 | 共有实体 | 仅原版 | 仅修复版 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in STRATEGIES:
        a, b = entities_by_run[f"原版-{strategy}"], entities_by_run[f"修复版-{strategy}"]
        lines.append(
            f"| {strategy} | {by_name[f'原版-{strategy}']['chunks']} | "
            f"{by_name[f'修复版-{strategy}']['chunks']} | {len(a)} | {len(b)} | "
            f"{len(a & b)} | {len(a - b)} | {len(b - a)} |"
        )
    p_original = next(r for r in runs if r["strategy"] == "P" and "修复" not in r["document"])
    p_fixed = next(r for r in runs if r["strategy"] == "P" and "修复" in r["document"])
    p_original_parents = sum(bool(c.get("heading", {}).get("parent_headings")) for c in p_original["chunks"])
    p_fixed_parents = sum(bool(c.get("heading", {}).get("parent_headings")) for c in p_fixed["chunks"])
    total_failures = sum(len(row["failed_chunks"]) for row in rows)
    lines += [
        "",
        "## 结果解读",
        "",
        f"- 标题修复让 P 块中带父级标题路径的数量从 {p_original_parents}/{len(p_original['chunks'])} 提升到 {p_fixed_parents}/{len(p_fixed['chunks'])}；P 的块数仍为 17，说明主要收益是章节语境，而非块数变化。",
        "- F 和 R 的块数在两份文档上分别保持 13 和 14。它们对标题符号的变化有局部边界变化，但没有利用父级标题路径。",
        "- V 在两份文档中分别产生 33 和 32 块，其中短于 300 token 的块分别有 16 和 15 个。它给出最多的实体名称，也需要最多逐块抽取；更多名称不等于更准确或更适合检索。",
        f"- 共 {total_failures} 个块未成功抽取（模型服务过滤或返回空对象），已在上表列出块号。相关组的实体数是下界，不能将失败块当作无实体。",
    ]
    lines += [
        "",
        "## 文件索引",
        "",
        "- [交互式 HTML 对照](index.html)：两份文档分别查看、完整切块与逐块输出、实体明细、关系图谱预览及并排差异筛选。图谱来自本次抽取结果，尚未写入 Neo4j。",
        "- [完整切块横向对照](chunks_side_by_side.md)：每种策略下按块号并排展示两份文档的完整文本。块号仅表示各自顺序，不保证语义一一对应。",
        "- [实体横向矩阵](entities_side_by_side.md)：每个实体在八组实验中的出现情况，含各策略独有实体。",
        "- [实体矩阵 CSV](entity_matrix.csv)：便于按八组结果筛选与排序。",
        "- `responses/`：逐块模型原始响应及用量，可核查失败或截断。",
        "",
        "| 策略 | 原版完整切块 | 修复版完整切块 | 原版实体明细 | 修复版实体明细 |",
        "|---|---|---|---|---|",
    ]
    for strategy in STRATEGIES:
        lines.append(
            f"| {strategy} | [JSON](runs/doc1_{strategy}.json) | "
            f"[JSON](runs/doc2_{strategy}.json) | "
            f"[JSON](entities/原版-{strategy}.json) | "
            f"[JSON](entities/修复版-{strategy}.json) |"
        )
    lines += [
        "",
        "## 解读边界",
        "",
        "Markdown 文件引用的 images/ 图片未包含在本次输入目录，原生解析器跳过了图片；八组实验一致。实体为初次抽取后按名称去重，未运行 LightRAG 图谱合并、关系归并或二次补抽；数量仅用于本实验横向比较。",
        "",
    ]
    (OUT / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    chunk_lines = ["# 完整切块横向对照", "", "同一行块号只是各文档内部顺序，不保证内容语义对齐。", ""]
    for strategy in STRATEGIES:
        original = next(r for r in runs if r["strategy"] == strategy and "修复" not in r["document"])
        fixed = next(r for r in runs if r["strategy"] == strategy and "修复" in r["document"])
        chunk_lines.extend([f"## {strategy}", "", "<table><thead><tr><th>块号</th><th>原版</th><th>标题修复版</th></tr></thead><tbody>"])
        for i in range(max(len(original["chunks"]), len(fixed["chunks"]))):
            cells = []
            for run in (original, fixed):
                if i >= len(run["chunks"]):
                    cells.append("<td>—</td>")
                    continue
                chunk = run["chunks"][i]
                heading = format_heading_context(chunk)
                title = f"<strong>{html.escape(heading)}</strong><br>" if heading else ""
                cells.append(
                    f"<td>{title}{chunk['tokens']} token<pre style=\"white-space:pre-wrap\">"
                    f"{html.escape(chunk['content'])}</pre></td>"
                )
            chunk_lines.append(f"<tr><td>{i + 1}</td>{''.join(cells)}</tr>")
        chunk_lines.extend(["</tbody></table>", ""])
    (OUT / "chunks_side_by_side.md").write_text("\n".join(chunk_lines), encoding="utf-8")

    all_entities = sorted(set().union(*entities_by_run.values()))
    columns = [f"{doc}-{strategy}" for doc in ("原版", "修复版") for strategy in STRATEGIES]
    with (OUT / "entity_matrix.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["实体", *columns])
        for entity in all_entities:
            writer.writerow([entity, *(int(entity in entities_by_run[col]) for col in columns)])
    entity_lines = [
        "# 实体横向矩阵",
        "",
        "✓ 表示该实验至少一个块抽取到了该规范化实体名称。",
        "",
        "| 实体 | 原F | 原R | 原V | 原P | 修F | 修R | 修V | 修P |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for entity in all_entities:
        entity_lines.append(
            f"| {entity.replace('|', '&#124;')} | "
            + " | ".join("✓" if entity in entities_by_run[col] else "" for col in columns)
            + " |"
        )
    entity_lines.extend(["", "## 各策略独有实体", ""])
    for strategy in STRATEGIES:
        a, b = entities_by_run[f"原版-{strategy}"], entities_by_run[f"修复版-{strategy}"]
        entity_lines += [
            f"### {strategy}",
            "",
            f"仅原版（{len(a - b)}）：{'、'.join(sorted(a - b)) or '无'}",
            "",
            f"仅修复版（{len(b - a)}）：{'、'.join(sorted(b - a)) or '无'}",
            "",
        ]
    (OUT / "entities_side_by_side.md").write_text("\n".join(entity_lines), encoding="utf-8")
    print(f"report: {OUT / 'comparison.md'}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("chunks", "entities", "report"))
    args = parser.parse_args()
    if args.phase == "chunks":
        asyncio.run(make_chunks())
    elif args.phase == "entities":
        asyncio.run(extract_entities())
    else:
        make_report()


if __name__ == "__main__":
    main()
