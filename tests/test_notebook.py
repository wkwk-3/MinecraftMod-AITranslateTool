"""Colab ノートブックの設定: llama.cpp をドライブに保存するかどうか。"""
import json
import os
import re
import shutil
import types

import pytest

from conftest import ROOT

NB = json.load(open(ROOT / "MC_Mod_Translator.ipynb", encoding="utf-8"))
CELL1 = "".join(NB["cells"][1]["source"])
CELL2 = "".join(NB["cells"][2]["source"])


def test_setting_default_off():
    assert re.search(r'^SAVE_LLAMA_TO_DRIVE = False  #@param \{type:"boolean"\}', CELL1, re.M)
    assert re.search(r'^LLAMA_DRIVE_DIR = "MyDrive/llama"', CELL1, re.M)
    assert "CACHE_LLAMA_TO_DRIVE" not in CELL1 + CELL2


def llama_block():
    """セットアップセルのうち llama-server を用意する部分だけを取り出す。"""
    start = CELL2.index('LLAMA_DIR = "/content/llama.cpp"')
    end = CELL2.index('os.environ["LLAMA_SERVER"] = LLAMA_BIN')
    return CELL2[start:end]


def run_block(tmp_path, save: bool, monkeypatch, prebuilt_works: bool = True):
    content = tmp_path / "content"
    builds = []

    def fake_setup(llama_dir):          # !bash setup_llama.sh の代わり: なければビルドする
        b = os.path.join(llama_dir, "build", "bin", "llama-server")
        if not os.path.exists(b):
            os.makedirs(os.path.dirname(b), exist_ok=True)
            open(b, "w").write("built")
            builds.append(b)

    code = llama_block().replace("/content/", str(content).replace("\\", "/") + "/")
    code = code.replace("!bash setup_llama.sh {LLAMA_DIR}", "fake_setup(LLAMA_DIR)")
    ok = types.SimpleNamespace(returncode=0, stdout="version", stderr="")
    dev = types.SimpleNamespace(returncode=0, stdout="CUDA0: Tesla T4", stderr="")
    bad = types.SimpleNamespace(returncode=1, stdout="", stderr="version `GLIBC_2.38' not found")

    def run(args, **k):
        if "--list-devices" in args:
            return dev
        if "llama-prebuilt" in args[0].replace("\\", "/") and not prebuilt_works:
            return bad
        return ok
    fake_sub = types.SimpleNamespace(run=run)
    monkeypatch.setattr(os, "chmod", lambda *a: None)
    env = {"os": os, "shutil": shutil, "subprocess": fake_sub, "fake_setup": fake_setup,
           "SAVE_LLAMA_TO_DRIVE": save, "LLAMA_DRIVE_DIR": "MyDrive/llama"}
    exec(code, env)
    drive_file = content / "drive/MyDrive/llama/llama-server-static"
    return builds, drive_file, env


def test_off_does_not_touch_drive(tmp_path, monkeypatch):
    builds, drive_file, _ = run_block(tmp_path, False, monkeypatch)
    assert len(builds) == 1 and not drive_file.exists()


def test_on_saves_then_loads_from_drive(tmp_path, monkeypatch):
    builds, drive_file, _ = run_block(tmp_path, True, monkeypatch)
    assert len(builds) == 1 and drive_file.read_text() == "built"        # 初回: ビルドしてドライブに保存
    shutil.rmtree(tmp_path / "content/llama.cpp")                          # 新しいランタイム (ディスクは空)
    builds, drive_file, env = run_block(tmp_path, True, monkeypatch)
    assert builds == []                                                    # 2 回目: ドライブから読み込み、ビルドしない
    assert open(env["LLAMA_BIN"]).read() == "built"


def put_official_packs(tmp_path):
    """ドライブに公式の Linux CUDA 版 (tar.gz) を置いた状態を作る。"""
    import io
    import tarfile
    d = tmp_path / "content/drive/MyDrive/llama"
    d.mkdir(parents=True, exist_ok=True)
    for name, files in [("llama-b1-bin-ubuntu-cuda-12.8-x64", {"llama-server": b"bin", "libllama.so.0": b"so"}),
                        ("cudart-llama-b1-bin-ubuntu-cuda-12.8-x64", {"libcudart.so.12": b"so"})]:
        prefix = name.replace("-bin-ubuntu-cuda-12.8-x64", "") if name.startswith("llama") else name
        with tarfile.open(d / f"{name}.tar.gz", "w:gz") as t:
            for fn, data in files.items():
                info = tarfile.TarInfo(f"{prefix}/{fn}")
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))


def test_official_linux_build_on_drive_is_used(tmp_path, monkeypatch):
    put_official_packs(tmp_path)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    builds, drive_file, env = run_block(tmp_path, True, monkeypatch)
    assert builds == [] and env["USING_PREBUILT"]                          # ビルドしない
    assert env["LLAMA_BIN"].replace("\\", "/").endswith("llama-prebuilt/llama-b1/llama-server")
    assert "llama-prebuilt" in os.environ["LD_LIBRARY_PATH"]               # ライブラリの場所を llama-server に渡す
    assert not drive_file.exists()                                         # 公式版はそのまま。別に保存しない


def test_official_build_not_working_falls_back_to_build(tmp_path, monkeypatch):
    put_official_packs(tmp_path)
    builds, drive_file, env = run_block(tmp_path, True, monkeypatch, prebuilt_works=False)
    assert len(builds) == 1 and not env["USING_PREBUILT"]                  # glibc が合わない等 → ビルドする
    assert drive_file.read_text() == "built"                              # ビルドしたものは保存して次回に使う


def test_official_build_ignored_when_setting_off(tmp_path, monkeypatch):
    put_official_packs(tmp_path)
    builds, _, env = run_block(tmp_path, False, monkeypatch)
    assert len(builds) == 1 and not env["USING_PREBUILT"]
