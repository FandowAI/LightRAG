# Local Qwen3-VL-Embedding-8B for LightRAG

This installation uses Transformers and the weights already present at
`/data/Qwen3-VL-Embedding-8B`. It serves text embeddings through a loopback
OpenAI-compatible endpoint on port 9622. The launcher selects the sixth
physical GPU (`CUDA_VISIBLE_DEVICES=5`), visible to PyTorch as `cuda:0`.

The Python environment is `/root/MasterRAG/.qwen-vl-venv`. It inherits the
already-installed PyTorch, Transformers, FastAPI, and Uvicorn packages from
`/usr/local/miniforge3/envs/FT`. The endpoint uses Qwen's Transformers model
wrapper, chat template, last-token pooling, and vector normalization. It needs
no vLLM process or `qwen-vl-utils` for LightRAG's text inputs.

Run the embedding service in one terminal:

```bash
cd /root/MasterRAG/LightRAG
./scripts/run_qwen_vl_embedding.sh
```

Wait for model loading to complete. Then, in another terminal:

```bash
curl -fsS http://127.0.0.1:9622/health
curl -fsS http://127.0.0.1:9622/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen3-VL-Embedding-8B","input":["LightRAG embedding smoke test"],"encoding_format":"base64","dimensions":4096}' \
  | /root/MasterRAG/.qwen-vl-venv/bin/python -c 'import json,sys,base64; r=json.load(sys.stdin); print(r["model"],len(base64.b64decode(r["data"][0]["embedding"]))//4)'
```

The smoke check must print `Qwen3-VL-Embedding-8B 4096`. LightRAG's local
`.env` points `EMBEDDING_BINDING_HOST` at this service and uses the same model
name and dimension. Start the storage stack and LightRAG API after the
embedding check. The LLM is separate and still needs valid configuration.

The endpoint accepts strings only, which is the format LightRAG's OpenAI
embedding binding sends. Using the model for direct image or video indexing
requires a separate multimodal integration. If a LightRAG workspace already
contains vectors from another embedding model, re-index those documents in a
new workspace before querying with this one.
