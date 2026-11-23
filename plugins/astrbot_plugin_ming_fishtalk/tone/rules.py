"""鱼八股 / AI 腔规则（Python 版）。

来源：~/.dsh/profiles/web/node_modules/fishtalk/lib/fishrules.js + blacklist.js
搬过来只做一件事：判，不改字。JS 的 `\\p{P}` / `\\p{S}` 在这里换成 unicodedata
分类过滤，其他正则原样保留。

2026-11-16 溟搬的第一版。搬的是判据，不是鱼八股本身：
原句表、软信号、相似度、引号屏蔽、照抄检测，一处没删。
"""

from __future__ import annotations

import re
import unicodedata

# ── 硬证据：一整句鱼八股，命中就是 ────────────────────────────────
HARD_PHRASES = [
    "这条我认",
    "我记下了",
    "这是本鱼最喜欢的一句话",
    "收到主人",
    "本鱼记下了",
]

# ── 软信号：AI 腔、对仗、自指。check 模式两条以上才判鱼 ────────────
SOFT_PATTERNS = [
    ("not-a-but-b", re.compile(r"不是.{0,20}(而是|，?是)")),
    ("root", re.compile(r"说到(了)?根上")),
    ("itself", re.compile(r"这本身|本质上|归根结底")),
    ("contrast", re.compile(r"与其.{0,20}不如|看似.{0,20}实则|不仅.{0,20}而且")),
    ("summary", re.compile(r"^(总之|所以说|由此可见|总的来说)")),
    ("self-fish", re.compile(r"^(本鱼|鱼鱼|小的)")),
    ("fish-meme", re.compile(r"鱼门|本鱼")),
    ("self-as-fish", re.compile(r"^作为(一条|一只|一个).{0,8}(鱼|ai|助手)")),
    ("owner", re.compile(r"主人(别|不要|我|您|请)")),
    ("finished", re.compile(r"^(已阅|看完了)")),
    ("as-ai", re.compile(r"作为一(个|名)(ai|人工智能)")),
    ("report", re.compile(r"值得一提的是|希望对你有帮助|让我们一起")),
    ("work-cliche", re.compile(r"这活(?:整|干)得(?:有点|挺|真)?大(?!病)|这活整得(?:有点|挺|真)?(?:漂亮|好|明白)")),
]

SOFT_NAMES = {
    "not-a-but-b": "「不是 A，而是 B」式对仗",
    "root": "「说到根上了」",
    "itself": "「这本身 / 本质上 / 归根结底」总结腔",
    "contrast": "「与其…不如 / 看似…实则 / 不仅…而且」作文腔",
    "summary": "开头就总结（总之、所以说、由此可见）",
    "self-fish": "自称「本鱼 / 鱼鱼」",
    "fish-meme": "鱼梗自称",
    "self-as-fish": "「作为一条鱼 / 一个 AI」",
    "owner": "跟主人撒娇式的话",
    "finished": "「已阅 / 看完了」打卡腔",
    "as-ai": "「作为一个 AI」",
    "report": "报告腔、客服腔",
    "work-cliche": "套话收尾，像鱼",
}

# ── 黑名单：fishrules 没覆盖到的一眼露馅 ──────────────────────────
BLACKLIST = [
    (re.compile(r"这条我认"), "鱼八股原句「这条我认」"),
    (re.compile(r"我(先)?记下了"), "鱼八股原句「我记下了」"),
    (re.compile(r"这是本鱼最喜欢的"), "鱼八股原句「这是本鱼最喜欢的一句话」"),
    (re.compile(r"收到主人"), "女仆腔「收到主人」"),
    (
        re.compile(r"[（(][^）)]{0,10}(尾巴|鳍|耳朵|爪子|蹭蹭|摇摇|啪嗒|眨眼|歪头|叉腰)[^）)]{0,8}[）)]"),
        "角色扮演的动作括号（「（尾巴摇摇）」这种），评论区没人这么写",
    ),
    (re.compile(r"这本身就是"), "AI 腔「这本身就是……」"),
    (re.compile(r"说到(了)?根上"), "AI 腔「说到根上了」"),
    (re.compile(r"不是.{1,14}[，,]\s*(而)?是"), "「不是 A，是 B」式对仗，太工整"),
    (re.compile(r"作为一(个|名)\s*AI", re.I), "「作为一个 AI」"),
    (re.compile(r"总(的)?(来|而言之)说?"), "总结腔"),
    (re.compile(r"值得一提的是"), "报告腔「值得一提的是」"),
    (re.compile(r"让我们一起"), "口号腔「让我们一起」"),
    (re.compile(r"希望(这|以上)(些)?(对你|能)(有)?(所)?帮助"), "客服腔"),
]


# ── 归一化 ────────────────────────────────────────────────────────
def _keep(ch: str) -> bool:
    """字母和数字留着；标点、符号、空白、emoji 全丢（对应 JS 的 \\p{P}\\p{S}\\s）。"""
    cat = unicodedata.category(ch)
    return cat[0] in ("L", "N")


def normalize(text: object) -> str:
    s = "" if text is None else str(text)
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"\[[^\]]{1,16}\]", "", s)
    s = re.sub(r"回复\s*@[^:：]+[:：]", "", s)
    s = re.sub(r"https?://\S+", "", s)
    return "".join(ch for ch in s if _keep(ch))


def grams(text: object) -> set[str]:
    t = normalize(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def jaccard(a, b) -> float:
    A = a if isinstance(a, set) else grams(a)
    B = b if isinstance(b, set) else grams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def similarity(a, b) -> float:
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if len(x) < 4 or len(y) < 4:
        return 0.0
    return jaccard(x, y)


def lcs(a, b) -> int:
    """最长公共子串长度（只看归一化后的字）。"""
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0
    best = 0
    prev = [0] * (len(y) + 1)
    for i in range(1, len(x) + 1):
        diag = 0
        for j in range(1, len(y) + 1):
            tmp = prev[j]
            prev[j] = diag + 1 if x[i - 1] == y[j - 1] else 0
            if prev[j] > best:
                best = prev[j]
            diag = tmp
    return best


def cjk_in_common(a, b) -> int:
    """最长公共子串里有几个汉字（品牌名、数字不算照抄）。"""
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0
    best = 0
    end = 0
    prev = [0] * (len(y) + 1)
    for i in range(1, len(x) + 1):
        diag = 0
        for j in range(1, len(y) + 1):
            tmp = prev[j]
            prev[j] = diag + 1 if x[i - 1] == y[j - 1] else 0
            if prev[j] > best:
                best, end = prev[j], i
            diag = tmp
    return sum(1 for ch in x[end - best : end] if "\u4e00" <= ch <= "\u9fff")


# ── 引号 / 列表 / 引用上下文：这些地方的原句不算 ──────────────────
_QUOTE_RES = [
    re.compile(r"「[^」]*」"),
    re.compile(r"『[^』]*』"),
    re.compile(r"“[^”]*”"),
    re.compile(r"‘[^’]*’"),
    re.compile(r'"[^"\n]*"'),
    re.compile(r"'[^'\n]*'"),
]


def mask_quoted(text: object) -> str:
    out = "" if text is None else str(text)
    for rex in _QUOTE_RES:
        out = rex.sub(lambda m: " " * len(m.group(0)), out)
    # > 引用行
    out = re.sub(r"(^|\n)(\s*>.*)(?=\n|$)", lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), out)
    return out


def is_list_like(text: object) -> bool:
    s = str(text)
    numbered = len(re.findall(r"(^|\n)\s*\d{1,2}[.、]", s))
    lines = s.count("\n")
    return numbered >= 3 or (len(s) > 500 and lines >= 5)


_QUOTE_CTX = re.compile(r"引用|引述|复读|复制|刷|别人|有人|他说|她说|原话|这句|这种话|所谓|嘲讽|笑死|照抄|模仿|玩梗|烂梗")


def has_quote_context(text: object, phrase: str) -> bool:
    s = str(text)
    idx = s.find(phrase)
    if idx < 0:
        return False
    around = s[max(0, idx - 80) : idx + len(phrase) + 80]
    return bool(_QUOTE_CTX.search(around))


# ── 打分 ──────────────────────────────────────────────────────────
def score(text, *, for_check: bool = True, others=None, own=None, known=None) -> dict:
    """返回 {fish, hard, soft, reasons}。"""
    raw = "" if text is None else str(text).strip()
    if not raw:
        return {"fish": False, "hard": [], "soft": [], "reasons": ["empty"]}

    hard: list[str] = []
    soft: list[str] = []
    repeats: list[str] = []
    masked = mask_quoted(raw)
    list_like = is_list_like(raw)

    for phrase in HARD_PHRASES:
        if phrase not in masked:
            continue
        if list_like or has_quote_context(raw, phrase):
            continue
        if phrase not in hard:
            hard.append(phrase)

    soft_text = normalize(masked)
    for code, rex in SOFT_PATTERNS:
        if rex.search(soft_text) and code not in soft:
            soft.append(code)

    for pool, tag in ((known or [], "known_fish"), (own or [], "own")):
        for cand in pool:
            if similarity(raw, cand) >= 0.8:
                repeats.append(f"repeat:{tag}")
                break

    if others and len(normalize(raw)) >= 8:
        for cand in others:
            if similarity(raw, cand) >= 0.75:
                repeats.append("repeat:other_source")
                break

    fish = bool(hard) or (for_check and (len(soft) >= 2 or (len(soft) >= 1 and bool(repeats))))
    return {"fish": fish, "hard": hard, "soft": soft, "repeats": repeats}


def blacklist_hits(text: object) -> list[str]:
    s = "" if text is None else str(text)
    return [why for rex, why in BLACKLIST if rex.search(s)]


def nearest(text: object, pool) -> tuple[float, str]:
    """在一堆 {text} 或字符串里找最像的一条。"""
    g = grams(text)
    best_s, best_t = 0.0, ""
    for item in pool:
        t = item.get("text", "") if isinstance(item, dict) else str(item)
        s = jaccard(g, grams(t))
        if s > best_s:
            best_s, best_t = s, t
    return best_s, best_t


def check(text: object, *, keep_said=None, fish_pool=None, human_pool=None, said_pool=None) -> dict:
    """发之前过一遍。只报问题，不改写。

    keep_said(text) 传进来就在通过时记一笔（去重用）。
    """
    t = "" if text is None else str(text).strip()
    problems: list[str] = []
    notes: list[str] = []
    if not t:
        return {"ok": False, "problems": ["没有内容"], "notes": notes}

    direct = score(t, for_check=True)
    for h in direct["hard"]:
        problems.append(f"鱼八股原句：「{h}」")
    for code in direct["soft"]:
        notes.append(f"有 AI 腔：{SOFT_NAMES.get(code, code)}")
    if direct["fish"] and not direct["hard"]:
        problems.append("AI 腔叠了好几处，太像鱼了")

    for why in blacklist_hits(t):
        if not any(h in why for h in direct["hard"]):
            problems.append(f"撞黑名单：{why}")

    if fish_pool:
        s, near = nearest(t, fish_pool)
        if s >= 0.5:
            problems.append(f"跟抓到的鱼评太像（{round(s * 100)}%）：「{near[:40]}」")

    if said_pool:
        s, near = nearest(t, said_pool)
        if s >= 0.6:
            problems.append(f"跟你以前说过的话太像（{round(s * 100)}%）：「{near[:40]}」")

    if human_pool:
        for h in human_pool:
            src = h.get("text", "") if isinstance(h, dict) else str(h)
            n = lcs(t, src)
            if n >= 6 and cjk_in_common(t, src) < 4:
                continue
            if n >= 10 or (n >= 6 and n >= len(src) * 0.8):
                problems.append(f"照抄了语料原句（连续 {n} 个字一样）：「{src[:40]}」")
                break

    ok = not problems
    if ok and keep_said:
        try:
            keep_said(t)
        except Exception:
            pass
    return {"ok": ok, "problems": problems, "notes": notes}
