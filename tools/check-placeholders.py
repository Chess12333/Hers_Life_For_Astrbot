"""check-placeholders.py —— 装到别人机器上之前，先看清这份仓库里还剩哪些「作者本机的值」。

查四件事：

1. 占位 QQ 号 / 群号（1000000xx / 7000000xx）还留在哪几行；
2. 写死的作者工作区路径（C:\\dsh\\hers_workplace）还留在哪几行；
3. 现在环境里有没有把该设的变量设上——没设，代码就还在用占位值；
4. 每个插件有没有 requirements.txt（AstrBot 自动装依赖全靠它）。

用法：

    python tools/check-placeholders.py            # 只报，返回 0
    python tools/check-placeholders.py --strict   # 代码里还有占位值就返回 1（CI 用）

退出码只看「代码行」：注释、docstring、README 里的占位符是给人看的，不算失败。
"""

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"

# 占位号：1000000xx（人）、7000000xx（群）
QQ_RE = re.compile(r"(?:^|[^0-9])(1000000\d{2}|7000000\d{2})(?![0-9])")
# 作者本机工作区（两种写法：反斜杠、正斜杠，以及 os.sep 拼接式）
WS_RE = re.compile(r"C:[\\/]+dsh[\\/]+hers_workplace|\"C:\" \+ os\.sep \+ \"dsh\"")

SCAN_SUFFIX = {".py", ".json", ".yaml", ".yml"}

ENV_VARS = [
    ("MING_WORKSPACE", "工作区根目录（live/ people/ diary/ 都在它下面）"),
    ("MING_PLATFORM_ID", "AstrBot 里这个平台的 id（umo 前缀，比如 snowluma）；默认 ming"),
    ("MING_SELF_NAME", "这个平台上的自我称呼（群里喊名字要认它）；默认「溟」"),
    ("MING_HER_QQ", "主人的 QQ 号（HER_QQ / OWNER_QQ / HER_ID 共用）"),
    ("MING_OWNER_UMO", "主动消息投递用的完整 umo；不设就按平台 id + MING_HER_QQ 拼"),
    ("MING_OWNERS", "逗号分隔：算「自己人」的号"),
    ("MING_GROUPS", "逗号分隔：要认的群号"),
    ("MING_ABOUT_BY_QQ", "JSON：{\"100000001\": \"她\"} —— 号码到称呼"),
    ("MING_OLD_OBS", "旧壳（QQ-Agent）observations.jsonl 的路径；没有就留空"),
]

STANDALONE_FILES = {"tools/check-placeholders.py"}


FALLBACK_MARK = 'os.environ.get("MING_WORKSPACE")'
# 一行是不是「说明」而不是「代码」：注释、docstring 里、yaml 的 desc 行、作者 fallback 默认值、纯中文叙述。
PROSE_HEAD = ("desc:", "description:")


def classify(line: str, in_doc: bool) -> str:
    s = line.strip()
    if in_doc or '"""' in line or "'''" in line or s.startswith("#") or s.startswith(PROSE_HEAD):
        return "说明"
    if FALLBACK_MARK in line or "os.environ.get(" in line:
        return "默认值"   # 已经接到环境变量上的 fallback，不是残留
    # 冒号/中文逗号出现在第一个引号之前 → 当叙述看
    return "代码"


def scan() -> list:
    hits = []
    for path in sorted(PLUGINS.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIX:
            continue
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        in_doc = False
        quote_hits = 0
        for no, line in enumerate(text.splitlines(), 1):
            if path.suffix in {".py", ".yml", ".yaml"}:
                q = line.count('"""') + line.count("'''")
                if q % 2 == 1:
                    in_doc = not in_doc
            tag = classify(line, in_doc)
            for kind, rx in (("号码", QQ_RE), ("工作区路径", WS_RE)):
                for m in rx.finditer(line):
                    val = m.group(1) if m.lastindex else m.group(0)
                    hits.append({
                        "file": rel,
                        "line": no,
                        "kind": kind,
                        "value": val.strip(),
                        "tag": tag,
                        "text": line.strip()[:110],
                    })
            quote_hits += 0
    return hits


STDLIB = set("""
abc argparse asyncio base64 binascii bisect calendar codecs collections concurrent contextlib copy csv
ctypes dataclasses datetime decimal difflib email enum errno fnmatch fractions functools gc getpass glob
gzip hashlib hmac html http importlib inspect io ipaddress itertools json logging math mimetypes mmap
operator os pathlib pickle platform pprint queue random re secrets select shlex shutil signal socket
sqlite3 ssl stat string struct subprocess sys tarfile tempfile textwrap threading time token tokenize
traceback types typing unicodedata urllib uuid warnings weakref xml zipfile zlib zoneinfo
""".split())
IMPORT_RE = re.compile(r"^\\s*(?:import|from)\\s+([A-Za-z_][\\w.]*)", re.M)


def third_party(plugin):
    """这个插件真正 import 了哪些第三方模块（astrbot 与标准库不算）。"""
    mods = set()
    for src in plugin.rglob("*.py"):
        try:
            text = src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in IMPORT_RE.finditer(text):
            top = m.group(1).split(".")[0]
            if top in ("astrbot", "__future__") or top in STDLIB:
                continue
            mods.add(top)
    return mods


def check_requirements() -> list:
    """只对「真的 import 了第三方」的插件要求 requirements.txt。"""
    missing = []
    for plugin in sorted(p for p in PLUGINS.iterdir() if p.is_dir()):
        if not (plugin / "main.py").exists():
            continue
        mods = third_party(plugin)
        if mods and not (plugin / "requirements.txt").exists():
            missing.append((plugin.name, sorted(mods)))
    return missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="代码行还有占位值就返回 1")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    hits = scan()
    code_hits = [h for h in hits if h["tag"] == "代码"]

    print("=" * 70)
    print("环境变量")
    print("=" * 70)
    for name, why in ENV_VARS:
        val = os.environ.get(name, "")
        state = "已设" if val else "未设"
        shown = val if val else "—"
        print(f"  [{state}] {name} = {shown}")
        print(f"          {why}")
    print()

    print("=" * 70)
    print(f"占位值 / 写死路径：{len(hits)} 处（其中代码行 {len(code_hits)} 处；默认值(fallback)与说明不算）")
    print("=" * 70)
    by_file = {}
    for h in hits:
        by_file.setdefault(h["file"], []).append(h)
    for f, arr in sorted(by_file.items()):
        print(f"\n{f}")
        for h in arr:
            print(f"  {h['line']:>5}  [{h['tag']}][{h['kind']}] {h['value']}")
            print(f"         {h['text']}")
    if not hits:
        print("  （干净）")
    print()

    print("=" * 70)
    print("requirements.txt")
    print("=" * 70)
    missing = check_requirements()
    if missing:
        print("  缺（这些插件 import 了第三方模块却没有 requirements.txt，AstrBot 不会自动装）：")
        for name, mods in missing:
            print("    - " + name + ": 需要 " + ", ".join(mods))
    else:
        print("  （没有插件缺第三方依赖声明）")
    print()

    if code_hits:
        print("结论：代码里还有占位值。要么设上面的环境变量（优先），要么逐个改成自己的号。")
        print("      代码行清单：")
        for h in code_hits:
            print(f"        {h['file']}:{h['line']}  {h['value']}")
    else:
        print("结论：代码里没有残留占位值；剩下的都在注释/文档里，不影响运行。")

    if args.strict and code_hits:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
