#!/usr/bin/env bash
# llama.cpp (llama-server) を CUDA 有効でビルドする。Colab / Linux 用。
# 最新アーキテクチャ (Index-Translate 等の新しい MoE) に対応するため常に最新ソースを使う。
set -e
DIR="${1:-/content/llama.cpp}"
if [ -x "$DIR/build/bin/llama-server" ]; then echo "already built: $DIR"; exit 0; fi
[ -d "$DIR" ] || git clone --depth 1 https://github.com/ggml-org/llama.cpp "$DIR"
ARCH=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.')
CUDA_FLAGS="-DGGML_CUDA=ON"
[ -n "$ARCH" ] && CUDA_FLAGS="$CUDA_FLAGS -DCMAKE_CUDA_ARCHITECTURES=$ARCH"
command -v nvcc >/dev/null || [ -x /usr/local/cuda/bin/nvcc ] || CUDA_FLAGS="-DGGML_CUDA=OFF"
export PATH=/usr/local/cuda/bin:$PATH
cmake -S "$DIR" -B "$DIR/build" $CUDA_FLAGS -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$DIR/build" --target llama-server -j"$(nproc)"
echo "built: $DIR/build/bin/llama-server"
