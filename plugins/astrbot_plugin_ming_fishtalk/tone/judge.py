"""判断层：不问「有几个」，问「这一处该不该有」。

来源：MX3a48c5/dsh-fish-tone 的 lib/judge.js（MIT）。
这一层是那个插件最值钱的地方：配额只治标，「一段里连换三种句式」才是 AI 味的指纹。
它不替你决定，只摆材料——文体基线、每一处的处境、要你自己答的问题。

数字是原作者量的：10 万条真人评论 + 2180 条 AI 评论，同一个 100 字窗口。
    ≥2 种不同句式：真人长篇 5.6%，AI 19.6%
    ≥3 种：真人 1.4%，AI 8.7%
"""

from __future__ import annotations

import re
import unicodedata

from .structures import FRAMES

REGISTERS = {
    "chat": {"label": "闲聊", "per1k": 0.8, "hint": "短、口语；这种长度里框架基本是凭空多出来的"},
    "note": {"label": "随手记", "per1k": 1.3, "hint": "不长的一段话，一两个框架就到顶了"},
    "explain": {"label": "解释", "per1k": 2.0, "hint": "在说明一件事；框架有活可干，但也不是越多越好"},
    "argue": {"label": "论证", "per1k": 2.4, "hint": "在跟某个说法较劲；对比和纠正本来就需要框架"},
    "steps": {"label": "步骤", "per1k": 2.6, "hint": "在排动作；先…再…在这里是真干活"},
}


def _count(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text))


def _has_pictographic(text: str) -> int:
    n = 0
    for ch in text:
        o = ord(ch)
        if 0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or o in (0x2764,):
            n += 1
    return n


def register_of(text) -> str:
    s = "" if text is None else str(text)
    n = len(s)
    casual = _count(s, r"哈哈|笑死|草|emmm|嗯嗯|诶|啊这|233|awsl|好耶")
    casual += _has_pictographic(s) + _count(s, r"[（(][^)）]{0,6}[)）]")
    steps = _count(s, r"然后|接着|第一步|第二步|之后|再是|最后|先是")
    argue = _count(s, r"但是|可是|其实|问题是|然而|不过|反而|恰恰|我觉得|我认为|说句|不是这样")
    explain = _count(s, r"因为|所以|也就是说|相当于|意思是|本质上|原因|意味着|换句话说")

    if casual >= 2:
        return "chat"
    if steps >= 3:
        return "steps"
    if n < 40:
        return "chat"
    if argue >= 2:
        return "argue"
    if explain >= 2:
        return "explain"
    if n < 150:
        return "note"
    return "explain"


def hits(text) -> list[dict]:
    """找出所有命中，带位置；同一截字只算更具体的那个。"""
    src = "" if text is None else str(text)
    out = []
    for f in FRAMES:
        for m in f["re"].finditer(src):
            out.append({"id": f["id"], "name": f["name"], "tier": f["tier"],
                        "at": m.start(), "span": m.group(0)})
    out.sort(key=lambda h: h["at"])

    def rank(h):
        return (2 if h["tier"] == "strict" else 1) * 100 + len(h["span"])

    kept: list[dict] = []
    for h in out:
        end = h["at"] + len(h["span"])
        clash = next((k for k in kept if h["at"] < k["at"] + len(k["span"]) and k["at"] < end), None)
        if clash is None:
            kept.append(h)
        elif rank(h) > rank(clash):
            kept[kept.index(clash)] = h
    kept.sort(key=lambda h: h["at"])
    return kept


def cluster(hs: list[dict], width: int = 100) -> dict:
    max_hits = max_distinct = 0
    at = -1
    for i, h in enumerate(hs):
        a = h["at"]
        win = [x for x in hs if a <= x["at"] < a + width]
        if len(win) > max_hits:
            max_hits, at = len(win), a
        d = len({x["id"] for x in win})
        if d > max_distinct:
            max_distinct, at = d, a
    return {"width": width, "maxHits": max_hits, "maxDistinct": max_distinct, "at": at}


_SENT_END = re.compile(r"[。！？!?\n]")


def sentence_at(text, at: int) -> str:
    src = "" if text is None else str(text)
    s = at
    while s > 0 and not _SENT_END.match(src[s - 1]):
        s -= 1
    e = at
    while e < len(src) and not _SENT_END.match(src[e]):
        e += 1
    return src[s : min(len(src), e + 1)].strip()


def overlap(a, b) -> float:
    x = re.sub(r"[\s，。！？、]", "", "" if a is None else str(a))
    y = re.sub(r"[\s，。！？、]", "", "" if b is None else str(b))
    if not x or not y:
        return 0.0

    def cont(A, B):
        if not A:
            return 0.0
        return sum(1 for g in A if g in B) / len(A)

    cx, cy = set(x), set(y)
    chars = max(cont(cx, cy), cont(cy, cx))

    def grams(s):
        return {s[i : i + 2] for i in range(len(s) - 1)}

    bigr = 0.0 if len(x) < 2 or len(y) < 2 else max(cont(grams(x), grams(y)), cont(grams(y), grams(x)))
    return max(chars, bigr)


_MISCONCEPTION = re.compile(
    r"以为|觉得|听说|很多人|别人说|常说|有人|都说|看上去|看起来|好像|不少人|总有人|认为|被当成|被说成"
)


def necessity(hit: dict, text, reg: str) -> dict | None:
    src = "" if text is None else str(text)
    prev = src[max(0, hit["at"] - 80) : hit["at"]]
    hid = hit["id"]

    if hid == "not-but":
        tail = src[hit["at"] : hit["at"] + 60]
        m = re.search(r"不是([^，。！？]{1,24}?)(?:，\s*)?(?:这|那|它|就)?(?:是|而是)([^，。！？]{1,24})", tail)
        if not m:
            return {"verdict": "ask", "why": "看不成对的两半", "ask": "这个「不是」否定的是谁的说法？"}
        x, y = m.group(1).strip(), m.group(2).strip()
        if overlap(x, y) > 0.55:
            return {"verdict": "cut", "why": f"「{y}」只是把「{x}」换个说法，不是真的对立",
                    "ask": f"「{y}」和「{x}」是两件事吗？"}
        if _MISCONCEPTION.search(prev):
            return {"verdict": "keep", "why": "前面确实种下了一个可能被误解的说法，这个对比有活干"}
        return {"verdict": "ask", "why": "前面没有人这么想过，这个对立是自造的",
                "ask": "前半句否定的是真有人持有的看法吗？说得出是谁吗？"}

    if hid == "most-de":
        near = src[max(0, hit["at"] - 120) : hit["at"]]
        items = _count(near, r"[、，]|和|跟|或|还有|一是|二是|第一|第二")
        if items >= 2:
            return {"verdict": "keep", "why": "前面列过几个候选，这个「最」是比出来的"}
        return {"verdict": "ask", "why": "前面没有候选集，「最」没有可比的东西",
                "ask": "跟什么比出来的「最」？比的东西在文中出现过吗？"}

    if hid == "first-then":
        if reg == "steps":
            return {"verdict": "keep", "why": "这段在排动作，先后是真的"}
        if len(src) < 150:
            return {"verdict": "cut", "why": "这么短的文里，「先…再…」多半只是顺口", "ask": "这两步真的分先后吗？"}
        return {"verdict": "ask", "why": "看不出这两步有没有硬先后",
                "ask": "把「先」和「再」调过来，意思会变吗？不会变就是多余的"}

    if hid == "emdash":
        after = src[hit["at"] + len(hit["span"]) : hit["at"] + len(hit["span"]) + 40]
        if re.match(r"^(因为|就是|也就是|意思是|指的是|说白了|相当于)", after.strip()):
            return {"verdict": "keep", "why": "破折号后面是真的解释"}
        if len(src) < 120:
            return {"verdict": "cut", "why": "短话里的破折号基本都是换气", "ask": "换成逗号，意思会丢吗？"}
        return {"verdict": "ask", "why": "破折号后面不是解释性内容",
                "ask": "这里是需要一个停顿，还是只是想让句子显得重一点？"}

    if hid == "roadmap":
        total = _count(src, r"首先|其次|综上所述|总而言之")
        if len(src) < 300:
            return {"verdict": "cut", "why": "不到 300 字的文不需要目录", "ask": "这段真的分了好几层吗？"}
        if total >= 3:
            return {"verdict": "ask", "why": "短文里排了一整套目录", "ask": "这三层各自解决的是不同的问题吗？"}
        return None

    if hid == "triple":
        parts = hit["span"].split("、")
        if all(len(p) <= 2 for p in parts):
            return {"verdict": "ask", "why": "三个都是短抽象词，像凑数", "ask": "这三样真的同一层级的并列吗？"}
        return None

    return None


BANDS = {
    "clean": {"label": "像人", "tone": "这个密度和扎堆程度都在真人范围里。"},
    "slight": {"label": "略重", "tone": "稍微多了一点，还不刺眼。"},
    "heavy": {"label": "重", "tone": "已经能看出来了，读的人会觉得这人在端着。"},
    "template": {"label": "模板腔", "tone": "这段不是写出来的，是套出来的。"},
}

MARKS_RELATE = ("也", "还", "反正")
MARKS_HEDGE = ("应该", "感觉", "可能", "好像")


def mark_density(text) -> dict:
    src = "" if text is None else str(text)
    chars = max(1, len(src))
    relate = sum(src.count(w) for w in MARKS_RELATE)
    hedge = sum(src.count(w) for w in MARKS_HEDGE)
    return {"relate": round(relate / chars * 1000, 1), "hedge": round(hedge / chars * 1000, 1)}


def assess(text) -> dict:
    src = "" if text is None else str(text)
    chars = len(src)
    reg = register_of(src)
    base = REGISTERS[reg]
    hs = hits(src)
    cl = cluster(hs)

    denom = max(chars, 120)
    per1k = round(len(hs) / denom * 1000, 1)
    ratio = round(per1k / base["per1k"], 1) if base["per1k"] else 0
    expected = round(base["per1k"] * denom / 1000, 2)
    over = round(len(hs) - expected, 2)

    short = chars < 150
    if not hs:
        band = "clean"
    elif cl["maxDistinct"] >= 3:
        band = "template"
    elif cl["maxDistinct"] >= 2:
        band = "heavy"
    elif over >= 3:
        band = "heavy"
    elif short and len(hs) >= 2:
        band = "heavy"
    elif over >= 1.5:
        band = "slight"
    elif short and len(hs) >= 1:
        band = "slight"
    else:
        band = "clean"

    rows = []
    for h in hs:
        v = necessity(h, src, reg)
        rows.append({
            **h,
            "sentence": sentence_at(src, h["at"]),
            "verdict": (v or {}).get("verdict", "count"),
            "why": (v or {}).get("why", ""),
            "ask": (v or {}).get("ask", ""),
        })

    questions = []
    for r in rows:
        if r["ask"] and len(questions) < 3:
            questions.append(r["ask"])
    if cl["maxDistinct"] >= 2:
        questions.insert(0, f"最挤的那 {cl['width']} 字里挤了 {cl['maxDistinct']} 种不同句式 —— "
                            f"它们是不是在解决同一件事？如果是，留一处就够。")

    return {
        "band": band,
        "bandLabel": BANDS[band]["label"],
        "bandTone": BANDS[band]["tone"],
        "register": reg,
        "registerLabel": base["label"],
        "baseline": base["per1k"],
        "chars": chars,
        "per1k": per1k,
        "ratio": ratio,
        "expected": expected,
        "over": over,
        "cluster": cl,
        "hits": rows,
        "marks": mark_density(src),
        "questions": questions[:3],
    }


_VERDICT_NAME = {"keep": "可以留", "ask": "要你判", "cut": "建议删", "count": "只计数"}


def render(v: dict) -> str:
    if not v:
        return ""
    L = [f"[判断] {v['bandLabel']} —— {v['bandTone']}",
         f"[文体] {v['registerLabel']} · 真人同文体基线 {v['baseline']}/千字",
         f"[个数] 这段长度，真人大概写 {v['expected']} 个；你写了 {len(v['hits'])} 个"
         f"（{v['per1k']}/千字，{v['ratio']} 倍）"]
    c = v["cluster"]
    if c["maxDistinct"] >= 2:
        L.append(f"[扎堆] 最挤的 {c['width']} 字里有 {c['maxDistinct']} 种不同句式 —— 这比总数更说明问题")
    else:
        L.append("[扎堆] 没有段落内的句式轮换")

    if v["hits"]:
        L.append("[逐处]")
        for i, h in enumerate(v["hits"], 1):
            extra = f"：{h['why']}" if h["why"] else ""
            L.append(f"  {i}. 「{h['span']}」 {h['name']} → {_VERDICT_NAME[h['verdict']]}{extra}")

    m = v["marks"]
    L.append(f"[微标记] 关系类（也/还/反正）{m['relate']}/千字，"
             f"留白类（好像/应该/感觉）{m['hedge']}/千字 —— 真人后一类约 2/千字")
    if v["questions"]:
        L.append("[要你自己答]")
        for q in v["questions"]:
            L.append(f"  - {q}")
    L.append("[看不见] 换个说法把同一句再说一遍、每段收个小结论、语气齐得不像人 —— 这些归写的人自己看")
    return "\n".join(L)
