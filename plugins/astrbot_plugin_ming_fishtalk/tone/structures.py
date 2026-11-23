"""结构腔：不是禁，是按密度压到真人的水平。

来源：MX3a48c5/dsh-fish-tone 的 lib/structures.js（MIT）。
底线只搬了三样——句式表、按字数算的预算、扫描与密度；它往成稿里插尾巴、
补手滑的那半层**一点没搬**（那跟人设里「删，不解释」是反的）。

per1k 是原作者拿 10 万条 B 站评论（3000 条 ≥150 字，96 万字）量的，不是估的。
"""

from __future__ import annotations

import re

# tier=strict 的基本只在长文里出现；soft 的每几百字允许一个
FRAMES = [
    {"id": "not-but", "name": "不是X而是Y", "tier": "strict", "per1k": 0.06,
     "re": re.compile(r"不是[^。！？]{1,24}(?:，\s*(?:这|那|它|就)?是|而是)")},
    {"id": "most-de", "name": "最…的是", "tier": "strict", "per1k": 0.05,
     "re": re.compile(r"最[^，。]{1,8}的是")},
    {"id": "first-then", "name": "先…再…", "tier": "strict", "per1k": 0.01,
     "re": re.compile(r"先[^，。]{1,10}再")},
    {"id": "notonly", "name": "不仅…更", "tier": "strict", "per1k": 0.01,
     "re": re.compile(r"不仅[^，。]{1,12}(?:更|还)")},
    {"id": "de-copula", "name": "X的是Y", "tier": "soft", "per1k": 0.37,
     "re": re.compile(r"[^的，。！？]{1,8}的是[^，。]")},
    {"id": "triple", "name": "三段并列", "tier": "soft", "per1k": 0.23,
     "re": re.compile(r"[^，。、]{2,8}、[^，。、]{2,8}、[^，。、]{2,8}")},
    {"id": "emdash", "name": "破折号", "tier": "soft", "per1k": 0.22,
     "re": re.compile(r"——+|—"), "fix": "comma"},
    {"id": "yue", "name": "越…越", "tier": "soft", "per1k": 0.16,
     "re": re.compile(r"越[^，。]{1,8}越")},
    {"id": "really", "name": "真正/本质上", "tier": "soft", "per1k": 0.17,
     "re": re.compile(r"(?:真正|本质上|归根结底)")},
    {"id": "roadmap", "name": "首先/其次/综上", "tier": "soft", "per1k": 0.5,
     "re": re.compile(r"(?:首先|其次|综上所述|总而言之)")},
]

_BY_ID = {f["id"]: f for f in FRAMES}


def budget(chars: int) -> dict[str, int]:
    """一篇多长，就允许多少个。"""
    out = {}
    for f in FRAMES:
        raw = f["per1k"] * chars / 1000
        if f["tier"] == "soft":
            # soft：200 字起至少给 1 个；不到 200 字只按比例，可以是 0
            out[f["id"]] = max(1 if chars >= 200 else 0, round(raw * 2))
        else:
            out[f["id"]] = round(raw)
    return out


def scan(text) -> list[dict]:
    src = "" if text is None else str(text)
    rows = []
    for f in FRAMES:
        spans = [m.group(0) for m in f["re"].finditer(src)]
        if spans:
            rows.append({"id": f["id"], "name": f["name"], "tier": f["tier"],
                         "count": len(spans), "spans": spans})
    return rows


def density(text) -> dict:
    src = "" if text is None else str(text)
    chars = max(1, len(src))
    rows = scan(src)
    total = sum(r["count"] for r in rows)
    return {"chars": chars, "total": total, "per1k": round(total / chars * 1000, 2), "rows": rows}


def thin(text, *, skip=None) -> dict:
    """压掉超预算的破折号（只有这一个会自动改，其余只报告）。

    skip(line) -> True 的行整行不碰（代码块、路径、表格）。
    默认不改任何字：只有调用方显式要 fix 才用。
    """
    src = "" if text is None else str(text)
    lines = src.split("\n")
    ok = [not (skip and skip(l)) for l in lines]
    chars = len("".join(l for l, k in zip(lines, ok) if k))
    if chars < 80:
        return {"text": src, "thinned": 0, "over": []}
    allow = budget(chars)

    counts: dict[str, int] = {}
    for i, line in enumerate(lines):
        if not ok[i]:
            continue
        for row in scan(line):
            counts[row["id"]] = counts.get(row["id"], 0) + row["count"]

    over, thinned = [], 0
    for fid, count in counts.items():
        allowed = allow.get(fid, 0)
        if count <= allowed:
            continue
        f = _BY_ID[fid]
        over.append({"id": fid, "name": f["name"], "count": count, "allowed": allowed})
        if not f.get("fix"):
            continue
        surplus = count - allowed
        for i, line in enumerate(lines):
            if not ok[i] or surplus <= 0:
                continue
            def _rep(m, _f=f):
                nonlocal surplus, thinned
                if surplus <= 0:
                    return m.group(0)
                surplus -= 1
                thinned += 1
                if _f.get("fix") == "comma":
                    return re.sub(r"[—、]", "，", m.group(0))
                return m.group(0)
            lines[i] = f["re"].sub(_rep, line)
    return {"text": "\n".join(lines), "thinned": thinned, "over": over}
