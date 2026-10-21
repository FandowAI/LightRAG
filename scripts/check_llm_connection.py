"""Check the configured OpenAI-compatible LLM with one short request."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def main() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        raise SystemExit(f"Missing configuration: {env_path}")

    load_dotenv(env_path, override=True)
    model = os.getenv("LLM_MODEL")
    base_url = os.getenv("LLM_BINDING_HOST")
    api_key = os.getenv("LLM_BINDING_API_KEY")
    if not all((model, base_url, api_key)):
        raise SystemExit("Set LLM_MODEL, LLM_BINDING_HOST, and LLM_BINDING_API_KEY in .env")

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "只回复：连接成功"}],
        max_tokens=256,
    )
    choice = response.choices[0]
    print(f"LLM request succeeded: model={response.model}, finish_reason={choice.finish_reason}")
    print(choice.message.content or "(No visible text returned)")


if __name__ == "__main__":
    main()
