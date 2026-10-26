import sys

import numpy as np
import pytest

from mctrans import llm

GB = 2**30
INDEX_FILES = [{"file": f"Index-Translate-35B-A3B-preview.{q}.gguf", "size": int(s * GB)} for q, s in [
    ("Q2_K", 12.3), ("Q3_K_S", 14.5), ("Q3_K_M", 16.0), ("Q3_K_L", 17.3), ("IQ4_XS", 18.1),
    ("Q4_K_S", 19.0), ("Q4_K_M", 20.2), ("Q8_0", 35.2)]]


@pytest.mark.parametrize("vram,ram,expected", [
    (0, 0, "Q4_K_M"),                       # GPU 不明 → 既定の優先順
    (80 * GB, 80 * GB, "Q4_K_M"),           # A100 等
    (15 * GB, 11 * GB, "Q3_K_L"),           # 無料 Colab T4: VRAM 13GB + RAM 5GB ≒ 17.7GB
    (8 * GB, 16 * GB, "Q2_K"),              # RTX 4060 + 16GB RAM: 6.8 + 7.2 ≒ 14GB
    (24 * GB, 16 * GB, "Q4_K_M"),           # RTX 4090
    (4 * GB, 2 * GB, "Q2_K"),               # どれも入らない → 最小
])
def test_pick_default_file_fits_memory(vram, ram, expected):
    assert llm.pick_default_file(INDEX_FILES, vram, ram).split(".")[-2] == expected


def test_pick_default_file_edge_cases():
    assert llm.pick_default_file([]) is None
    assert llm.pick_default_file([{"file": "weird.gguf", "size": 5}]) == "weird.gguf"


def _write_gguf(path, arch, layers, experts, pad_bytes):
    import gguf
    w = gguf.GGUFWriter(str(path), arch)
    w.add_block_count(layers)
    if experts:
        w.add_expert_count(experts)
    w.add_tensor("pad", np.zeros(pad_bytes // 4, dtype=np.float32))
    w.write_header_to_file(); w.write_kv_data_to_file(); w.write_tensors_to_file(); w.close()


def test_auto_cpu_moe(tmp_path):
    moe = tmp_path / "moe.gguf"
    _write_gguf(moe, "qwen3moe", 40, 128, 4_000_000)
    size = moe.stat().st_size
    assert llm.auto_cpu_moe(str(moe), vram=0) == 0                    # GPU 不明
    assert llm.auto_cpu_moe(str(moe), vram=size * 2) == 0             # 収まる
    n = llm.auto_cpu_moe(str(moe), vram=int(size * 0.8))              # 少しはみ出す
    assert 0 < n < 40
    assert llm.auto_cpu_moe(str(moe), vram=int(size * 0.1)) == 40     # 大きくはみ出す → 上限
    dense = tmp_path / "dense.gguf"
    _write_gguf(dense, "llama", 32, 0, 4_000_000)
    assert llm.auto_cpu_moe(str(dense), vram=int(size * 0.5)) == 0    # MoE 以外は対象外


def test_server_start_reports_crash(monkeypatch, tmp_path):
    # 起動直後に終了するバイナリ → ハングせず、ログ付きで例外
    monkeypatch.setattr(llm, "find_server_binary", lambda: sys.executable)
    s = llm.LlamaServer("t", 18999)
    with pytest.raises(RuntimeError, match="終了しました") as e:
        s.start(str(tmp_path / "no_such_module"), timeout=30)
    assert not s.running()
    assert "終了コード" in str(e.value) and "No module named" in str(e.value)   # 終了直前の出力 (理由) を含む


def test_server_crash_without_output_is_diagnosed(monkeypatch, tmp_path):
    # 出力を読む前に終了した場合 (共有ライブラリ不足など) は、単体で実行して理由を得る
    monkeypatch.setattr(llm, "find_server_binary", lambda: sys.executable)
    monkeypatch.setattr(llm.LlamaServer, "_pump", lambda self: None)
    monkeypatch.setattr(llm.LlamaServer, "_diagnose", staticmethod(
        lambda b: "error while loading shared libraries: libllama.so"))
    with pytest.raises(RuntimeError, match="libllama.so"):
        llm.LlamaServer("t", 18997).start(str(tmp_path / "no_such_module"), timeout=30)


def test_diagnose_reports_output():
    msg = llm.LlamaServer._diagnose(sys.executable)          # python --version は出力あり
    assert "Python" in msg
    assert "実行できません" in llm.LlamaServer._diagnose("/no/such/llama-server")


def test_server_missing_binary(monkeypatch):
    monkeypatch.setattr(llm, "find_server_binary", lambda: None)
    with pytest.raises(RuntimeError, match="llama-server が見つかりません"):
        llm.LlamaServer("t", 18998).start("x.gguf")


def test_presets_default_first():
    assert llm.PRESETS["translator"][0]["repo"] == "IndexTeam/Index-Translate-35B-A3B-preview-GGUF"
    assert llm.PRESETS["helper"][0]["repo"] == llm.DEFAULT_HELPER_REPO
