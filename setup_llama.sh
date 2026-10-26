#!/usr/bin/env bash
# llama.cpp (llama-server) を CUDA 有効でビルドする。Colab / Linux 用。
# 最新アーキテクチャ (Index-Translate 等の新しい MoE) に対応するため常に最新ソースを使う。
# llama-server 1 ファイルだけで動くよう、ライブラリは静的リンクする (Google ドライブへの保存・復元のため)。
set -e
DIR="${1:-/content/llama.cpp}"
BIN="$DIR/build/bin/llama-server"

if [ -x "$BIN" ]; then
  if "$BIN" --version >/dev/null 2>&1; then
    echo "ビルド済み: $BIN"
    exit 0
  fi
  echo "既存の llama-server が起動できないため、ビルドし直します"
  "$BIN" --version 2>&1 | head -3 || true
  rm -f "$BIN"
fi

if [ ! -d "$DIR/.git" ]; then
  # キャッシュから復元した build/bin だけがある場合もあるので、一時フォルダに取得して重ねる
  rm -rf "$DIR.src"
  git clone --depth 1 https://github.com/ggml-org/llama.cpp "$DIR.src"
  mkdir -p "$DIR"
  cp -r "$DIR.src/." "$DIR/"
  rm -rf "$DIR.src"
fi
ARCH=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.')
CUDA_FLAGS="-DGGML_CUDA=ON"
[ -n "$ARCH" ] && CUDA_FLAGS="$CUDA_FLAGS -DCMAKE_CUDA_ARCHITECTURES=$ARCH"
command -v nvcc >/dev/null || [ -x /usr/local/cuda/bin/nvcc ] || CUDA_FLAGS="-DGGML_CUDA=OFF"
export PATH=/usr/local/cuda/bin:$PATH
if [[ "$CUDA_FLAGS" == *"GGML_CUDA=ON"* ]]; then
  echo "CUDA: 有効 (GPU アーキテクチャ: ${ARCH:-自動})"
else
  echo "警告: nvcc が見つからないため CUDA なしでビルドします。翻訳は CPU で動き、非常に遅くなります。"
fi
cmake -S "$DIR" -B "$DIR/build" $CUDA_FLAGS -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build "$DIR/build" --target llama-server -j"$(nproc)"

if ! "$BIN" --version >/dev/null 2>&1; then
  echo "ビルドした llama-server を起動できません:"
  "$BIN" --version 2>&1 | head -5
  exit 1
fi
echo "ビルドしました: $BIN"
