from mctrans import modscan


def test_fabric_with_comments_and_trailing_comma(jar_factory):
    p = jar_factory("a.jar", {
        "fabric.mod.json": '{\n // comment\n "id": "magic", "name": "Magic", "version": "1.0",\n'
                           ' "depends": {"fabricloader": "*", "minecraft": "*", "corelib": "*",},\n}',
        "assets/magic/lang/en_us.json": {"item.magic.wand": "Wand", "x": "Y"},
        "assets/magic/lang/ja_jp.json": {"x": "わい"},
    })
    m = modscan.scan_jar(p, "a.jar")
    assert m.loader == "fabric" and m.mod_ids == ["magic"] and m.name == "Magic"
    assert m.depends == ["corelib"]
    assert m.source_entries == 2 and m.has_ja


def test_forge_mods_toml_required_only(jar_factory):
    toml = '''
modLoader="javafml"
[[mods]]
modId="forgemod"
displayName="Forge Mod"
version="${file.jarVersion}"
description=\'\'\'A mod\'\'\'
[[dependencies.forgemod]]
modId="forge"
mandatory=true
[[dependencies.forgemod]]
modId="geckolib"
mandatory=true
[[dependencies.forgemod]]
modId="jei"
mandatory=false
'''
    p = jar_factory("f.jar", {"META-INF/mods.toml": toml, "assets/forgemod/lang/en_us.json": {"a": "b"}})
    m = modscan.scan_jar(p, "f.jar")
    assert m.loader == "forge" and m.mod_ids == ["forgemod"]
    assert m.version == ""          # ${...} は未解決なので空
    assert m.depends == ["geckolib"]


def test_neoforge_type_field(jar_factory):
    toml = '''
[[mods]]
modId="neo"
[[dependencies.neo]]
modId="curios"
type="required"
[[dependencies.neo]]
modId="jade"
type="optional"
'''
    m = modscan.scan_jar(jar_factory("n.jar", {"META-INF/neoforge.mods.toml": toml}), "n.jar")
    assert m.loader == "neoforge" and m.depends == ["curios"]


def test_quilt(jar_factory):
    q = {"quilt_loader": {"id": "qm", "version": "2", "metadata": {"name": "Quilt Mod"},
                          "depends": ["quilt_loader", {"id": "qsl"}]}}
    m = modscan.scan_jar(jar_factory("q.jar", {"quilt.mod.json": q}), "q.jar")
    assert m.mod_ids == ["qm"] and m.name == "Quilt Mod" and m.depends == ["qsl"]


def test_legacy_mcmod_info_and_lang_format(jar_factory):
    info = [{"modid": "oldmod", "name": "Old Mod", "version": "1.7", "requiredMods": ["baubles@[1.0,)"]}]
    lang = "# comment\nitem.old.name=Old Thing\ntile.x.name=Has = sign\n\nbroken line\n"
    p = jar_factory("o.jar", {"mcmod.info": info, "assets/oldmod/lang/en_US.lang": lang})
    m = modscan.scan_jar(p, "o.jar")
    assert m.loader == "forge-legacy" and m.depends == ["baubles"] and m.source_entries == 2
    data = modscan.load_lang_entries(p, m)["oldmod"]
    assert data["format"] == "lang" and data["entries"]["tile.x.name"] == "Has = sign"


def test_no_metadata_falls_back(jar_factory):
    m = modscan.scan_jar(jar_factory("weird-Mod.jar", {"assets/wm/lang/en_us.json": {"a": "b"}}), "u")
    assert m.mod_ids == ["wm"]
    m2 = modscan.scan_jar(jar_factory("Nothing.jar", {"x.txt": "hi"}), "u2")
    assert m2.mod_ids == ["nothing"] and m2.source_entries == 0


def test_multiple_namespaces_and_bom(jar_factory):
    p = jar_factory("m.jar", {
        "fabric.mod.json": {"id": "m"},
        "assets/m/lang/en_us.json": "﻿" + '{"a":"A","n":1}',
        "assets/m_extra/lang/en_us.json": {"b": "B"},
    })
    m = modscan.scan_jar(p, "m.jar")
    assert set(m.lang_files) == {"m", "m_extra"} and m.source_entries == 2  # 非文字列値は除外


def _mods(spec):
    return {uid: modscan.ModInfo(uid=uid, filename=uid, mod_ids=[mid], depends=deps) for uid, (mid, deps) in spec.items()}


def test_resolve_order_missing_and_cycle():
    mods = _mods({"a": ("a", ["b", "ghost"]), "b": ("b", ["c"]), "c": ("c", []), "x": ("x", ["y"]), "y": ("y", ["x"])})
    order, missing = modscan.resolve_dependencies(["a"], mods)
    assert order == ["c", "b", "a"] and missing == ["ghost"]
    order, _ = modscan.resolve_dependencies(["x", "a", "a"], mods)   # 循環・重複でも停止
    assert sorted(order) == ["a", "b", "c", "x", "y"] and len(order) == 5
    assert modscan.resolve_dependencies(["nope"], mods) == ([], [])
