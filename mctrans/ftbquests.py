"""FTB Quests の言語ファイル (quests/lang/*.snbt) の読み書き・翻訳エントリ抽出・出力。

FTB Quests 1.20.1 以降の構成:
  quests/lang/en_us.snbt                       全テキストを 1 ファイルにまとめたもの
  quests/lang/en_us/chapters/<章>.snbt(_merged)  章ごとに分割したもの (読み込み後 _merged に改名される)
  quests/lang/en_us/{chapter,chapter_group,file,image,reward,reward_table}.snbt(_merged)
値は "文字列" か ["行", "行", ...] (説明文の各行)。
出力は en_us → ja_jp に置き換えた同じ構成 (ファイル名もそのまま) で作る。
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

LANG_FILE_RE = re.compile(r"(^|/)lang/en_us\.snbt$")
LANG_DIR_RE = re.compile(r"(^|/)lang/en_us/.+\.snbt(_merged)?$")


class SnbtError(ValueError):
    pass


# ---------------------------------------------------------------- SNBT (lang 用のサブセット)
def parse_lang_snbt(text: str) -> dict[str, str | list[str]]:
    """{ key: "str"  key: ["a", "b"] } 形式を順序付き dict にする。キーは引用符なしでも可。"""
    i, n = 0, len(text)
    text = text.lstrip("﻿")

    def skip():
        nonlocal i
        while i < n:
            c = text[i]
            if c in " \t\r\n,":
                i += 1
            elif text.startswith("//", i) or c == "#":
                while i < n and text[i] != "\n":
                    i += 1
            else:
                break

    def string() -> str:
        nonlocal i
        q = text[i]
        i += 1
        out = []
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])        # SNBT のエスケープは \" \' \\ のみ
                i += 2
                continue
            if c == q:
                i += 1
                return "".join(out)
            out.append(c)
            i += 1
        raise SnbtError("文字列が閉じていません")

    def key() -> str:
        nonlocal i
        if text[i] in "\"'":
            return string()
        m = re.compile(r"[^\s:{}\[\],\"']+").match(text, i)
        if not m:
            raise SnbtError(f"キーを読めません (位置 {i})")
        i = m.end()
        return m.group(0)

    skip()
    if i >= n or text[i] != "{":
        raise SnbtError("先頭が { ではありません")
    i += 1
    data: dict[str, str | list[str]] = {}
    while True:
        skip()
        if i >= n:
            raise SnbtError("} がありません")
        if text[i] == "}":
            return data
        k = key()
        skip()
        if i >= n or text[i] != ":":
            raise SnbtError(f"':' がありません: {k}")
        i += 1
        skip()
        if i < n and text[i] in "\"'":
            data[k] = string()
        elif i < n and text[i] == "[":
            i += 1
            items = []
            while True:
                skip()
                if i < n and text[i] == "]":
                    i += 1
                    break
                if i >= n or text[i] not in "\"'":
                    raise SnbtError(f"リストに文字列以外があります: {k}")
                items.append(string())
            data[k] = items
        else:
            raise SnbtError(f"値を読めません: {k}")


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_lang_snbt(data: dict[str, str | list[str]]) -> str:
    """FTB Quests と同じ書式 (タブインデント・1 要素リストは 1 行) で書き出す。"""
    lines = ["{"]
    for k, v in data.items():
        if isinstance(v, list):
            if len(v) <= 1:
                lines.append(f"\t{k}: [{', '.join(_q(x) for x in v)}]")
            else:
                lines.append(f"\t{k}: [")
                lines += [f"\t\t{_q(x)}" for x in v]
                lines.append("\t]")
        else:
            lines.append(f"\t{k}: {_q(v)}")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 翻訳エントリ
def _json_component(s: str) -> dict | None:
    t = s.strip()
    if not (t.startswith("{") and t.endswith("}")) or '"text"' not in t:
        return None
    try:
        d = json.loads(t)
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("text"), str) else None


def is_translatable(s: str) -> bool:
    t = s.strip()
    if not t or t == "{@pagebreak}" or re.fullmatch(r"\{image:[^{}]*\}", t):
        return False
    return bool(re.search(r"[A-Za-z]{2}", re.sub(r"&[0-9a-fk-or]|\\[n&\\]", "", t)))


def extract_entries(data: dict, check=is_translatable) -> dict[str, str]:
    """{エントリキー: 原文}。リストは key[i]、JSON テキストコンポーネントは key[i].text。"""
    out = {}
    for k, v in data.items():
        items = [(k, v)] if isinstance(v, str) else [(f"{k}[{i}]", x) for i, x in enumerate(v)]
        for ek, s in items:
            comp = _json_component(s)
            if comp is not None:
                if check(comp["text"]):
                    out[ek + ".text"] = comp["text"]
            elif check(s):
                out[ek] = s
    return out


def apply_entries(data: dict, translations: dict[str, str]) -> dict:
    """原文 data に訳を当てはめた新しい dict (訳がないものは原文)。"""
    out = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = translations.get(k, v)
            continue
        new = []
        for i, s in enumerate(v):
            ek = f"{k}[{i}]"
            comp = _json_component(s)
            if comp is not None and ek + ".text" in translations:
                comp["text"] = translations[ek + ".text"]
                new.append(json.dumps(comp, ensure_ascii=False))
            else:
                new.append(translations.get(ek, s))
        out[k] = new
    return out


def existing_entries(src: dict, ja: dict) -> dict[str, str]:
    """既存 ja_jp のうち、英語原文と構造が一致し実際に訳されているもの。"""
    src_e = extract_entries(src)
    ja_e = extract_entries({k: v for k, v in ja.items() if k in src}, check=lambda s: bool(s.strip()))
    return {k: v for k, v in ja_e.items() if k in src_e and v.strip() and v != src_e[k]}


# ---------------------------------------------------------------- ファイル構成
def find_documents(paths: list[str]) -> list[dict]:
    """アップロードされたパス一覧から en_us の言語ファイルを探す。
    章ごとのファイルを先に (文脈がまとまる)、全体ファイル en_us.snbt を最後に並べる。"""
    docs = []
    for p in sorted(paths):
        if LANG_DIR_RE.search(p) or LANG_FILE_RE.search(p):
            out = re.sub(r"(^|/)lang/en_us(?=\.snbt$|/)", r"\1lang/ja_jp", p)
            ja_src = out if out in paths else None
            docs.append({"path": p, "out": out, "ja": ja_src, "whole": bool(LANG_FILE_RE.search(p)),
                         "label": PurePosixPath(p).name.split(".")[0]})
    return sorted(docs, key=lambda d: (d["whole"], "/chapters/" not in d["path"], d["path"]))


FMT_EDGE_RE = re.compile(r"^((?:&[0-9a-fk-or]|\s)*)(.*?)((?:&[0-9a-fk-or]|\s)*)$", re.S | re.I)


def name_match(text: str, names: dict[str, str]) -> str | None:
    """文字列全体がアイテム/ブロック名 (前後の書式コードは除く) なら日本語名に置き換えた文字列。"""
    m = FMT_EDGE_RE.match(text)
    core = m.group(2)
    if core in names:
        return m.group(1) + names[core] + m.group(3)
    return None


def normalize_out_path(p: str) -> str:
    """出力 zip 内のパス。インスタンスのフォルダに展開できるよう config/ftbquests/quests/lang/... に揃える。"""
    i = p.find("lang/ja_jp")
    return "config/ftbquests/quests/" + p[i:]


def chapter_title(data: dict) -> str | None:
    for k, v in data.items():
        if re.fullmatch(r"chapter\.[0-9A-Fa-f]+\.title", k) and isinstance(v, str):
            return v
    return None
