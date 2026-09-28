#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
边狱巴士(Limbus Company)自动汉化工具

流程:
  1. scan      对比游戏中 Localize/kr/ 与 Lang/LLC_zh-CN/(零协包未安装时
               全部 KR_ 文件视为未翻译),
               把没有中文对应、或官方包结构落后于韩文的 KR_ 文件复制到 ./untranslated/;
               同时从官方包抽取 NPC/名称译名到 cache/glossary_official.json
  2. translate 将 ./untranslated/ 下的文件用 BYOK 大模型(OpenAI 兼容接口)
               按边狱巴士世界观汉化,输出到 Lang/ai/(去掉 KR_ 前缀)
  3. run       scan + translate

用法:
  python limbus_loc.py scan
  python limbus_loc.py test
  python limbus_loc.py translate [--file 名称子串] [--force] [--cold-cache] [--dry-run]
  python limbus_loc.py run      [--file 名称子串] [--force] [--cold-cache] [--dry-run]
  python limbus_loc.py pack     [--out 路径]   把补译文件打包成可分发 zip
  python limbus_loc.py help     [命令]         显示所有命令与说明

BYOK:在 config.json 的 api.api_key 填入密钥,或设置环境变量 LIMBUS_LLM_KEY。
仅使用 Python 标准库,无需安装任何依赖。
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import copy
import hashlib
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"
CONFIG_EXAMPLE_PATH = PROJECT_DIR / "config.example.json"
GLOSSARY_PATH = PROJECT_DIR / "glossary.json"
KEYS_PATH = PROJECT_DIR / "translatable_keys.json"
CACHE_PATH = PROJECT_DIR / "cache" / "translations.json"
OFFICIAL_GLOSSARY_PATH = PROJECT_DIR / "cache" / "glossary_official.json"
LOG_DIR = PROJECT_DIR / "logs"
UNTRANSLATED_DIR = PROJECT_DIR / "untranslated"
DIST_DIR = PROJECT_DIR / "dist"
PACK_NOTE_NAME = "使用说明.txt"

KR_SUFFIX = Path("LimbusCompany_Data/Assets/Resources_moved/Localize/kr")
ZH_SUFFIX = Path("LimbusCompany_Data/Lang/LLC_zh-CN")
AI_SUFFIX = Path("LimbusCompany_Data/Lang/ai")
GAME_DIRNAME = "Limbus Company"
STEAM_ROOTS = [
    Path(r"C:\Program Files (x86)\Steam"),
    Path(r"D:\Program Files (x86)\Steam"),
    Path(r"E:\Program Files (x86)\Steam"),
]

HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
ANGLE_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
BRACKET_TAG_RE = re.compile(r"\[[^\[\]\n]{1,80}\]")
PLACEHOLDER_RE = re.compile(r"\{\s*\d+(?::[^{}\n]*)?\}|\{[A-Za-z_][A-Za-z_0-9]*\}")
CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
VDF_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')

# 从官方包抽取译名时只看这些字段(短专有名词,避免把长描述塞进术语表)
NAME_GLOSSARY_FIELDS = frozenset({
    "speaker",
    "teller",
    "name",
    "nickName",
    "displayName",
    "panicName",
    "abName",
    "nameWithTitle",
})
MAX_GLOSSARY_TERM_LEN = 40
MIN_GLOSSARY_TERM_LEN = 2
GLOSSARY_MAJORITY_RATIO = 0.6

DEFAULTS = {
    "game_path": "",
    "api": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 1.0,
        "max_tokens": None,
        "json_mode": True,
        "timeout": 120,
    },
    "concurrency": 4,
    "batch_max_strings": 30,
    "batch_max_chars": 2400,
    "max_retries": 3,
    "merge_official": True,
}


class FatalError(Exception):
    """无法恢复的错误,立即中止整个运行。"""


class Item:
    """一条待翻译字符串。holder[slot] 在翻译完成后被赋值。"""

    __slots__ = ("key", "src", "holder", "slot", "ck", "job")

    def __init__(self, key, src, holder, slot, ck, job):
        self.key = key
        self.src = src
        self.holder = holder
        self.slot = slot
        self.ck = ck
        self.job = job


class Job:
    """一个待翻译文件。"""

    def __init__(self, rel, data, items, unknown):
        self.rel = rel
        self.data = data
        self.items = items
        self.unknown = unknown
        self.pending = 0
        self.written = False
        self.overlaid = False


# ---------------------------------------------------------------- 基础设施

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        if CONFIG_EXAMPLE_PATH.exists():
            shutil.copyfile(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
        print("已生成 config.json。")
    cfg = copy.deepcopy(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text("utf-8-sig"))
    except Exception as e:
        raise FatalError(f"config.json 解析失败:{e}")
    for k, v in user.items():
        if k == "api" and isinstance(v, dict):
            cfg["api"].update(v)
        else:
            cfg[k] = v
    return cfg


def load_json_config(path: Path) -> dict:
    if not path.exists():
        raise FatalError(f"缺少 {path.name}(应与脚本同目录)")
    data = json.loads(path.read_text("utf-8-sig"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_glossary() -> tuple[dict, list[str]]:
    """返回 (用户术语表, 译文含这些子串则失效的列表)。"""
    if not GLOSSARY_PATH.exists():
        raise FatalError(f"缺少 {GLOSSARY_PATH.name}(应与脚本同目录)")
    raw = json.loads(GLOSSARY_PATH.read_text("utf-8-sig"))
    invalidate = raw.get("_invalidate_if_contains") or []
    if not isinstance(invalidate, list):
        invalidate = []
    glossary = {
        k: v for k, v in raw.items()
        if not k.startswith("_") and isinstance(v, str) and v
    }
    return glossary, [s for s in invalidate if isinstance(s, str) and s]


def detect_game_path() -> Path | None:
    roots: list[Path] = []
    for steam in STEAM_ROOTS:
        vdf = steam / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in VDF_PATH_RE.finditer(text):
                p = m.group(1).replace("\\\\", "\\")
                roots.append(Path(p))
        roots.append(steam)
    for lib in roots:
        game = lib / "steamapps" / "common" / GAME_DIRNAME
        if (game / "LimbusCompany_Data").exists():
            return game
    return None


def locate_game_dirs(cfg: dict, require_zh: bool = True) -> tuple[Path, Path | None, Path]:
    game = cfg["game_path"]
    if game:
        game = Path(game)
        if not (game / "LimbusCompany_Data").exists():
            raise FatalError(f"game_path 无效:{game}")
    else:
        game = detect_game_path()
        if not game:
            raise FatalError(
                "未找到游戏目录。请在 config.json 的 game_path 填入游戏根目录,"
                rf'例如 "D:\Program Files (x86)\Steam\steamapps\common\{GAME_DIRNAME}"'
            )
    kr = game / KR_SUFFIX
    zh = game / ZH_SUFFIX
    ai = game / AI_SUFFIX
    if not kr.is_dir():
        raise FatalError(f"未找到韩文资源目录:{kr}")
    # 零协包是可选的:未安装时全量翻译(README 的"Token 杀手"模式)
    if not zh.is_dir():
        if require_zh:
            raise FatalError(f"未找到中文参考目录:{zh}")
        return kr, None, ai
    return kr, zh, ai


def kr_to_zh_rel(rel: Path) -> Path:
    return rel.with_name(rel.name[3:])  # 去掉 "KR_" 前缀


def zh_to_kr_rel(rel: Path) -> Path:
    return rel.with_name("KR_" + rel.name)


def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text("utf-8"))
        except Exception:
            print("[警告] 翻译缓存损坏,已忽略并重建。")
            return {}
        if not isinstance(cache, dict):
            print("[警告] 翻译缓存格式异常,已忽略并重建。")
            return {}
        return cache
    return {}


def scrub_cache(cache: dict, invalidate_contains: list[str]) -> int:
    """剔除仍含韩文、或含已废弃错译的缓存条目。返回删除条数。"""
    bad = []
    for k, v in cache.items():
        if k.startswith("_") or not isinstance(v, str):
            continue
        if HANGUL_RE.search(v):
            bad.append(k)
            continue
        if any(term in v for term in invalidate_contains):
            bad.append(k)
    for k in bad:
        del cache[k]
    return len(bad)


_cache_lock = threading.Lock()


def save_cache(cache: dict) -> None:
    with _cache_lock:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, CACHE_PATH)


def backoff_sleep(attempt: int) -> None:
    time.sleep(min(30, 2 ** attempt) + random.random())


def cache_key(key, src: str, user_glossary: dict) -> str:
    """缓存键 = sha1(字段名 + 原文 + 原文命中的用户术语)。

    不含官方抽取的大表,避免一次抽取就把几乎所有缓存作废。
    用户改 glossary.json 后,含该韩文术语的条目会自动换键重翻。
    """
    payload = f"{key}\n{src}"
    if user_glossary:
        hits = {k: v for k, v in user_glossary.items() if k and k in src}
        if hits:
            payload += "\n" + json.dumps(hits, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def glossary_subset(glossary: dict, texts: list[str], limit: int = 80) -> dict:
    """只注入当前批次实际命中的术语;过长的表会拖慢接口甚至超时。"""
    if not glossary:
        return {}
    blob = "\n".join(texts)
    hits = [(k, v) for k, v in glossary.items() if k and k in blob]
    hits.sort(key=lambda kv: (-len(kv[0]), kv[0]))
    return dict(hits[:limit])


def entry_ids(obj) -> set:
    """收集 JSON 中所有 id/key,用于判断官方包是否缺条目。"""
    ids: set[str] = set()

    def rec(o):
        if isinstance(o, dict):
            ident = o.get("id", o.get("key"))
            if ident is not None and not isinstance(ident, (dict, list)):
                ids.add(str(ident))
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(obj)
    return ids


def file_is_stale(kr_data, zh_data) -> bool:
    """官方文件存在但缺少韩文侧的 id/key,视为内容落后。"""
    extra = entry_ids(kr_data) - entry_ids(zh_data)
    return bool(extra)


def contains_hangul(obj) -> bool:
    """JSON 中是否存在任何韩文字符串值;全无韩文的源文件(空壳)没有可翻译内容。"""
    if isinstance(obj, str):
        return bool(HANGUL_RE.search(obj))
    if isinstance(obj, dict):
        return any(contains_hangul(v) for v in obj.values())
    if isinstance(obj, list):
        return any(contains_hangul(v) for v in obj)
    return False


def overlay_prefer_official(base, official):
    """按韩文结构走,官方已有且不含韩文的字符串优先,其余保留 base(AI/原文)。"""
    if isinstance(base, str):
        if (
            isinstance(official, str)
            and official.strip()
            and not HANGUL_RE.search(official)
        ):
            return official
        return base
    if isinstance(base, dict) and isinstance(official, dict):
        return {
            k: overlay_prefer_official(v, official[k]) if k in official else v
            for k, v in base.items()
        }
    if isinstance(base, list) and isinstance(official, list):
        if base and all(isinstance(x, dict) for x in base):
            off_map = {}
            for item in official:
                if isinstance(item, dict):
                    ident = item.get("id", item.get("key"))
                    if ident is not None:
                        off_map[str(ident)] = item
            if off_map and any(
                isinstance(x, dict) and x.get("id", x.get("key")) is not None
                for x in base
            ):
                out = []
                for item in base:
                    if not isinstance(item, dict):
                        out.append(item)
                        continue
                    ident = item.get("id", item.get("key"))
                    if ident is not None and str(ident) in off_map:
                        out.append(overlay_prefer_official(item, off_map[str(ident)]))
                    else:
                        out.append(item)
                return out
        n = min(len(base), len(official))
        return [
            overlay_prefer_official(base[i], official[i]) for i in range(n)
        ] + list(base[n:])
    return base


def _read_json(path: Path):
    return json.loads(path.read_text("utf-8-sig"))


def extract_official_glossary(kr_dir: Path, zh_dir: Path) -> dict:
    """从官方中文包抽取 speaker/teller/name 等短译名,多数决去冲突。"""
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    def collect(kobj, zobj):
        if isinstance(kobj, dict) and isinstance(zobj, dict):
            for k, v in kobj.items():
                zv = zobj.get(k)
                if zv is None:
                    continue
                collect(v, zv)
                if (
                    k in NAME_GLOSSARY_FIELDS
                    and isinstance(v, str)
                    and isinstance(zv, str)
                    and MIN_GLOSSARY_TERM_LEN <= len(v) <= MAX_GLOSSARY_TERM_LEN
                    and HANGUL_RE.search(v)
                    and zv.strip()
                    and not HANGUL_RE.search(zv)
                    and zv.strip() != v
                ):
                    votes[v][zv.strip()] += 1
        elif isinstance(kobj, list) and isinstance(zobj, list):
            for a, b in zip(kobj, zobj):
                collect(a, b)

    for kr_file in kr_dir.rglob("KR_*.json"):
        rel = kr_file.relative_to(kr_dir)
        zh_file = zh_dir / kr_to_zh_rel(rel)
        if not zh_file.is_file():
            continue
        try:
            collect(_read_json(kr_file), _read_json(zh_file))
        except Exception:
            continue

    out = {}
    for src, ctr in votes.items():
        dst, n = ctr.most_common(1)[0]
        if n / sum(ctr.values()) >= GLOSSARY_MAJORITY_RATIO:
            out[src] = dst
    return dict(sorted(out.items()))


def save_official_glossary(mapping: dict) -> None:
    OFFICIAL_GLOSSARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": "由 scan/translate 从官方 LLC_zh-CN 自动抽取,勿手改;与 glossary.json 冲突时以用户表为准。",
        "_generated": datetime.now().isoformat(timespec="seconds"),
        "_count": len(mapping),
        **mapping,
    }
    OFFICIAL_GLOSSARY_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), "utf-8"
    )


def load_official_glossary() -> dict:
    if not OFFICIAL_GLOSSARY_PATH.exists():
        return {}
    try:
        raw = json.loads(OFFICIAL_GLOSSARY_PATH.read_text("utf-8-sig"))
    except Exception:
        return {}
    return {
        k: v for k, v in raw.items()
        if not k.startswith("_") and isinstance(v, str) and v
    }


def refresh_official_glossary(kr_dir: Path, zh_dir: Path) -> dict:
    print("正在从官方中文包抽取 NPC/名称译名…")
    mapping = extract_official_glossary(kr_dir, zh_dir)
    save_official_glossary(mapping)
    try:
        shown = OFFICIAL_GLOSSARY_PATH.relative_to(PROJECT_DIR)
    except ValueError:  # 测试沙盒中路径被重定向到项目外
        shown = OFFICIAL_GLOSSARY_PATH
    print(f"官方术语 {len(mapping)} 条 → {shown}")
    return mapping


# ---------------------------------------------------------------- 扫描

def cmd_scan(cfg: dict) -> int:
    kr_dir, zh_dir, _ = locate_game_dirs(cfg, require_zh=False)
    if zh_dir is None:
        print(
            "[注意] 未检测到零协中文包(LLC_zh-CN):所有韩文文件都视为未翻译,\n"
            "       将整包送翻——Token 消耗非常大,建议先安装零协最新汉化。"
        )
    else:
        refresh_official_glossary(kr_dir, zh_dir)

    missing = []
    stale = []
    empty = 0
    total = 0
    for kr_file in sorted(kr_dir.rglob("KR_*.json")):
        total += 1
        rel = kr_file.relative_to(kr_dir)
        zh_rel = kr_to_zh_rel(rel)
        zh_file = zh_dir / zh_rel if zh_dir is not None else None
        kind = None
        if zh_file is None or not zh_file.exists():
            kind = "missing"
        else:
            try:
                if file_is_stale(_read_json(kr_file), _read_json(zh_file)):
                    kind = "stale"
            except Exception:
                kind = None
        if kind is None:
            continue
        try:
            kr_data = _read_json(kr_file)
        except Exception:
            kr_data = None
        if kr_data is not None and not contains_hangul(kr_data):
            # 韩文源里没有任何韩文字符串(如空 dataList 壳文件),没有可翻译内容
            empty += 1
            continue
        (missing if kind == "missing" else stale).append((kr_file, zh_rel, kind))

    to_copy = missing + stale
    UNTRANSLATED_DIR.mkdir(exist_ok=True)
    for kr_file, zh_rel, _kind in to_copy:
        dst = UNTRANSLATED_DIR / zh_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kr_file, dst)

    # 清理 untranslated/:零协(官方包)已补齐(结构完整)或韩文源已删除的文件不再保留,
    # 避免旧语料反复回写 ai/、也被 pack 误打包;无零协包时只按源文件是否存在清理
    pruned = []
    if UNTRANSLATED_DIR.exists():
        for p in sorted(UNTRANSLATED_DIR.rglob("*.json")):
            rel = p.relative_to(UNTRANSLATED_DIR)
            kr_src = kr_dir / zh_to_kr_rel(rel)
            if not kr_src.is_file():
                pruned.append(rel)
                continue
            try:
                if not contains_hangul(_read_json(kr_src)):
                    pruned.append(rel)
                    continue
            except Exception:
                pass
            if zh_dir is not None and (zh_dir / rel).is_file():
                try:
                    stale_now = file_is_stale(_read_json(kr_src), _read_json(zh_dir / rel))
                except Exception:
                    stale_now = False
                if not stale_now:
                    pruned.append(rel)
    for rel in pruned:
        (UNTRANSLATED_DIR / rel).unlink()
    for d in sorted((x for x in UNTRANSLATED_DIR.rglob("*") if x.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass

    LOG_DIR.mkdir(exist_ok=True)
    report = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "kr_dir": str(kr_dir),
        "zh_dir": str(zh_dir) if zh_dir else "(未安装零协包)",
        "total_kr_files": total,
        "missing_count": len(missing),
        "stale_count": len(stale),
        "empty_count": empty,
        "pruned_count": len(pruned),
        "missing": sorted(str(zh_rel) for _, zh_rel, _ in missing),
        "stale": sorted(str(zh_rel) for _, zh_rel, _ in stale),
    }
    (LOG_DIR / "scan_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )

    def by_dir(rows):
        acc: dict[str, int] = {}
        for _, zh_rel, _ in rows:
            d = str(zh_rel.parent)
            d = d if d != "." else "(根目录)"
            acc[d] = acc.get(d, 0) + 1
        return acc

    print(
        f"韩文文件共 {total} 个,无中文对应 {len(missing)} 个,"
        f"官方包内容落后 {len(stale)} 个,已复制到 ./untranslated/:"
    )
    if empty:
        print(f"  [跳过] {empty} 个韩文源文件不含任何韩文字符串(空壳文件),未纳入语料")
    if pruned:
        print(f"  [清理] {len(pruned)} 个文件已被官方包完整覆盖/源已删除/源无韩文,移出 untranslated/")
    if missing:
        print("  [缺失]")
        for d, n in sorted(by_dir(missing).items()):
            print(f"    {d}: {n}")
    if stale:
        print("  [落后]")
        for d, n in sorted(by_dir(stale).items()):
            print(f"    {d}: {n}")
        for _, zh_rel, _ in stale:
            print(f"    - {zh_rel}")
    print("报告:logs/scan_report.json")
    return 0


# ---------------------------------------------------------------- 提取

def walk_collect(container, key_hint: str | None, whitelist: set,
                 items: list, unknown: dict, job: Job, user_glossary: dict) -> None:
    """递归遍历 JSON,收集含韩文且字段名在白名单内的字符串值。"""
    if isinstance(container, dict):
        pairs = container.items()
    elif isinstance(container, list):
        pairs = enumerate(container)
    else:
        return
    for slot, value in pairs:
        key = slot if isinstance(container, dict) else key_hint
        if isinstance(value, str):
            if not HANGUL_RE.search(value):
                continue
            if key in whitelist:
                ck = cache_key(key, value, user_glossary)
                items.append(Item(key, value, container, slot, ck, job))
            else:
                unk = unknown.setdefault(key, {"files": set(), "examples": []})
                unk["files"].add(job.rel)
                if len(unk["examples"]) < 5 and value not in unk["examples"]:
                    unk["examples"].append(value)
        else:
            walk_collect(value, key, whitelist, items, unknown, job, user_glossary)


def collect_jobs(paths: list[Path], whitelist: set, user_glossary: dict,
                 zh_dir: Path | None) -> list[Job]:
    jobs = []
    for path in paths:
        job = Job(path.relative_to(UNTRANSLATED_DIR), None, [], {})
        job.data = json.loads(path.read_text("utf-8-sig"))
        if zh_dir is not None:
            zh_file = zh_dir / job.rel
            if zh_file.is_file():
                try:
                    official = _read_json(zh_file)
                    job.data = overlay_prefer_official(job.data, official)
                    job.overlaid = True
                except Exception:
                    pass
        walk_collect(job.data, None, whitelist, job.items, job.unknown, job, user_glossary)
        jobs.append(job)
    return jobs


# ---------------------------------------------------------------- 大模型调用

def build_system_prompt(glossary: dict) -> str:
    lines = [
        "你是 Steam 游戏《边狱巴士》(Limbus Company)的简体中文本地化译者,"
        "译文质量需达到官方本地化水准。",
        "该游戏出自 Project Moon 世界观(《脑叶公司》《废墟图书馆》《边狱巴士》),"
        "译名与用词必须符合这一世界观的既有简体中文翻译习惯。",
        "",
        "翻译规则:",
        "1. 必须使用下方术语表中的固定译名;术语表未覆盖的专有名词,"
        "参照 Project Moon 官方简中文案的命名风格。",
        "2. 叙述文本保持原作冷峻、克制的文学化笔调;对话要贴合说话角色的语气与身份。",
        "3. 原文中的方括号标签若为英文(如 [OnSucceedAttackHead])必须原样保留;"
        "方括号内是韩文时(如 [로쟈])保留方括号、翻译其中韩文。"
        "样式标签(如 <style=\"dkr\">、<color=#5bffde>)、"
        "占位符(如 {0}、{conditions})、数字、英文、缩写必须原样保留,禁止增删或改写。",
        "4. 换行符的位置与数量保持不变。",
        "5. 仅翻译韩语;原文中已有的英文、数字、符号保持原样。"
        "所有韩文都必须译成中文,输出中禁止残留任何韩文字符,"
        "也禁止原样照抄原文或返回空译文。",
        "6. Buff(增益/减益)等效果的叠加数量一律译为「层数/层」(如\"叠加 2 层\"),"
        "禁止译作「次数/次」,与官方简中文案的表述一致。",
        "7. 韩文的词间空格不要带进中文(如\"침잠 强度\"应译\"沉沦强度\");"
        "禁止照搬韩语语序,效果获得类句子按中文语序重组,"
        "如\"[X] 3 얻음\"应译\"获得3层[X]\",不得译\"[X] 3 获得\"。",
        "8. 中文正文只使用全角标点,省略号一律输出\"……\"。",
        "9. 韩中同形异义词按上下文翻译:저자 是\"那位/那人\",不是\"作者/笔者\"。"
        "修饰结构不得更换基础名词(如\"부서진 뼛걸이\"译\"破损的骨衣架\","
        "不得改成\"破损的骨架\");同一术语、人名、地名在所有条目中必须使用"
        "同一译名,与术语表及既有的 NPC/地点显示名保持一致,不得自创变体。",
        "10. 只输出译文,不要添加任何解释、注释、拼音或原文。",
    ]
    if glossary:
        lines += [
            "",
            "术语表(韩文 → 简体中文):",
            json.dumps(glossary, ensure_ascii=False, sort_keys=True),
        ]
    return "\n".join(lines)


def call_llm(api: dict, messages: list, json_mode: bool) -> str:
    url = api["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": api["model"],
        "temperature": api["temperature"],
        "messages": messages,
    }
    if api.get("max_tokens"):
        payload["max_tokens"] = api["max_tokens"]
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    def post(pl: dict) -> str:
        req = urllib.request.Request(
            url,
            data=json.dumps(pl, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api['api_key']}",
                # 部分 API 中转站套了 Cloudflare,默认的 Python-urllib UA 会被
                # 按"浏览器签名"拦截(HTTP 403 error 1010),必须带常规 UA
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=api["timeout"]) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]

    try:
        return post(payload)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            raise FatalError(f"API 认证失败({e.code}),请检查 api_key。{detail}")
        if e.code == 400 and json_mode:
            # 部分服务不支持 response_format,退回普通模式重试一次
            payload.pop("response_format", None)
            return post(payload)
        raise


def extract_translations(text: str | None, n: int) -> list[str] | None:
    if not text:
        return None
    t = text.strip()
    m = CODE_FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    obj = None
    try:
        obj = json.loads(t)
    except Exception:
        i, j = t.find("["), t.rfind("]")
        if i != -1 and j > i:
            try:
                obj = json.loads(t[i : j + 1])
            except Exception:
                return None
    if isinstance(obj, dict):
        obj = obj.get("t") or obj.get("translations")
    if not isinstance(obj, list) or len(obj) != n:
        return None
    if not all(isinstance(x, str) for x in obj):
        return None
    return obj


def tags_preserved(src: str, dst: str) -> bool:
    if dst.strip() == "":
        return False
    if src.count("\n") != dst.count("\n"):
        return False
    # 白名单字段的译文不允许残留任何韩文(模型偷懒原样返回时必须重试)
    if HANGUL_RE.search(dst):
        return False
    for tag in ANGLE_TAG_RE.findall(src):
        if tag not in dst:
            return False
    for tag in BRACKET_TAG_RE.findall(src):
        # 纯英文方括号标签(如 [OnSucceedAttackHead])必须原样保留;
        # 含韩文的(如 [로쟈])允许翻译内部,只要求括号数量守恒(见下)
        if not HANGUL_RE.search(tag) and tag not in dst:
            return False
    if src.count("[") != dst.count("[") or src.count("]") != dst.count("]"):
        return False
    for ph in PLACEHOLDER_RE.findall(src):
        if ph not in dst:
            return False
    # 占位符集合必须与原文完全一致:既不能丢,也不能多出(多出的占位符会让游戏格式化出错)
    if len(PLACEHOLDER_RE.findall(dst)) != len(PLACEHOLDER_RE.findall(src)):
        return False
    return True


def request_translation(ctx: types.SimpleNamespace, items: list[Item],
                        system_prompt: str) -> list | None:
    """调用一次大模型,返回与 items 等长的译文数组;失败返回 None。"""
    srcs = [it.src for it in items]
    user = (
        "将以下 JSON 数组中的每个韩文字符串翻译为简体中文,"
        '返回与输入等长且顺序对应的译文数组。输出格式:{"t": ["译文", ...]}。\n'
        + json.dumps(srcs, ensure_ascii=False)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]
    for attempt in range(ctx.cfg["max_retries"] + 1):
        if ctx.stop.is_set():
            return None
        try:
            content = call_llm(ctx.api, messages, ctx.api["json_mode"])
            arr = extract_translations(content, len(srcs))
            if arr is not None:
                return arr
        except FatalError:
            raise
        except Exception as e:
            ctx.warn(f"请求异常,重试中:{type(e).__name__}: {e}")
        backoff_sleep(attempt)
    return None


def process_batch(ctx: types.SimpleNamespace, batch: list[Item]) -> dict:
    """翻译一批字符串,返回 {缓存键: 译文或 None(回退原文)}。"""
    srcs = [it.src for it in batch]
    glo = {**glossary_subset(ctx.full_glossary, srcs), **ctx.user_glossary}
    system_prompt = build_system_prompt(glo)
    results: dict[str, str | None] = {}
    bad = list(range(len(batch)))
    attempt = 0
    while bad and attempt <= ctx.cfg["max_retries"] and not ctx.stop.is_set():
        subset = [batch[i] for i in bad]
        try:
            arr = request_translation(ctx, subset, system_prompt)
        except FatalError:
            raise
        if arr is None:
            attempt += 1
            backoff_sleep(attempt)
            continue
        new_bad = []
        for i, trans in zip(bad, arr):
            if tags_preserved(batch[i].src, trans):
                results[batch[i].ck] = trans
            else:
                new_bad.append(i)
        bad = new_bad
        attempt += 1
    # 对顽固条目逐条抢救一次
    for i in list(bad):
        if ctx.stop.is_set():
            break
        try:
            arr = request_translation(ctx, [batch[i]], system_prompt)
        except FatalError:
            raise
        if arr and tags_preserved(batch[i].src, arr[0]):
            results[batch[i].ck] = arr[0]
            bad.remove(i)
    for i in bad:
        results[batch[i].ck] = None
        with ctx.lock:
            ctx.failures.append(
                {"file": str(batch[i].job.rel), "key": batch[i].key, "src": batch[i].src}
            )
    # 成功条目写入缓存
    good = {ck: t for ck, t in results.items() if t is not None}
    if good:
        with ctx.lock:
            ctx.cache.update(good)
            ctx.new_cache += len(good)
    return results


# ---------------------------------------------------------------- 连接测试

def cmd_test(cfg: dict) -> int:
    """用一次最小翻译请求检测 BYOK 大模型 API 的可用性。"""
    api = cfg["api"]
    if not api.get("api_key"):
        api["api_key"] = os.environ.get("LIMBUS_LLM_KEY", "")
    key = api.get("api_key")
    print(f"base_url: {api['base_url']}")
    print(f"model:    {api['model']}")
    if key:
        print(f"api_key:  {key[:6]}***{key[-4:]}" if len(key) > 12 else "api_key:  (已设置)")
    else:
        print("[失败] 未配置 API 密钥:请在 config.json 的 api.api_key 填入,"
              "或设置环境变量 LIMBUS_LLM_KEY。")
        return 1

    src = "안녕하세요, 방장."
    messages = [
        {"role": "system", "content": "你是《边狱巴士》的韩译中译者,只输出译文。"},
        {"role": "user",
         "content": '将下面的韩文翻译为简体中文。输出格式:{"t": ["译文"]}。\n'
                    + json.dumps([src], ensure_ascii=False)},
    ]
    print(f"发送测试请求(json_mode={api['json_mode']})...")
    started = time.time()
    try:
        content = call_llm(api, messages, api["json_mode"])
    except FatalError as e:
        print(f"[失败] {e}")
        return 1
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        hints = {
            401: "API 密钥无效或未授权,请检查 api_key。",
            403: "密钥无权访问该资源(可能被风控或欠费)。",
            404: "路径或模型不存在:检查 base_url 是否需要以 /v1 结尾、model 名称是否正确。",
            429: "请求被限流(连接本身是通的):稍后再试或降低 concurrency。",
        }
        print(f"[失败] HTTP {e.code}:{hints.get(e.code, '服务端返回错误。')}")
        if detail:
            print(f"       服务端信息:{detail}")
        return 1
    except urllib.error.URLError as e:
        if "timed out" in str(e.reason):
            print(f"[失败] 请求超时:可在 config.json 调大 api.timeout(当前 {api['timeout']} 秒)。")
        else:
            print(f"[失败] 无法连接到 {api['base_url']}:{e.reason}")
            print("       请检查 base_url 与网络/代理设置。")
        return 1
    except Exception as e:
        print(f"[失败] {type(e).__name__}: {e}")
        return 1

    elapsed = time.time() - started
    arr = extract_translations(content, 1)
    if arr is None:
        print(f"[警告] 连接正常,但回复无法解析为预期 JSON(json_mode={api['json_mode']})。")
        print(f"       原始回复前 200 字符:{(content or '')[:200]!r}")
        print("       若持续出现,可在 config.json 将 api.json_mode 设为 false。")
        return 1
    print(f"[成功] 连接正常,耗时 {elapsed:.2f} 秒。")
    print(f"       测试翻译:{src} → {arr[0]}")
    return 0


# ---------------------------------------------------------------- 合并官方中文包

def _files_identical(a: Path, b: Path) -> bool:
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")


def merge_official_pack(cfg: dict) -> tuple[int, int, int, int]:
    """把官方中文包 LLC_zh-CN 复制进 ai/,使其成为完整语言包。

    同名且官方内容完整的文件以官方版为准;官方缺条目的文件与已有 AI 译文做条目级叠加。
    LLC_zh-CN 只读,绝不改动。
    返回 (新增数, 覆盖数, 已一致数, 落后叠加数)。
    """
    kr_dir, zh_dir, ai_dir = locate_game_dirs(cfg, require_zh=False)
    if zh_dir is None:
        return 0, 0, 0, 0
    added, overwritten, unchanged, overlaid = 0, 0, 0, 0
    for src in sorted(zh_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(zh_dir)
        dst = ai_dir / rel
        kr_file = kr_dir / zh_to_kr_rel(rel)
        existed = dst.exists()

        stale = False
        zh_data = None
        if src.suffix.lower() == ".json" and kr_file.is_file():
            try:
                zh_data = _read_json(src)
                stale = file_is_stale(_read_json(kr_file), zh_data)
            except Exception:
                stale = False

        if stale and existed:
            try:
                ai_data = _read_json(dst)
            except Exception:
                ai_data = None
            if ai_data is not None and zh_data is not None:
                merged = overlay_prefer_official(ai_data, zh_data)
                if merged != ai_data:
                    _write_json(dst, merged)
                    overlaid += 1
                else:
                    unchanged += 1
                continue

        if existed and _files_identical(src, dst):
            unchanged += 1
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if existed:
            overwritten += 1
        else:
            added += 1
    return added, overwritten, unchanged, overlaid


def cmd_merge(cfg: dict) -> int:
    _, zh_dir, _ = locate_game_dirs(cfg, require_zh=False)
    if zh_dir is None:
        print("[跳过] 未检测到零协中文包(LLC_zh-CN),没有可合并的内容。")
        return 0
    added, overwritten, unchanged, overlaid = merge_official_pack(cfg)
    print(
        f"已把官方中文包 LLC_zh-CN 合并到 ai/:新增 {added} 个,"
        f"以官方版覆盖 {overwritten} 个,落后条目叠加 {overlaid} 个,内容一致 {unchanged} 个。"
    )
    print("同名且官方完整时以官方译文为准;官方缺条目时保留 AI 补译。LLC_zh-CN 原目录未被改动。")
    return 0


# ---------------------------------------------------------------- 打包分发

def build_pack_note(n_files: int) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""《边狱巴士》(Limbus Company) AI 补译语言包
生成日期:{today}
补译文件数:{n_files}

【这是什么】
本包是开源工具 limbus-ai-localization(github.com/Noplch0/limbus-ai-localization)
生成的 AI 补译文本,只包含官方中文包(LLC_zh-CN)尚未翻译、由 AI 补译的游戏
JSON 文本(如新剧情、新机制文案等)。译文由大语言模型生成,非官方翻译,
可能存在误译,仅供交流学习。
本包只含纯文本 JSON:不含任何配置文件、API 密钥、缓存、日志、脚本或程序。

【安装方法】(安装前请先完全退出游戏)
  1. 把本压缩包整体解压到游戏目录的 LimbusCompany_Data\\Lang\\ 下,
     得到 Lang\\<文件夹名>\\(文件夹名可自定义,例如 AI_zh-CN;
     zip 根目录即语言包内容,直接解压即可,无需再建子文件夹);
  2. 把零协中文包(LLC_zh-CN,一般在 LimbusCompany_Data\\Lang\\LLC_zh-CN)
     里的全部文件复制进上一步解压出的文件夹;提示文件冲突时选择
     "跳过"而不是覆盖——包内同名文件是"官方已有译文 + AI 补译"的
     更完整版本,不要被零协旧版盖回去;
  3. 启动游戏,在标题界面左下角选择"自定义语言",选中你的文件夹名即可。

【还原】
在标题界面把语言切回原来的选项即可;或直接删除 Lang\\<文件夹名>\\。
零协中文包全程未被改动。

【游戏更新后】
游戏新增的文本不在本包内,会显示默认语言。请到项目 Release 页获取
按新版本生成的最新包;本包文件若因文本结构变化显示异常,删除对应
文件即可回退。

【免责声明】
本包与官方及零协汉化组无关;AI 译文按 Project Moon 官方简中风格约束
生成,但无法保证与官方最终译本一致。翻译包不含任何付费内容或作弊功能。
"""


def cmd_pack(cfg: dict, args) -> int:
    """把本工具翻译的文件( untranslated/ 与 ai/ 的交集 )打包成可分发 zip。

    zip 根目录即语言包内容(解压到 Lang/<自定义文件夹> 后把零协包复制进去、
    冲突选跳过即可),只含 ai/ 下的补译 JSON 与一份使用说明 txt;
    绝不打包 config.json(密钥)、cache/、logs/、untranslated/ 或脚本本身。
    """
    kr_dir, zh_dir, ai_dir = locate_game_dirs(cfg, require_zh=False)
    rels = []
    for p in sorted(UNTRANSLATED_DIR.rglob("*.json")):
        rel = p.relative_to(UNTRANSLATED_DIR)
        if not (ai_dir / rel).is_file():
            continue
        # 零协已完整覆盖(结构完整)的文件没有 AI 独占内容,不打包,
        # 避免把 LLC_zh-CN 的译文转发出去;未安装零协包时无需排除
        if zh_dir is not None:
            zh_file = zh_dir / rel
            if zh_file.is_file():
                try:
                    if not file_is_stale(_read_json(kr_dir / zh_to_kr_rel(rel)), _read_json(zh_file)):
                        continue
                except Exception:
                    pass
        rels.append(rel)
    if not rels:
        raise FatalError(
            "没有可打包的文件:untranslated/ 与 ai/ 没有交集。请先运行 scan + translate。"
        )

    if getattr(args, "out", None):
        out_path = Path(args.out)
    else:
        out_path = DIST_DIR / f"LimbusCompany_AI_CN_{datetime.now():%Y%m%d}.zip"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in rels:
            zf.write(ai_dir / rel, rel.as_posix())
        zf.writestr(PACK_NOTE_NAME, build_pack_note(len(rels)).encode("utf-8-sig"))

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"已打包 {len(rels)} 个补译文件 → {out_path}({size_mb:.1f} MB)")
    print(f"附使用说明:{PACK_NOTE_NAME}(含安装/还原/兼容性说明)")
    print("zip 根目录即语言包内容:解压到 Lang\\<自定义文件夹>,再把零协包复制进去(冲突选跳过)。")
    print("未包含:config.json(密钥)、cache/、logs/、untranslated/、脚本文件;")
    print("零协已完整覆盖的文件也不会打包(避免转发 LLC_zh-CN 译文)。")
    return 0


# ---------------------------------------------------------------- 翻译主流程

def cmd_translate(cfg: dict, args) -> int:
    kr_dir, zh_dir, ai_dir = locate_game_dirs(cfg, require_zh=False)
    whitelist = set(load_json_config(KEYS_PATH)["keys"])
    user_glossary, invalidate_contains = load_glossary()
    official_glossary = load_official_glossary() if zh_dir else {}
    if zh_dir and not official_glossary:
        official_glossary = refresh_official_glossary(kr_dir, zh_dir)
    # 用户表覆盖官方抽取,避免把已订正的译名又盖回去
    full_glossary = {**official_glossary, **user_glossary}

    files = sorted(UNTRANSLATED_DIR.rglob("*.json"))
    if args.file:
        files = [f for f in files if args.file in str(f.relative_to(UNTRANSLATED_DIR))]
        if not files:
            raise FatalError(f"untranslated/ 下没有匹配 “{args.file}” 的文件,请先运行 scan。")
    if not files:
        raise FatalError("untranslated/ 目录为空,请先运行:python limbus_loc.py scan")

    jobs = collect_jobs(files, whitelist, user_glossary, zh_dir)

    cache = load_cache()
    dropped = scrub_cache(cache, invalidate_contains)
    if dropped:
        print(f"[缓存] 已剔除 {dropped} 条含韩文或已废弃译名的条目,这些句子将重新翻译。")
        save_cache(cache)

    ctx = types.SimpleNamespace(
        cfg=cfg,
        api=cfg["api"],
        cache=cache,
        lock=threading.Lock(),
        stop=threading.Event(),
        failures=[],
        new_cache=0,
        user_glossary=user_glossary,
        full_glossary=full_glossary,
        warn=lambda msg: print(f"[警告] {msg}"),
    )

    ignore_cache = bool(getattr(args, "force", False) or getattr(args, "cold_cache", False))

    # 区分缓存命中与待翻译;相同字符串只请求一次,但结果要应用到所有出现位置
    items_by_ck: dict[str, list[Item]] = {}
    cache_hits = 0
    exact_hits = 0
    exact_saved = 0
    for job in jobs:
        for it in job.items:
            # 名称类字段:整条字符串就是术语时直接套用官方译名,保证 NPC/名称译名一致
            exact = (
                full_glossary.get(it.src)
                if it.key in NAME_GLOSSARY_FIELDS
                else None
            )
            if exact is not None and not HANGUL_RE.search(exact):
                it.holder[it.slot] = exact
                exact_hits += 1
                if cache.get(it.ck) != exact:
                    cache[it.ck] = exact
                    exact_saved += 1
                continue
            if not ignore_cache and it.ck in cache and isinstance(cache[it.ck], str):
                it.holder[it.slot] = cache[it.ck]
                cache_hits += 1
            else:
                items_by_ck.setdefault(it.ck, []).append(it)
    pending_items = [lst[0] for lst in items_by_ck.values()]

    batches = []
    cur: list[Item] = []
    chars = 0
    for it in pending_items:
        if cur and (
            len(cur) >= cfg["batch_max_strings"]
            or chars + len(it.src) > cfg["batch_max_chars"]
        ):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(it)
        chars += len(it.src)
    if cur:
        batches.append(cur)

    total_items = sum(len(j.items) for j in jobs)
    overlaid_n = sum(1 for j in jobs if j.overlaid)
    print(
        f"共 {len(jobs)} 个文件,{total_items} 条待译文本"
        f"(其中 {overlaid_n} 个文件已套用官方已有译文);"
        f"缓存命中 {cache_hits} 条,术语精确命中 {exact_hits} 条,"
        f"本次需请求 {len(pending_items)} 条,分 {len(batches)} 批,"
        f"并发 {cfg['concurrency']},模型 {cfg['api']['model']}。"
    )
    if args.dry_run:
        return finish_dry_run(ctx, jobs, batches, total_items, cache_hits)

    if not cfg["api"].get("api_key"):
        cfg["api"]["api_key"] = os.environ.get("LIMBUS_LLM_KEY", "")
    if not cfg["api"]["api_key"]:
        raise FatalError(
            "未配置 API 密钥:请在 config.json 的 api.api_key 填入,"
            "或设置环境变量 LIMBUS_LLM_KEY。"
        )

    for job in jobs:
        job.pending = sum(1 for it in job.items if it.ck in items_by_ck)

    started = time.time()
    done_batches = 0
    applied = 0
    files_done = 0
    ai_dir.mkdir(parents=True, exist_ok=True)

    def progress_line() -> None:
        print(
            f"\r进度:文件 {files_done}/{len(jobs)} | 批次 {done_batches}/{len(batches)} | "
            f"文本 {applied}/{len(pending_items)} 条 | 回退 {len(ctx.failures)} | "
            f"用时 {time.time() - started:.0f} 秒   ",
            end="", flush=True,
        )

    def finish_job(job: Job) -> None:
        nonlocal files_done
        write_job(ai_dir, job)
        job.written = True
        files_done += 1
        print(f"\n[完成 {files_done}/{len(jobs)}] {job.rel}({len(job.items)} 条)")

    def apply_and_maybe_write(batch: list[Item], results: dict) -> None:
        nonlocal applied
        touched: dict[Job, int] = {}
        for it in batch:
            trans = results.get(it.ck)
            value = trans if trans is not None else it.src
            # 同一字符串(同一缓存键)在该文件或其他文件中的所有出现位置都要替换
            for dup in items_by_ck.get(it.ck, ()):
                dup.holder[dup.slot] = value
                touched[dup.job] = touched.get(dup.job, 0) + 1
            applied += 1
        for job, n in touched.items():
            job.pending -= n
            if job.pending <= 0 and not job.written:
                finish_job(job)

    def abort_now(code: int, msg: str) -> None:
        ctx.stop.set()
        with ctx.lock:
            save_cache(cache)
        print(msg)
        os._exit(code)

    if batches:
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=cfg["concurrency"])
        futs = {ex.submit(process_batch, ctx, b): b for b in batches}
        try:
            for fut in concurrent.futures.as_completed(futs):
                batch = futs[fut]
                results = fut.result()
                apply_and_maybe_write(batch, results)
                done_batches += 1
                progress_line()
                if ctx.new_cache:
                    save_cache(cache)
        except KeyboardInterrupt:
            abort_now(130, "\n[中止] 已保存缓存,重跑可续。")
        except FatalError as e:
            ctx.stop.set()
            with ctx.lock:
                save_cache(cache)
            ex.shutdown(wait=False, cancel_futures=True)
            print(f"\n[失败] {e}")
            return 1
        else:
            ex.shutdown(wait=True)
        print()
    # 全部命中缓存或无待译条目的文件不会经过批次循环,这里统一收尾写出
    for job in jobs:
        if not job.written:
            finish_job(job)

    if ctx.new_cache or dropped or exact_saved:
        save_cache(cache)

    # 汇总
    unknown_agg: dict[str, dict] = {}
    for job in jobs:
        for key, info in job.unknown.items():
            agg = unknown_agg.setdefault(
                key, {"files": set(), "examples": []}
            )
            agg["files"].update(info["files"])
            for e in info["examples"]:
                if e not in agg["examples"] and len(agg["examples"]) < 5:
                    agg["examples"].append(e)
    for key, agg in unknown_agg.items():
        agg["files"] = len(agg["files"])

    summary = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "model": cfg["api"]["model"],
        "files": len(jobs),
        "items_total": total_items,
        "cache_hits": cache_hits,
        "glossary_exact_hits": exact_hits,
        "newly_translated": ctx.new_cache,
        "fallback": len(ctx.failures),
        "elapsed_sec": round(time.time() - started, 1),
        "files_detail": {
            str(j.rel): {
                "items": len(j.items),
                "fallback": sum(1 for f in ctx.failures if f["file"] == str(j.rel)),
            }
            for j in jobs
        },
    }
    LOG_DIR.mkdir(exist_ok=True)
    (LOG_DIR / "translate_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
    )
    (LOG_DIR / "unknown_keys.json").write_text(
        json.dumps(unknown_agg, ensure_ascii=False, indent=2), "utf-8"
    )
    if ctx.failures:
        (LOG_DIR / "failures.json").write_text(
            json.dumps(ctx.failures, ensure_ascii=False, indent=2), "utf-8"
        )

    print(
        f"完成:{files_done}/{len(jobs)} 个文件,本次新译 {ctx.new_cache} 条,"
        f"缓存命中 {cache_hits} 条,回退原文 {len(ctx.failures)} 条,"
        f"用时 {summary['elapsed_sec']} 秒。"
    )
    print(f"输出目录:{ai_dir}")
    if unknown_agg:
        print(
            "[注意] 发现含韩文但不在白名单的字段(已保留原文):"
            + ", ".join(sorted(unknown_agg))
        )
        print("请检查 logs/unknown_keys.json,确认安全后加入 translatable_keys.json 重跑。")
    if ctx.failures:
        print(f"[注意] 有 {len(ctx.failures)} 条翻译失败回退原文,详见 logs/failures.json,重跑可重试。")

    # 把官方中文包合并进 ai/,使 ai 成为完整语言包(官方译文 + AI 补译)
    if cfg.get("merge_official", True):
        if zh_dir is None:
            print("官方包合并:未检测到零协包,已跳过(ai/ 目前只含 AI 译文)。")
        else:
            added, overwritten, unchanged, overlaid = merge_official_pack(cfg)
            print(
                f"官方包合并:ai/ 新增 {added} 个文件,以官方版覆盖 {overwritten} 个,"
                f"落后条目叠加 {overlaid} 个,已一致 {unchanged} 个(LLC_zh-CN 只读未动)。"
            )
    return 0


def finish_dry_run(ctx, jobs, batches, total_items, cache_hits) -> int:
    LOG_DIR.mkdir(exist_ok=True)
    unknown_agg: dict[str, dict] = {}
    per_file = {}
    for job in jobs:
        per_file[str(job.rel)] = {"items": len(job.items), "overlaid": job.overlaid}
        for key, info in job.unknown.items():
            agg = unknown_agg.setdefault(key, {"files": set(), "examples": []})
            agg["files"].update(info["files"])
            for e in info["examples"]:
                if e not in agg["examples"] and len(agg["examples"]) < 5:
                    agg["examples"].append(e)
    for agg in unknown_agg.values():
        agg["files"] = len(agg["files"])
    report = {
        "mode": "dry-run",
        "files": len(jobs),
        "items_total": total_items,
        "cache_hits": cache_hits,
        "api_calls_estimate": len(batches),
        "per_file": per_file,
        "unknown_keys": unknown_agg,
    }
    (LOG_DIR / "dry_run_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    print(f"[dry-run] 预计调用 API {len(batches)} 次,报告见 logs/dry_run_report.json")
    if unknown_agg:
        print(
            "[注意] 含韩文但不在白名单的字段(将保留原文):"
            + ", ".join(sorted(unknown_agg))
        )
        print("详情见 logs/dry_run_report.json 的 unknown_keys 部分。")
    return 0


def write_job(ai_dir: Path, job: Job) -> None:
    dst = ai_dir / job.rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps(job.data, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )


# ---------------------------------------------------------------- 帮助

HELP_COMMANDS: dict[str, dict] = {
    "scan": {
        "short": "找出缺失/落后的韩文文件,复制到 ./untranslated/",
        "desc": "对比游戏韩文资源(kr/)与零协中文包(LLC_zh-CN),把缺失或内容落后"
                "(缺 id/key)的 KR_ 文件复制到 ./untranslated/(未安装零协包时全部"
                " KR_ 文件视为缺失,整包送翻);同时从零协包抽取 NPC/名称译名到"
                " cache/glossary_official.json。零协已补齐、源已删除或源中无任何"
                "韩文(空壳文件)的条目会自动移出语料目录。",
        "options": [],
        "example": "python limbus_loc.py scan",
    },
    "test": {
        "short": "测试大模型 API 连通性",
        "desc": "用一次最小翻译请求测试 BYOK 大模型 API 是否可用,首次配置后建议先跑。",
        "options": [],
        "example": "python limbus_loc.py test",
    },
    "translate": {
        "short": "翻译语料并输出到游戏 Lang/ai/",
        "desc": "翻译 ./untranslated/ 下的文件并输出到游戏目录 Lang/ai/(去掉 KR_ 前缀)。"
                "字符串级缓存断点续翻;落后文件先套零协已有译文,只补翻新增韩文;名称类"
                "字段整条命中术语时直接套用官方译名。",
        "options": [
            ("--file <子串>", "只处理相对路径含该子串的文件,如 --file RPGSystem"),
            ("--force", "忽略缓存,所有字符串重新翻译(费用高,慎用)"),
            ("--cold-cache", "忽略全部缓存;改术语表后一般不必用,含该术语的条目会自动失效"),
            ("--dry-run", "只统计(条数/批数/未知字段),不调用 API 不写文件"),
        ],
        "example": "python limbus_loc.py translate --file KR_S1017B",
    },
    "merge": {
        "short": "把零协中文包合并进 ai/(translate 后自动执行)",
        "desc": "把零协中文包 LLC_zh-CN 合并进 ai/,使 ai 成为完整中文语言包:官方完整"
                "文件整体采用零协版,落后文件条目级叠加、保留 AI 补译(未安装零协包时"
                "自动跳过)。LLC_zh-CN 只读不动。",
        "options": [],
        "example": "python limbus_loc.py merge",
    },
    "run": {
        "short": "scan + translate + merge,日常一键命令",
        "desc": "等价于依次执行 scan 和 translate(完成后自动合并零协包)。游戏更新后"
                "重跑即可:新增/落后自动识别,零协补翻的内容自动回填。选项同 translate。",
        "options": [],
        "example": "python limbus_loc.py run",
    },
    "pack": {
        "short": "把补译文件打包成可分发 zip(含使用说明)",
        "desc": "把本工具翻译的文件(untranslated/ 与 ai/ 的交集)打包成可分发 zip,附"
                "使用说明.txt;零协已完整覆盖的文件自动排除;绝不含密钥、缓存、日志、脚本。",
        "options": [
            ("--out <路径>", "输出 zip 路径(默认 dist/ 下按日期命名)"),
        ],
        "example": "python limbus_loc.py pack --out D:\\tmp\\mypack.zip",
    },
}


def cmd_help(args) -> int:
    topic = getattr(args, "topic", None)
    if topic:
        info = HELP_COMMANDS.get(topic)
        if not info:
            print(f"没有命令 “{topic}”。可用命令:{', '.join(HELP_COMMANDS)}")
            return 1
        print(f"{topic} — {info['desc']}")
        if info["options"]:
            print("选项:")
            for opt, note in info["options"]:
                print(f"  {opt:<16}{note}")
        print(f"示例:{info['example']}")
        return 0

    print("边狱巴士(Limbus Company)自动汉化工具 — 可用命令:\n")
    for name, info in HELP_COMMANDS.items():
        print(f"  {name:<11}{info['short']}")
    print(
        "  help [命令]  查看某个命令的详细说明\n"
        "\n"
        "通用选项(translate / run):\n"
        "  --file <子串>  只处理相对路径含该子串的文件\n"
        "  --force        忽略缓存全部重翻(费用高)\n"
        "  --cold-cache   忽略全部缓存;改术语表后一般不必用\n"
        "  --dry-run      只统计,不调用 API 不写文件\n"
        "\n"
        "典型流程:\n"
        "  首次使用   test → run → 把游戏 Lang/config.json 的 \"lang\" 改为 \"ai\"\n"
        "  游戏更新   run(自动识别新增/落后,零协补翻自动回填)\n"
        "  分享成果   pack\n"
        "\n"
        "详见 README.md。"
    )
    return 0


# ---------------------------------------------------------------- 入口

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="边狱巴士自动汉化工具")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="找出无中文对应或官方包落后的文件并复制到 ./untranslated/")
    sub.add_parser("test", help="测试大模型 API 连接性")
    sub.add_parser("merge", help="把官方中文包 LLC_zh-CN 合并进 ai/(翻译后也会自动执行)")
    pk = sub.add_parser("pack", help="把本工具翻译的文件打包成可分发的 zip(含使用说明)")
    hp = sub.add_parser("help", help="显示所有可用命令及说明")
    hp.add_argument("topic", nargs="?", help="要查看详情的命令名,如:help pack")
    tr = sub.add_parser("translate", help="翻译 ./untranslated/ 并输出到 Lang/ai/")
    run = sub.add_parser("run", help="scan + translate")
    for sp in (tr, run):
        sp.add_argument("--file", help="只处理相对路径包含该子串的文件")
        sp.add_argument("--force", action="store_true", help="忽略缓存,全部重新翻译")
        sp.add_argument(
            "--cold-cache",
            action="store_true",
            help="忽略全部缓存(改术语表后一般不必用:含该术语的条目会自动失效)",
        )
        sp.add_argument("--dry-run", action="store_true", help="只统计不调用 API 不写文件")
    pk.add_argument("--out", help="输出 zip 路径(默认 dist/ 下按日期命名)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        if args.command == "help":
            return cmd_help(args)
        cfg = load_config()
        if args.command == "scan":
            return cmd_scan(cfg)
        if args.command == "test":
            return cmd_test(cfg)
        if args.command == "merge":
            return cmd_merge(cfg)
        if args.command == "pack":
            return cmd_pack(cfg, args)
        if args.command == "translate":
            return cmd_translate(cfg, args)
        if args.command == "run":
            rc = cmd_scan(cfg)
            if rc:
                return rc
            return cmd_translate(cfg, args)
        return 2
    except FatalError as e:
        print(f"[错误] {e}")
        return 1
    except KeyboardInterrupt:
        print("\n已中止。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
