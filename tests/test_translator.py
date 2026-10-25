import json
import zipfile

import pytest

from mctrans import translator as tr


@pytest.mark.parametrize("src", [
    "Deals %1$s damage to {target}",
    "%d%% chance, %.2f speed, %s",
    "Press [123456] to §cexplode§r",
    "Line one\nLine two",
    "Value ${amount} via $(item)",
    "Use <br> here and &aGreen",
    "{0} and {player_name} and {}",
])
def test_protect_restore_roundtrip(src):
    p, found = tr.protect(src)
    assert found, "placeholder not detected"
    assert not any(f in p for f in found)
    assert tr.restore(p, found) == src


def test_restore_allows_reordering_but_not_loss_or_dup():
    p, f = tr.protect("%s gave %s")
    assert tr.restore("⟦1⟧が⟦0⟧をくれた", f) == "%s が %s をくれた".replace(" ", "")
    assert tr.restore("⟦0⟧がくれた", f) is None
    assert tr.restore("⟦0⟧⟦0⟧⟦1⟧", f) is None
    assert tr.restore("⟦0⟧⟦1⟧⟦2⟧", f) is None


def test_plain_text_has_no_placeholders():
    assert tr.protect("Magic Wand") == ("Magic Wand", [])
    # 角括弧の中が数字でないものは対象外
    assert tr.protect("[Shift] for info")[1] == []


@pytest.mark.parametrize("src,dst,ok", [
    ("Run `cmd`", "`cmd` を実行", True),
    ("Run `cmd`", "cmd を実行", False),
    ("```\ncode\n```", "```\ncode\n```", True),
    ('{"a": "b"}', '{"a": "び"}', True),
    ('{"a": "b"}', "{a: び}", False),
    ("| a | b |", "| あ | い |", True),
    ("| a | b |", "あ い", False),
    ("a,b,c,d\ne,f,g,h", "あ,い,う,え\nお,か,き,く", True),
    ("a,b,c,d\ne,f,g,h", "あ、い、う、え\nお", False),
    ("{not json", "{じぇいそんでない", True),
])
def test_structure_ok(src, dst, ok):
    assert tr.structure_ok(src, dst) is ok


@pytest.mark.parametrize("src,skip", [
    ("", True), ("%s", True), ("12345", True), ("§a%s§r", True), ("textures/x.png", True),
    ("Hello", False), ("%s Hello", False),
])
def test_should_skip(src, skip):
    assert tr.should_skip(src) is skip


def test_chunking_keeps_order_and_limits():
    items = [(f"k{i}", "x" * 100) for i in range(25)]
    chunks = tr.chunk_entries(items, max_items=10, max_chars=10_000)
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert [k for c in chunks for k, _ in c] == [k for k, _ in items]
    assert all(len(c) <= 4 for c in tr.chunk_entries(items, 50, 450))
    assert tr.chunk_entries([("big", "y" * 9999)], 10, 100) == [[("big", "y" * 9999)]]


def test_memory_terms_priority_and_word_boundary(tmp_path):
    m = tr.Memory(tmp_path / "mem.json")
    m.glossary["Mana"] = "魔力"
    m.user_glossary["Mana"] = "マナ"
    terms = m.relevant_terms(["Uses Mana and an Iron Pickaxe", "Environment"])
    assert terms["Mana"] == "マナ" and terms["Iron"] == "鉄" and terms["Pickaxe"] == "ツルハシ"
    assert "End" not in terms               # Environment に誤マッチしない
    m.tm["Hello"] = "こんにちは"
    m.save()
    m2 = tr.Memory(tmp_path / "mem.json")
    assert m2.tm == {"Hello": "こんにちは"} and m2.user_glossary == {"Mana": "マナ"}


def test_prompt_is_markdown_with_all_rules():
    p = tr.build_user_prompt("Magic", "desc", {"a": "Wand ⟦0⟧"}, {"Wand": "杖"}, {"prev": "前"}, {"Wand": "杖"})
    for h in ["# MOD 情報", "# 用語集", "| Wand | 杖 |", "# 過去の翻訳例", "# 直前の翻訳", "# 翻訳対象", "```json"]:
        assert h in p
    s = tr.SYSTEM_PROMPT
    for rule in ["用語集の強制適用", "プレースホルダ", "構造化データ", "Minecraft らしさ", "自然な日本語", "表記揺れ", "連続性"]:
        assert rule in s


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure! {"a": "b"} done', {"a": "b"}),
    ("nothing", None),
    ("{broken", None),
])
def test_parse_json_obj(text, expected):
    assert tr.parse_json_obj(text) == expected


def test_resource_pack(tmp_path):
    out = tmp_path / "p.zip"
    tr.build_resource_pack({
        "modern": {"format": "json", "entries": {"a": "あ", "b": "改行\nあり"}},
        "old": {"format": "lang", "entries": {"x": "えっくす"}},
    }, out, 34, "テスト")
    z = zipfile.ZipFile(out)
    meta = json.loads(z.read("pack.mcmeta"))
    assert meta["pack"]["pack_format"] == 34 and meta["pack"]["description"] == "テスト"
    assert json.loads(z.read("assets/modern/lang/ja_jp.json").decode()) == {"a": "あ", "b": "改行\nあり"}
    assert z.read("assets/old/lang/ja_JP.lang").decode() == "x=えっくす"
    assert set(json.loads(z.read("translations_all.json"))) == {"modern", "old"}
