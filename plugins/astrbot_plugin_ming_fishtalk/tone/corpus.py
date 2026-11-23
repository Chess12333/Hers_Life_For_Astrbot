"""语料与文风包：读 fishtalk 的那份，也往里写。

位置跟 DSH 侧同一个：~/.fishtalk/（corpus.jsonl / said.jsonl / pack.json / 文风包.md）。
两边共用一个池子，QQ 学到的口气，网页那边立刻能用；反过来也一样。

只做三件事：
  1. brief()   —— 把 pack.json 压成一小段能塞进提示词的话（≤ 700 字）
  2. learn()   —— 群里的真人发言写进 corpus.jsonl（去重、脱敏、去表情）
  3. said()    —— 我说过的话记一笔，下次查「别老说一样的」
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

HOME = os.environ.get("FISHTALK_HOME") or os.path.join(os.path.expanduser("~"), ".fishtalk")
PACK = os.path.join(HOME, "pack.json")
CORPUS = os.path.join(HOME, "corpus.jsonl")
SAID = os.path.join(HOME, "said.jsonl")
SEEN = os.path.join(HOME, ".ming-qq-seen.json")

LIVE = _MING_WS + r"\live"
STATE = os.path.join(LIVE, "fishtalk-qq.json")
PROBE = os.path.join(LIVE, "fishtalk-qq.log")

# 2026-11-18 修：口癖（喵/捏）与宿主注入尾巴不进语料
TIC_RE = re.compile(r"喵|喵呜|本鱼|鱼鱼|捏")
INJECT_TAG_RE = re.compile(
    r"\s*(?:<system[-_]reminder>[\s\S]*?</system[-_]reminder>|\[MSG_ID:[^\]]+\])\s*",
    re.I,
)

# 只过滤真正敏感的东西（跟 fishtalk store.js 同一套；人名不匿名化）
_SENSITIVE = [
    (re.compile(r"https?://\S+", re.I), "[链接]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[身份证号]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[卡号]"),
]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def scrub(text: str) -> str:
    t = str(text or "")
    # AstrBot 会往 message_str 尾巴上挂系统时间提醒，那不是人说的话
    t = re.sub(r"<system_reminder>[\s\S]*?</system_reminder>", "", t)
    t = re.sub(r"<system_reminder>[\s\S]*$", "", t)
    t = re.sub(r"<[^>]{1,40}>", "", t)
    t = re.sub(r"^\s*回复\s*@[^\s:：]{1,30}\s*[:：]\s*", "", t)
    # 2026-11-19：burst 合并标记 / 引用标签不是人说的话
    t = re.sub(r"[\uFF08(]\s*\d{1,2}:\d{2}:\d{2}\s*[\u2013\-]\s*\d{1,2}:\d{2}:\d{2}\s*之间连发\s*\d+\s*条\s*[\uFF09)]", "", t)
    t = re.sub(r"\[引用消息[^\]]*\]", "", t)
    for rex, rep in _SENSITIVE:
        t = rex.sub(rep, t)
    t = re.sub(r"\[UPOWER_[^\]\s\[]{1,40}\]", "", t)
    # 方括号表情、emoji 不学
    t = re.sub(r"\[[^\]\s\[]{1,12}\]", "", t)
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _safe_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _tail_jsonl(path: str, n: int) -> list[dict]:
    """只读最后 n 行（文件可能几十万行，别整份读进来）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 200_000)
            f.seek(size - block)
            data = f.read().decode("utf-8", "ignore")
        lines = [l for l in data.split("\n") if l.strip()]
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []


# ── 文风包 → 提示词里的一小段 ────────────────────────────────────
_pack_cache: dict[str, object] = {"mtime": 0.0, "data": None}


def load_pack() -> dict:
    try:
        mtime = os.stat(PACK).st_mtime
    except OSError:
        return {}
    if _pack_cache["mtime"] == mtime and _pack_cache["data"]:
        return _pack_cache["data"]  # type: ignore[return-value]
    data = _safe_json(PACK, {})
    if isinstance(data, dict):
        _pack_cache.update({"mtime": mtime, "data": data})
        return data
    return {}


def _fmt_pairs(pairs, n: int = 4) -> str:
    out = []
    for item in (pairs or [])[:n]:
        try:
            out.append(f"「{item[0]}」")
        except Exception:
            continue
    return "、".join(out)


def brief(scene: str = "QQ群", max_chars: int = 700) -> str:
    """把文风包压成一小段。没有包就返回空串（调用方据此决定不注入）。"""
    pack = load_pack()
    if not pack:
        return ""

    scenes = pack.get("scenes") or {}
    sc = scenes.get(scene) or {}
    total = (pack.get("counts") or {}).get("human") or sum(
        (v.get("count") or 0) for v in scenes.values() if isinstance(v, dict)
    )
    length = sc.get("length") or pack.get("length") or {}
    no_end = sc.get("noEndPunctRatio", pack.get("noEndPunctRatio"))
    endings = sc.get("endings") or pack.get("endings") or []
    particles = sc.get("particles") or pack.get("particles") or []
    examples = sc.get("examples") or pack.get("examples") or []

    label = "真人基线" if scene in ("QQ群", "QQ私聊", "B站评论区") else "语料基线"
    lines = [f"【说话的松紧 · {label}（{scene} {sc.get('count') or total} 条）】"]
    if length:
        lines.append(
            f"长度：中位 {length.get('median')} 字（p25 {length.get('p25')} / p75 {length.get('p75')}）。"
            "短是常态，别一段话塞三件事。"
        )
    if isinstance(no_end, (int, float)):
        lines.append(f"结尾：{round(no_end * 100)}% 的话不加标点；加的时候优先「？」。")
    # 2026-11-19：笑声词（哈/嘿/呵）不学——她明说过「先笑一声再把话说软，是讨赏」。
    BAN_LAUGH = re.compile(r"^[哈嘿呵]+$")
    particles = [p for p in particles if not TIC_RE.search(str(p)) and not BAN_LAUGH.search(str(p))]
    examples = [e for e in examples if not TIC_RE.search(str(e.get("text", "")))]
    if particles:
        lines.append(f"句尾语气词（从多到少）：{_fmt_pairs(particles, 5)}。一次一个，别叠。")
    if endings:
        lines.append(f"标点取舍：{_fmt_pairs(endings, 3)}。")
    if examples:
        picks = [e.get("text", "") for e in examples[:3] if e.get("text")]
        if picks:
            lines.append("范例（学味道，不照抄）：" + " ／ ".join(f"「{p[:40]}」" for p in picks))
    # 2026-11-19：不点名。名单本身会被当成可用的词（2026-11-13 删「病名清单」同一个道理）。
    lines.append("不要：全句不许出现「哈哈」「嘿嘿」「呵呵」——一个都不带；先笑一声再把话说软那种最像讨赏，整句都别那么起头；"
                 "想说「这条」「那个」就把指代落到具体一件事上，落不上就不发。")
    lines.append("句式：一段里别连换三种（不是A而是B / 最…的是 / 先…再…）；真人是重复自己的习惯，不是轮换工具箱。")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


# ── 学：群里的真人发言写进语料 ────────────────────────────────────
_seen_cache: dict[str, object] = {"mtime": 0.0, "set": None}


def _seen() -> set[str]:
    try:
        mtime = os.stat(SEEN).st_mtime
    except OSError:
        mtime = 0.0
    if _seen_cache["mtime"] == mtime and _seen_cache["set"] is not None:
        return _seen_cache["set"]  # type: ignore[return-value]
    raw = _safe_json(SEEN, [])
    s = set(raw) if isinstance(raw, list) else set()
    _seen_cache.update({"mtime": mtime, "set": s})
    return s


def _save_seen(s: set[str]) -> None:
    keep = list(s)[-6000:]
    os.makedirs(HOME, exist_ok=True)
    tmp = SEEN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False)
    os.replace(tmp, SEEN)
    _seen_cache.update({"mtime": os.stat(SEEN).st_mtime, "set": set(keep)})


def learn(text: str, *, where: str = "", scene: str = "QQ群", source: str = "qq") -> bool:
    """写一条。返回 True 才算真的新增了。"""
    text = INJECT_TAG_RE.sub(" ", str(text or ""))
    t = scrub(text)
    if TIC_RE.search(t):
        return False
    if not (2 <= len(t) <= 200):
        return False
    if t.startswith("/") or t.startswith("#"):
        return False
    h = hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]
    seen = _seen()
    if h in seen:
        return False
    row = {
        "text": t,
        "source": source,
        "where": where,
        "kind": "group",
        "day": time.strftime("%Y-%m-%d"),
        "scene": scene,
        "id": h,
    }
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(CORPUS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        return False
    seen.add(h)
    _save_seen(seen)
    return True


def remember_said(text: str) -> None:
    t = scrub(text)
    if not t:
        return
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(SAID, "a", encoding="utf-8") as f:
            f.write(json.dumps({"text": t, "day": time.strftime("%Y-%m-%d")}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def pools(limit: int = 400) -> tuple[list[dict], list[dict]]:
    """给 check 用的池子：真人语料（防照抄）和我说过的话（防复读）。"""
    human = [r for r in _tail_jsonl(CORPUS, limit) if isinstance(r.get("text"), str)]
    said = [r for r in _tail_jsonl(SAID, limit) if isinstance(r.get("text"), str)]
    return human, said


def realtime_stats(limit: int = 3000) -> dict:
    """最近这批群聊的现场统计（不重蒸馏，够看趋势）。"""
    rows = _tail_jsonl(CORPUS, limit)
    lens = sorted(len(r["text"]) for r in rows if r.get("text"))
    if not lens:
        return {}
    no_end = sum(1 for r in rows if r.get("text") and r["text"][-1] not in "。！？!?…~～")
    tail = Counter(r["text"][-1] for r in rows if r.get("text"))
    return {
        "n": len(lens),
        "median": lens[len(lens) // 2],
        "p25": lens[len(lens) // 4],
        "p75": lens[len(lens) * 3 // 4],
        "noEndRatio": round(no_end / len(lens), 2),
        "topEndings": tail.most_common(4),
    }


# ── 上一轮踩了什么雷：给下一轮的提示用 ────────────────────────────
def save_problems(items: list[str], text: str) -> None:
    try:
        os.makedirs(LIVE, exist_ok=True)
        data = {
            "at": _now(),
            "text": (text or "")[:120],
            "problems": items[:6],
        }
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except Exception:
        pass


def load_problems(max_age_min: int = 30) -> dict:
    data = _safe_json(STATE, {})
    if not isinstance(data, dict) or not data.get("problems"):
        return {}
    try:
        at = time.mktime(time.strptime(data.get("at", ""), "%Y-%m-%d %H:%M"))
    except Exception:
        return {}
    if (time.time() - at) / 60 > max_age_min:
        return {}
    return data


def probe(line: str) -> None:
    try:
        os.makedirs(LIVE, exist_ok=True)
        with open(PROBE, "a", encoding="utf-8") as f:
            f.write(f"{_now()} {line}\n")
        # 别让它长成一本账
        if os.path.getsize(PROBE) > 400_000:
            with open(PROBE, "r", encoding="utf-8", errors="ignore") as f:
                keep = f.readlines()[-800:]
            with open(PROBE, "w", encoding="utf-8") as f:
                f.writelines(keep)
    except Exception:
        pass
