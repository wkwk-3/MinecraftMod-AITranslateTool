"""MOD (jar) の解析: メタデータ・依存関係・lang ファイル抽出。"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

# 依存として無視する ID (ローダー・本体)
IGNORED_DEPS = {
    "minecraft", "java", "fabricloader", "fabric-loader", "forge", "neoforge",
    "quilt_loader", "quilt_base", "fml", "javafml", "mcp",
}

LANG_RE = re.compile(r"^assets/([^/]+)/lang/([A-Za-z_]+)\.(json|lang)$")


@dataclass
class ModInfo:
    uid: str                     # アップロード時のファイル名ベースの一意ID
    filename: str
    mod_ids: list[str] = field(default_factory=list)
    name: str = ""
    version: str = ""
    loader: str = "unknown"
    description: str = ""
    depends: list[str] = field(default_factory=list)
    lang_files: dict[str, list[str]] = field(default_factory=dict)  # namespace -> [paths]
    source_entries: int = 0
    has_ja: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_json(text: str) -> str:
    # 一部 MOD の json に含まれるコメント/末尾カンマに耐える
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def read_text(zf: zipfile.ZipFile, path: str) -> str:
    raw = zf.read(path)
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_lang(text: str, fmt: str) -> dict[str, str]:
    if fmt == "json":
        try:
            data = json.loads(_clean_json(text))
        except json.JSONDecodeError:
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str)}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v
    return out


def scan_jar(path: Path, uid: str) -> ModInfo:
    info = ModInfo(uid=uid, filename=path.name)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        if "fabric.mod.json" in names:
            info.loader = "fabric"
            try:
                d = json.loads(_clean_json(read_text(zf, "fabric.mod.json")))
                info.mod_ids.append(d.get("id", ""))
                info.name = d.get("name", "")
                info.version = str(d.get("version", ""))
                desc = d.get("description", "")
                info.description = desc if isinstance(desc, str) else ""
                info.depends += list((d.get("depends") or {}).keys())
            except Exception:
                pass
        if "quilt.mod.json" in names:
            info.loader = "quilt"
            try:
                d = json.loads(_clean_json(read_text(zf, "quilt.mod.json"))).get("quilt_loader", {})
                info.mod_ids.append(d.get("id", ""))
                meta = d.get("metadata", {})
                info.name = info.name or meta.get("name", "")
                info.version = info.version or str(d.get("version", ""))
                for dep in d.get("depends", []):
                    info.depends.append(dep if isinstance(dep, str) else dep.get("id", ""))
            except Exception:
                pass
        for toml_path, loader in (("META-INF/neoforge.mods.toml", "neoforge"), ("META-INF/mods.toml", "forge")):
            if toml_path not in names:
                continue
            info.loader = loader
            try:
                d = tomllib.loads(read_text(zf, toml_path))
                for m in d.get("mods", []):
                    info.mod_ids.append(m.get("modId", ""))
                    info.name = info.name or m.get("displayName", "")
                    v = str(m.get("version", ""))
                    info.version = info.version or ("" if "${" in v else v)
                    info.description = info.description or str(m.get("description", "")).strip()
                for mid, deps in (d.get("dependencies") or {}).items():
                    for dep in deps or []:
                        mandatory = dep.get("mandatory", dep.get("type", "required") == "required")
                        if mandatory:
                            info.depends.append(dep.get("modId", ""))
            except Exception:
                pass
            break
        if "mcmod.info" in names and not info.mod_ids:
            info.loader = "forge-legacy"
            try:
                d = json.loads(_clean_json(read_text(zf, "mcmod.info")))
                lst = d if isinstance(d, list) else d.get("modList", [])
                for m in lst:
                    info.mod_ids.append(m.get("modid", ""))
                    info.name = info.name or m.get("name", "")
                    info.version = info.version or m.get("version", "")
                    info.depends += [x.split("@")[0] for x in m.get("requiredMods", [])]
            except Exception:
                pass

        for n in names:
            m = LANG_RE.match(n)
            if not m:
                continue
            ns, lang, _ = m.groups()
            if lang.lower() == "en_us":
                info.lang_files.setdefault(ns, []).append(n)
            elif lang.lower() == "ja_jp":
                info.has_ja = True
        for ns, paths in info.lang_files.items():
            for p in paths:
                info.source_entries += len(parse_lang(read_text(zf, p), p.rsplit(".", 1)[1]))

    info.mod_ids = [i for i in dict.fromkeys(info.mod_ids) if i]
    if not info.mod_ids:
        info.mod_ids = list(info.lang_files.keys())[:1] or [path.stem.lower()]
    info.name = info.name or info.mod_ids[0]
    info.depends = [d for d in dict.fromkeys(info.depends)
                    if d and d not in IGNORED_DEPS and d not in info.mod_ids]
    return info


def load_lang_entries(path: Path, info: ModInfo) -> dict[str, dict]:
    """namespace -> {"format": json|lang, "entries": {...}, "existing_ja": {...}}"""
    out: dict[str, dict] = {}
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        for ns, paths in info.lang_files.items():
            entries: dict[str, str] = {}
            fmt = "json"
            for p in paths:
                fmt = p.rsplit(".", 1)[1]
                entries.update(parse_lang(read_text(zf, p), fmt))
            existing: dict[str, str] = {}
            for cand in (f"assets/{ns}/lang/ja_jp.{fmt}", f"assets/{ns}/lang/ja_JP.{fmt}"):
                if cand in names:
                    existing = parse_lang(read_text(zf, cand), fmt)
            out[ns] = {"format": fmt, "entries": entries, "existing_ja": existing}
    return out


def resolve_dependencies(selected: list[str], mods: dict[str, ModInfo]) -> tuple[list[str], list[str]]:
    """選択 MOD + アップロード済み前提 MOD を依存順(前提が先)に並べる。
    戻り値: (順序付き uid リスト, 見つからなかった依存 ID)"""
    by_id = {mid: uid for uid, m in mods.items() for mid in m.mod_ids}
    order: list[str] = []
    missing: set[str] = set()
    visiting: set[str] = set()

    def visit(uid: str):
        if uid in order or uid in visiting:
            return
        visiting.add(uid)
        for dep in mods[uid].depends:
            if dep in by_id:
                visit(by_id[dep])
            else:
                missing.add(dep)
        visiting.discard(uid)
        order.append(uid)

    for uid in selected:
        if uid in mods:
            visit(uid)
    return order, sorted(missing)
