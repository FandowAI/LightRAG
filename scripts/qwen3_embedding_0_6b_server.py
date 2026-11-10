"""Serve the local Qwen3-Embedding-0.6B model through an OpenAI-shaped API.

This is intentionally a parallel service to ``qwen_vl_embedding_server.py``.
It defaults to one GPU inference request at a time and accepts at most three
texts per request, so it can coexist with the existing 8B embedding service.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


MODEL_ID = os.getenv("QWEN3_EMBED_MODEL_ID", "Qwen3-Embedding-0.6B")
MODEL_PATH = Path(
    os.getenv("QWEN3_EMBED_MODEL_PATH", "/data/models/Qwen3-Embedding-0.6B")
)
EMBEDDING_DIM = int(os.getenv("QWEN3_EMBED_DIM", "1024"))
MAX_BATCH = int(os.getenv("QWEN3_EMBED_MAX_BATCH", "3"))
MAX_TOKENS = int(os.getenv("QWEN3_EMBED_MAX_TOKENS", "8192"))
logger = logging.getLogger(__name__)


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: int | None = Field(default=None, ge=32, le=EMBEDDING_DIM)


class TransformerTextEncoder:
    def __init__(self) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; start this service on a GPU host")
        if not MODEL_PATH.is_dir():
            raise FileNotFoundError(f"Model directory is missing: {MODEL_PATH}")

        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_PATH,
            padding_side="left",
            local_files_only=True,
        )
        self.model = AutoModel.from_pretrained(
            MODEL_PATH,
            torch_dtype=torch.bfloat16,
            attn_implementation="sdpa",
            device_map={"": 0},
            local_files_only=True,
            low_cpu_mem_usage=True,
        ).eval()

    def _last_token_pool(self, hidden, attention_mask):
        left_padding = bool(
            (attention_mask[:, -1].sum() == attention_mask.shape[0]).item()
        )
        if left_padding:
            return hidden[:, -1]
        lengths = attention_mask.sum(dim=1) - 1
        rows = hidden.shape[0]
        row_ids = self.torch.arange(rows, device=hidden.device)
        return hidden[row_ids, lengths]

    def encode(self, texts: list[str], dimensions: int) -> tuple[np.ndarray, int]:
        inputs = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=MAX_TOKENS,
            return_tensors="pt",
        )
        token_count = int(inputs["attention_mask"].sum().item())
        inputs = {key: value.to(self.model.device) for key, value in inputs.items()}
        with self.torch.inference_mode():
            hidden = self.model(**inputs).last_hidden_state
            vectors = self._last_token_pool(hidden, inputs["attention_mask"])
            vectors = vectors[:, :dimensions].float()
            vectors = self.torch.nn.functional.normalize(vectors, p=2, dim=1)
        return vectors.cpu().numpy(), token_count


def prepare_request(request: EmbeddingRequest) -> tuple[list[str], int]:
    if request.model != MODEL_ID:
        raise HTTPException(status_code=400, detail="Unknown embedding model")
    texts = [request.input] if isinstance(request.input, str) else request.input
    if not texts or len(texts) > MAX_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size must be 1–{MAX_BATCH}",
        )
    if any(not text.strip() for text in texts):
        raise HTTPException(status_code=400, detail="Input text must be nonempty")
    return texts, request.dimensions or EMBEDDING_DIM


def format_response(
    vectors: np.ndarray,
    token_count: int,
    encoding_format: Literal["float", "base64"],
) -> dict:
    data = []
    for index, vector in enumerate(vectors):
        values = np.asarray(vector, dtype="<f4")
        embedding = (
            base64.b64encode(values.tobytes()).decode("ascii")
            if encoding_format == "base64"
            else values.tolist()
        )
        data.append({"object": "embedding", "index": index, "embedding": embedding})
    return {
        "object": "list",
        "data": data,
        "model": MODEL_ID,
        "usage": {"prompt_tokens": token_count, "total_tokens": token_count},
    }


def create_app(encoder_factory: Callable[[], TransformerTextEncoder] | None = None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.encoder = (
            encoder_factory() if encoder_factory is not None else TransformerTextEncoder()
        )
        app.state.inference_lock = asyncio.Lock()
        yield

    app = FastAPI(title="Local Qwen3-Embedding-0.6B", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "model": MODEL_ID,
            "dimensions": EMBEDDING_DIM,
            "max_batch": MAX_BATCH,
            "inference_concurrency": 1,
        }

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]}

    @app.post("/v1/embeddings")
    async def embeddings(request: EmbeddingRequest):
        texts, dimensions = prepare_request(request)
        try:
            async with app.state.inference_lock:
                vectors, token_count = await asyncio.to_thread(
                    app.state.encoder.encode, texts, dimensions
                )
        except Exception as exc:
            logger.exception("Qwen3 embedding inference failed")
            raise HTTPException(
                status_code=500, detail="Embedding inference failed"
            ) from exc

        if vectors.shape != (len(texts), dimensions):
            raise HTTPException(status_code=500, detail="Unexpected embedding shape")
        return format_response(vectors, token_count, request.encoding_format)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("QWEN3_EMBED_PORT", "9623")),
        workers=1,
    )
