"""Check request validation and OpenAI-compatible embedding serialization."""

import base64
import unittest

import numpy as np
from fastapi import HTTPException

from scripts.qwen_vl_embedding_server import (
    EMBEDDING_DIM,
    MODEL_ID,
    EmbeddingRequest,
    format_response,
    prepare_request,
)


class EmbeddingServiceContractTests(unittest.TestCase):
    def test_float_and_base64_responses_preserve_order_and_shape(self):
        vectors = np.zeros((2, 64), dtype=np.float32)
        vectors[:, 0] = 1.0
        result = format_response(vectors, 12, "float")
        self.assertEqual([row["index"] for row in result["data"]], [0, 1])
        self.assertEqual(len(result["data"][0]["embedding"]), 64)
        self.assertEqual(result["usage"]["total_tokens"], 12)

        encoded = format_response(vectors, 12, "base64")["data"][0]["embedding"]
        decoded = np.frombuffer(base64.b64decode(encoded), dtype="<f4")
        self.assertEqual(decoded.shape, (64,))
        self.assertEqual(decoded[0], 1.0)

    def test_model_batch_and_dimension_validation(self):
        texts, dimensions = prepare_request(
            EmbeddingRequest(model=MODEL_ID, input="text")
        )
        self.assertEqual(texts, ["text"])
        self.assertEqual(dimensions, EMBEDDING_DIM)

        for payload in (
            {"model": "wrong", "input": "text"},
            {"model": MODEL_ID, "input": ["x"] * 5},
            {"model": MODEL_ID, "input": [" "]},
        ):
            with self.subTest(payload=payload), self.assertRaises(HTTPException):
                prepare_request(EmbeddingRequest(**payload))


if __name__ == "__main__":
    unittest.main()
