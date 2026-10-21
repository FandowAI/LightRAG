"""Serve local Qwen3-VL-Embedding-8B text vectors through the OpenAI API shape.

This follows Qwen's Transformers example: Qwen3VLModel, the model's chat
template, last-token pooling, and L2 normalization. The LightRAG OpenAI
embedding binding sends text, so image and video inputs are outside this API.
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


MODEL_ID = "Qwen3-VL-Embedding-8B"
MODEL_PATH = Path(os.getenv("QWEN_VL_MODEL_PATH", "/data/Qwen3-VL-Embedding-8B"))
EMBEDDING_DIM = 4096
MAX_BATCH = 4
MAX_TOKENS = 8192
INSTRUCTION = "Represent the user's input."
logger = logging.getLogger(__name__)


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]
    encoding_format: Literal["float", "base64"] = "float"
    dimensions: int | None = Field(default=None, ge=64, le=EMBEDDING_DIM)


class TransformerTextEncoder:
    """Load Qwen's local weights on the single GPU exposed by the launcher."""

    def __init__(self) -> None:
        import torch
        from transformers.models.qwen3_vl.modeling_qwen3_vl import (
            Qwen3VLModel,
            Qwen3VLPreTrainedModel,
        )
        from transformers.models.qwen3_vl.processing_qwen3_vl import (
            Qwen3VLProcessor,
        )

        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable; start this service on the GPU host")
        if not MODEL_PATH.is_dir():
            raise FileNotFoundError(f"Model directory is missing: {MODEL_PATH}")

        # The official checkpoint stores tensors under "model.*". This wrapper
        # matches Qwen's Qwen3VLForEmbedding without its vision-only dependency.
        class Qwen3VLForEmbedding(Qwen3VLPreTrainedModel):
            _checkpoint_conversion_mapping = {}
            accepts_loss_kwargs = False

            def __init__(self, config):
                super().__init__(config)
                self.model = Qwen3VLModel(config)
                self.post_init()

            def get_input_embeddings(self):
                return self.model.get_input_embeddings()

            def set_input_embeddings(self, value):
                self.model.set_input_embeddings(value)

            def forward(self, **kwargs):
                return self.model(**kwargs)

        self.torch = torch
        self.model = Qwen3VLForEmbedding.from_pretrained(
            MODEL_PATH,
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
            device_map={"": 0},
            local_files_only=True,
            low_cpu_mem_usage=True,
        ).eval()
        self.processor = Qwen3VLProcessor.from_pretrained(
            MODEL_PATH, padding_side="right", local_files_only=True
        )

    def encode(self, texts: list[str], dimensions: int) -> tuple[np.ndarray, int]:
        conversations = [
            [
                {"role": "system", "content": [{"type": "text", "text": INSTRUCTION}]},
                {"role": "user", "content": [{"type": "text", "text": value}]},
            ]
            for value in texts
        ]
        prompts = self.processor.apply_chat_template(
            conversations, add_generation_prompt=True, tokenize=False
        )
        inputs = self.processor(
            text=prompts,
            images=None,
            videos=None,
            truncation=True,
            max_length=MAX_TOKENS,
            padding=True,
            return_tensors="pt",
        )
        token_count = int(inputs["attention_mask"].sum())
        inputs = {key: value.to(self.model.device) for key, value in inputs.items()}
        with self.torch.inference_mode():
            hidden = self.model(**inputs).last_hidden_state
            mask = inputs["attention_mask"]
            last_position = mask.shape[1] - mask.flip(dims=[1]).argmax(dim=1) - 1
            rows = self.torch.arange(hidden.shape[0], device=hidden.device)
            vectors = hidden[rows, last_position, :dimensions].float()
            vectors = self.torch.nn.functional.normalize(vectors, p=2, dim=-1)
        return vectors.cpu().numpy(), token_count


def prepare_request(request: EmbeddingRequest) -> tuple[list[str], int]:
    if request.model != MODEL_ID:
        raise HTTPException(status_code=400, detail="Unknown embedding model")
    texts = [request.input] if isinstance(request.input, str) else request.input
    if not texts or len(texts) > MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"Batch size must be 1–{MAX_BATCH}")
    if any(not text.strip() for text in texts):
        raise HTTPException(status_code=400, detail="Input text must be nonempty")
    return texts, request.dimensions or EMBEDDING_DIM


def format_response(
    vectors: np.ndarray, token_count: int, encoding_format: Literal["float", "base64"]
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


def create_app(encoder_factory: Callable[[], TransformerTextEncoder] | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.encoder = (
            encoder_factory() if encoder_factory is not None else TransformerTextEncoder()
        )
        app.state.inference_lock = asyncio.Lock()
        yield

    app = FastAPI(title="Local Qwen3-VL embedding", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok", "model": MODEL_ID, "dimensions": EMBEDDING_DIM}

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
            logger.exception("Qwen embedding inference failed")
            raise HTTPException(status_code=500, detail="Embedding inference failed") from exc

        if vectors.shape != (len(texts), dimensions):
            raise HTTPException(status_code=500, detail="Unexpected embedding shape")
        return format_response(vectors, token_count, request.encoding_format)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9622, workers=1)
