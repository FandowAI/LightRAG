#!/usr/bin/env bash
set -euo pipefail

cd /root/MasterRAG/LightRAG
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-7}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export QWEN3_EMBED_MODEL_PATH="${QWEN3_EMBED_MODEL_PATH:-/data/models/Qwen3-Embedding-0.6B}"
export QWEN3_EMBED_MODEL_ID="${QWEN3_EMBED_MODEL_ID:-Qwen3-Embedding-0.6B}"
export QWEN3_EMBED_DIM="${QWEN3_EMBED_DIM:-1024}"
export QWEN3_EMBED_MAX_BATCH="${QWEN3_EMBED_MAX_BATCH:-3}"
export QWEN3_EMBED_MAX_TOKENS="${QWEN3_EMBED_MAX_TOKENS:-8192}"
export QWEN3_EMBED_PORT="${QWEN3_EMBED_PORT:-9623}"

exec /root/MasterRAG/.qwen-vl-venv/bin/python \
  scripts/qwen3_embedding_0_6b_server.py
