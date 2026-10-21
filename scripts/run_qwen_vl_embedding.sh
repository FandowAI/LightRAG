#!/usr/bin/env bash
set -euo pipefail

cd /root/MasterRAG/LightRAG
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=5
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export QWEN_VL_MODEL_PATH=/data/Qwen3-VL-Embedding-8B

exec /root/MasterRAG/.qwen-vl-venv/bin/python scripts/qwen_vl_embedding_server.py
