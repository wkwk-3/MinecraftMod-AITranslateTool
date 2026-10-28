"""Index-Translate 専用の依頼形式 (instTrans) と、翻訳モデルを Index-Translate に限定する設定。"""
import json
import re
import zipfile

from mctrans import app as A, translator as tr
from test_app import LIB, MAIN, client, upload, wait_job  # noqa: F401


def insttrans_respond(fake):
    """偽の Index-Translate: 【源文】の JSON を訳して返す。system は受け取らない前提。"""
    def respond(body):
        msgs = body["messages"]
        fake.calls.append({"system": "", "user": msgs[-1]["content"], "body": body})
        assert all(m["role"] != "system" for m in msgs), "instTrans では system を送らない"
        user = msgs[-1]["content"]
        if "【源文】" in user:
            src = json.loads(user.split("【源文】\n", 1)[1].split("\n\n【约束要求】", 1)[0])
            out = {}
            for k, v in src.items():
                if "stay_english" in k:
                    out[k] = v                                   # 英語のまま返す
                elif "bad_term" in k:
                    out[k] = "まちがった" + v                     # 用語集に従わない
                else:
                    out[k] = "訳「" + v + "」"
            return json.dumps(out, ensure_ascii=False)
        # 1 項目だけの基本形式
        text = user.split("：\n\n", 1)[1]
        return "単体訳「" + text + "」"
    return respond


def test_prompt_format():
    p = tr.build_insttrans_prompt("Magic", {"item.m.wand": "Deals ⟦0⟧ damage"}, {"Wand": "杖"}, {"a": "前"},
                                  {"Wand": "武器"}, "original", "敬体")
    assert p.startswith("请将以下Minecraft模组《Magic》的JSON语言文件翻译成日语")
    assert "【源文】" in p and '"Deals {v0} damage"' in p and "⟦0⟧" not in p
    assert "【硬性要求】保留占位符 {v0}" in p and "术语映射：Wand -> 杖（武器）" in p
    assert "保留英文原文" in p and "敬体" in p and p.rstrip().endswith("只输出译文，不要有任何额外说明。")
    assert tr.from_v_markers("A {v0} B {v12}") == "A ⟦0⟧ B ⟦12⟧"


def test_helpers():
    assert tr.looks_untranslated("Add Pocket Slot", "Add Pocket Slot")
    assert not tr.looks_untranslated("Add Pocket Slot", "ポケットスロットの追加")
    assert not tr.looks_untranslated("⟦0⟧", "⟦0⟧") and not tr.looks_untranslated("modid:item", "modid:item")
    assert tr.glossary_violations("Show &bPocket&r Slot", "ポークを表示", {"Pocket": "ポケット"}) == ["Pocket -> ポケット"]
    assert tr.glossary_violations("Show Pocket Slot", "ポケットを表示", {"Pocket": "ポケット"}) == []
    assert tr.glossary_violations("Pockets", "x", {"Pocket": "ポケット"}) == []      # 単語の一部は対象外
    assert tr.is_insttrans_model("IndexTeam/Index-Translate-2B-GGUF") and not tr.is_insttrans_model("Qwen/Qwen2.5")


def test_translator_restricted_to_index(client):
    r = client.post("/api/models/config", json={"role": "translator", "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF"})
    assert r.status_code == 400 and "Index-Translate" in r.json()["detail"]
    r = client.post("/api/models/from_url", json={"url": "https://huggingface.co/Qwen/Q-GGUF/resolve/main/q.gguf"})
    assert r.status_code == 400
    assert client.post("/api/models/config", json={"role": "translator",
                                                   "repo": "IndexTeam/Index-Translate-2B-GGUF"}).status_code == 200
    assert client.post("/api/models/config", json={"role": "helper", "repo": "Qwen/x"}).status_code == 200  # 補助は自由
    presets = client.get("/api/models/presets").json()["presets"]
    assert all("Index-Translate" in p["repo"] for p in presets["translator"] if not p.get("src"))
    assert presets["helper"][0]["repo"] == ""                                   # 補助は既定で使わない


def test_translation_uses_insttrans(client, fake_llm, monkeypatch):
    monkeypatch.setattr(fake_llm, "respond", insttrans_respond(fake_llm))
    jar = __import__("test_app").jar_bytes({
        "fabric.mod.json": {"id": "pk", "name": "PK"},
        "assets/pk/lang/en_us.json": {"pk.slot.pocket": "Pocket", "pk.a": "Deals %s damage",
                                      "pk.stay_english": "Keep this English",
                                      **{f"pk.n{i}": f"Line number {i}" for i in range(10)},
                                      "pk.bad_term.x": "Open the Pocket"}})   # 用語登録より後のまとまり
    upload(client, ("pk.jar", jar))
    client.post("/api/models/config", json={"role": "translator", "url": fake_llm.url})   # 既定 = instTrans
    client.post("/api/models/config", json={"role": "helper", "repo": ""})
    client.post("/api/translate", json={"uids": ["pk.jar"]})
    j = wait_job(client)
    assert j["error"] is None, A.S.logs[-10:]
    out = json.loads(zipfile.ZipFile(A.OUT_DIR / j["result"]).read("assets/pk/lang/ja_jp.json"))
    assert out["pk.slot.pocket"] == "訳「Pocket」"
    assert out["pk.a"] == "訳「Deals %s damage」"                                # {v0} を通して往復
    assert out["pk.stay_english"] == "単体訳「Keep this English」"              # 英語のまま → 1 項目で再依頼
    assert out["pk.bad_term.x"].startswith("単体訳")                              # 用語集違反 → 再依頼
    bodies = [c["body"] for c in fake_llm.calls if "【源文】" in c["user"]]
    assert all(b["temperature"] == 0 and b["chat_template_kwargs"] == {"enable_thinking": False} for b in bodies[:1])
    assert max(len(re.findall(r'^ "pk\.', c["user"], re.M)) for c in fake_llm.calls if "【源文】" in c["user"]) \
        <= A.INSTTRANS_CHUNK_ITEMS                                                # 少しずつ依頼する
    assert A.S.memory.glossary.get("Pocket") == "訳「Pocket」"                  # 短い名前は用語集へ
    later = [c["user"] for c in fake_llm.calls if "Open the Pocket" in c["user"]]
    assert later and "Pocket -> 訳「Pocket」" in later[0]


def test_memory_drops_untranslated(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"tm": {"Add Pocket Slot": "Add Pocket Slot", "Wand": "杖", "%s": "%s"},
                             "glossary": {"Pocket": "Pocket", "Altar": "祭壇"}}), "utf-8")
    m = tr.Memory(p)
    assert m.tm == {"Wand": "杖", "%s": "%s"} and m.glossary == {"Altar": "祭壇"}
