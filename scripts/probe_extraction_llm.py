"""Probe one bounded extraction request against the configured LLM provider.

This uses the same JSON extraction prompt and stored chunk text as LightRAG.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from lightrag.constants import (
    DEFAULT_MAX_EXTRACTION_ENTITIES,
    DEFAULT_MAX_EXTRACTION_RECORDS,
)
from lightrag.prompt import PROMPTS, resolve_entity_extraction_prompt_profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--chunk-index", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=True)
    storage = (
        Path(os.environ["WORKING_DIR"])
        / os.environ["WORKSPACE"]
        / "kv_store_text_chunks.json"
    )
    chunks = json.loads(storage.read_text())
    suffix = f"-chunk-{args.chunk_index:03d}"
    matches = [(key, value) for key, value in chunks.items() if key.endswith(suffix)]
    if len(matches) != 1:
        raise SystemExit(f"Expected one stored chunk ending in {suffix}, found {len(matches)}")
    chunk_id, chunk = matches[0]
    content = chunk["content"]
    max_tokens = args.max_tokens or int(
        os.getenv("EXTRACT_OPENAI_LLM_MAX_TOKENS", "8192")
    )
    reasoning_effort = os.getenv("EXTRACT_OPENAI_LLM_REASONING_EFFORT")

    profile = resolve_entity_extraction_prompt_profile({}, True)
    context = {
        "entity_types_guidance": profile["entity_types_guidance"],
        "examples": "\n".join(profile["entity_extraction_json_examples"]),
        "language": os.getenv("SUMMARY_LANGUAGE", "English"),
        "max_total_records": int(
            os.getenv("MAX_EXTRACTION_RECORDS", DEFAULT_MAX_EXTRACTION_RECORDS)
        ),
        "max_entity_records": int(
            os.getenv("MAX_EXTRACTION_ENTITIES", DEFAULT_MAX_EXTRACTION_ENTITIES)
        ),
    }
    system = PROMPTS["entity_extraction_json_system_prompt"].format(**context)
    prompt = PROMPTS["entity_extraction_json_user_prompt"].format(
        **context, input_text=content, heading_context_block=""
    )
    print(
        f"Chunk={chunk_id}; prompt characters: system={len(system)}, "
        f"user={len(prompt)}; output cap={max_tokens}; "
        f"reasoning_effort={reasoning_effort or 'default'}; timeout={args.timeout}s",
        flush=True,
    )
    if args.dry_run:
        return

    client = OpenAI(
        api_key=os.environ["LLM_BINDING_API_KEY"],
        base_url=os.environ["LLM_BINDING_HOST"],
        timeout=args.timeout,
        max_retries=0,
    )
    start = time.monotonic()
    try:
        request_options = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        response = client.chat.completions.create(
            model=os.environ["LLM_MODEL"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            **request_options,
        )
    except Exception as exc:
        print(
            f"Failed after {time.monotonic() - start:.1f}s: "
            f"{type(exc).__name__}: {exc}"
        )
        raise SystemExit(1) from None
    choice = response.choices[0]
    usage = response.usage
    content = choice.message.content or ""
    details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = getattr(details, "reasoning_tokens", None)
    try:
        json.loads(content)
        valid_json = True
    except (json.JSONDecodeError, TypeError):
        valid_json = False
    print(
        f"Completed in {time.monotonic() - start:.1f}s; "
        f"finish_reason={choice.finish_reason}; "
        f"output_chars={len(content)}; valid_json={valid_json}; "
        f"output_tokens={usage.completion_tokens if usage else 'unknown'}; "
        f"reasoning_tokens={reasoning_tokens if reasoning_tokens is not None else 'unknown'}"
    )
    if not valid_json or choice.finish_reason != "stop":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
