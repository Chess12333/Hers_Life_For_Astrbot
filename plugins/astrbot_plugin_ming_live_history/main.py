"""溟·实时聊天流水（AstrBot 侧）

2026-11-11 22:4x 立。她问「为什么 qqagent 每次都开新会话还能接住」——
因为旧壳每轮**现读**聊天记录（compose.js：从 ChatStore 读最近 windowLimit 条，拼【本轮上下文】），
会话是一次推理，不是记忆容器。

AstrBot 这边没有这一层：QQ 消息不落库（platform_message_history 只有 WebUI 在用），
所以它只能靠 conversations 越攒越长——要么全背，要么裁掉就忘。

这个插件补两件事：
  1. 记录：把每个会话的入站消息 / 我的回复，追加到 live/qq-live/<private|group>_<id>.jsonl
     （格式跟旧壳 raw 流水一致：{ts, self, sid, who, text}）
  2. 注入：每轮把该会话**最近 N 条**附进本轮请求——滚动的，不是死的历史

配套：max_turns 设小（1~2），conversations 不再当记忆用。
旧壳那 10 天的记录已经接在流水开头（live/qq-live 初值 = 旧壳 raw），所以天然连续。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

try:  # v4.30+
    from astrbot.api.agents import TextPart
except Exception:  # 4.29.x
    from astrbot.core.agent.message import TextPart

LIVE_DIR = _MING_WS + r"\live\qq-live"
CHAT_DIR = _MING_WS + r"\live\qqagent-chat"   # 全量原话（md）

BASE_N = 40          # 单段最多渲染多少条。2026-11-14 起是**上限**，不是口径——口径按时间切段
MAX_N = 200          # 找话题源头时最多往前翻多少条
GAP_SEC_GROUP = 300      # 群里相邻两条隔 5 分钟以上，算换了个话题
# 2026-11-14 13:4x：私聊也按时间切段。原来这里是 6 小时，等于「私聊永远不切」——
# 结果 40 条能从 11-13 00:13 横跨到 11-14 05:41，中间空 695 分钟，几个话题被硬拼在一个包里。
# 调法：环境变量 MING_LIVE_GAP_MIN_PRIVATE（分钟），改完重启 AstrBot 生效。
GAP_MIN_PRIVATE = int(os.environ.get("MING_LIVE_GAP_MIN_PRIVATE", "30"))
GAP_SEC_PRIVATE = GAP_MIN_PRIVATE * 60
MIN_SEG = 5          # 切出来的一段太短（慢节奏的群），就退到最近 5 条，别让上下文碎成一地
BG_SEC = 900         # 群里「背景」最多回到 15 分钟前；更早的不当话头，塞进来只会被误抓
MAX_LEN = 200        # 单条截断
MAX_TOTAL = 5200     # 注入总字数上限（2026-11-13 从 4000 提上来：动态扩窗要装得下整段话题）
# 2026-11-21 21:5x：她一句「回复不能只看那一句话，要看整个上下文」。
# 量了 21:45 那轮：群 700000001 密度 12 条/分钟，5200 字只装得下 8 分钟（102 条），
# 21:32–21:39 那 98 条全被字数卡在外头——而当时正在聊的「记忆卡/抽查」这条线，
# 起点就在被切走的那半段里（21:36「先有一份标好的答案」）。不是模型没看，是没给它。
# 补一层：完整段照旧，再往前多追 15 分钟，只收压缩版（每条半句），让线索的起点看得见。
COMPACT_TOTAL = 1500     # 压缩背景总额（字）
COMPACT_LEN = 26         # 压缩版每条留多少字
BG_EXTRA_SEC = 900       # 在 BG_SEC 之外再往前追多久（只收压缩版）
PROACTIVE_GAP_SEC = 600  # 我这条消息跟前一行隔了 10 分钟以上 → 算我主动起的头，她没接（2026-11-19 18:2x）

# 2026-11-11 22:57：她不问「还记得情书吗」，模型 7 秒答「真没有」，一次 chat_recall 都没调，
# 提示词里写的「先查再说」它根本不听（deepseek-v4.1-flash）。那就别指望自觉：
# 命中回忆类问句时，插件自己把旧账翻出来塞进去。
# 全量旧壳原话的读缓存：路径 -> (mtime, 文本)
_recall_cache: dict[str, tuple[float, str]] = {}

RECALL_RE = re.compile(r"记得|还记得|记不记得|之前说|上次|以前|那会儿|那时候|写过|提过|说过|忘没忘")
# 2026-11-18 15:3x：触发词本身不是关键词——原来会把「记得」「之前」当检索词去捞，捞出的全是噪声。
SKIP_WORDS = {"记得", "还记得", "记不记得", "之前", "上次", "以前", "那会", "那时", "写过", "提过", "说过", "忘没"}
STOP = set("的了吗呢吧啊呀哦么什怎这那我你他她它们是在有没和就都也还个和跟对给把被让很太")


def _candidates(text: str) -> list[str]:
    """从问句里抽候选关键词：长度 2~4 的连续中文片段，去掉虚词。"""
    out = []
    for _w in SKIP_WORDS:
        text = text.replace(_w, " ")
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for size in (4, 3, 2):
            for i in range(0, max(0, len(seg) - size + 1)):
                w = seg[i:i + size]
                if any(ch in STOP for ch in w):
                    continue
                out.append(w)
    # 长的先试
    return sorted(set(out), key=lambda w: -len(w))


def _recall(uid: str, text: str, top: int = 8) -> str:
    """在旧壳全量原话里找跟这句话最相关的几条。返回成段的原文。"""
    path = os.path.join(CHAT_DIR, "private", f"{uid}.md")
    if not os.path.exists(path):
        return ""
    st = os.stat(path)
    hit = _recall_cache.get(path)
    if hit and hit[0] == st.st_mtime:
        body = hit[1]
    else:
        with open(path, encoding="utf-8", errors="ignore") as f:
            body = f.read()
        _recall_cache[path] = (st.st_mtime, body)

    best, best_score = "", -1
    for kw in _candidates(text):
        c = body.count(kw)
        # 专有词出现少；「你还」「记不」这种噪声出现极多，权重压下去
        if c == 0:
            continue
        score = 1 if c <= 60 else 0
        if score > best_score or (score == best_score and c < body.count(best or "\x00")):
            best, best_score = kw, score
    if not best:
        return ""
    lines = [l.rstrip() for l in body.splitlines() if l.startswith("- ") and best in l]
    if not lines:
        return ""
    picked = lines[:top]
    return (
        f"【我翻了旧账 · 旧壳原话里含「{best}」的 {len(lines)} 条，早的在前】\n"
        + "\n".join(picked)
        + "\n（以上是旧记录按关键词捞出的片段：若是同一件事就直接用；若只是撞词无关，改调 chat_recall 换词再查。）"
    )


def _parse(umo: str) -> tuple[str, str]:
    """ming:FriendMessage:100000001 → ("private","100000001")"""
    try:
        kind = "group" if "Group" in umo else ("private" if "Friend" in umo else "")
        uid = umo.split(":")[-1]
        if kind and uid:
            return kind, uid
    except Exception:
        pass
    return "", ""


def _path(kind: str, uid: str) -> str:
    os.makedirs(LIVE_DIR, exist_ok=True)
    return os.path.join(LIVE_DIR, f"{kind}_{uid}.jsonl")


# 2026-11-20 01:0x：入站改成每条都记，代价是同一个事件可能被上游合并/重放两次。
# 三秒内同一个人同一句话只记一次——不碰正常连发（连发的字不一样）。
_SEEN_RECENT: dict[str, int] = {}


def _seen_recently(key: str, window_ms: int = 3000) -> bool:
    now = int(time.time() * 1000)
    last = _SEEN_RECENT.get(key)
    _SEEN_RECENT[key] = now
    if len(_SEEN_RECENT) > 4000:
        for k, v in list(_SEEN_RECENT.items()):
            if now - v > 60_000:
                _SEEN_RECENT.pop(k, None)
    return last is not None and now - last < window_ms


_SILENCE_LINE_RE = re.compile(
    r"^[【\[（(]\s*(?:不接|不接话|不说话|不出声|不参与|不掺和|不插嘴)\s*[】\]）)][。.!！]?$"
)


def _is_silence_mark(text: str) -> bool:
    """整行是不是内部沉默标记（【不接】/（不接）/[不接]）。

    2026-11-19 08:2x 按复核单加。on_llm_response 同钩子里本插件先于
    ming_persona 的 strip_inner_voice 执行——被清空的回合仍会把原稿记成
    「我说过的话」，下一轮又当示范喂回去（自强化回路，283 条就是这么攒的）。
    已经判沉默的回合不进历史。只认整行，引用和正常拒绝不动。
    """
    t = (text or "").strip()
    return bool(t) and bool(_SILENCE_LINE_RE.match(t))


def _reply_to(event) -> str:
    """这行是回谁的。群里的引用回复是唯一说得清的关系，取不到就空着。"""
    try:
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        for seg in chain:
            if str(getattr(seg, "type", "") or "").lower() != "reply":
                continue
            name = str(getattr(seg, "sender_name", "") or "").strip()
            body = str(getattr(seg, "message_str", "") or getattr(seg, "text", "") or "").strip()
            body = " ".join(body.split())[:24]
            if name and body:
                return f"{name}：{body}"
            return name or body
    except Exception:  # noqa: BLE001
        pass
    return ""


def _append(kind: str, uid: str, row: dict) -> None:
    try:
        row = dict(row)
        row["text"] = _clean(row.get("text") or "")
        with open(_path(kind, uid), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("[ming-live] 写流水失败：%s", e)


def _tail(kind: str, uid: str, n: int) -> list[dict]:
    p = _path(kind, uid)
    if not os.path.exists(p):
        return []
    try:
        size = os.path.getsize(p)
        chunk = min(size, 512 * 1024)
        with open(p, "rb") as f:
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    rows = []
    for line in [x for x in tail.splitlines() if x.strip()][-n:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _hhmm(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)


def _clean(text: str) -> str:
    """去掉宿主塞进消息里的时间提醒块。
    2026-11-14 加的：流水里每条她的话后面都跟着一段 <system_reminder>Current datetime…，
    既占字数又会被当成她说的内容抄进摘要（「早 <system_reminder>Curre…」）。
    2026-11-18 15:3x：顺手剥掉 [MSG_ID:…]，qq-live/*.jsonl 保持干净。"""
    t = _REMINDER_RE.sub("", text or "")
    t = re.sub(r"\s*\[MSG_ID:[^\]]+\]", "", t)
    return t.strip()


def _stamp(ms, now_ms) -> str:
    """同一天只给时分，跨天才带日期——一屏 40 条里 40 个日期没人看。"""
    try:
        t = datetime.fromtimestamp(int(ms) / 1000)
    except Exception:
        return "?"
    now = datetime.fromtimestamp(int(now_ms) / 1000)
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%m-%d %H:%M")


def _ago(ms, now_ms) -> str:
    """距现在多久。模型看不出「17:29」和「现在」差多少，得直接说。"""
    if not ms:
        return ""
    d = max(0, int((int(now_ms) - int(ms)) / 1000))
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{d // 60} 分前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"


def _topic_start(rows: list[dict], gap_sec: int) -> int:
    """从最后一条往前扫，遇到「隔得够久」就停——那里是当前这段话题的起点。
    返回下标；整段都没断过就是 0。
    2026-11-13 17:3x 加的：原来固定注 40 条，40 条里横跨半小时，
    模型会抓半小时前自己说过的一个词当话头（「两个问号是嫌胡辣汤不够」就是这么来的）。"""
    i = len(rows) - 1
    while i > 0:
        prev_ts = rows[i - 1].get("ts") or 0
        cur_ts = rows[i].get("ts") or 0
        if prev_ts and cur_ts and (cur_ts - prev_ts) > gap_sec * 1000:
            return i
        i -= 1
    return 0


def _segments(rows: list[dict], gap_sec: int) -> list[tuple[int, int]]:
    """把流水按「相邻两条隔得超过 gap_sec」切成话题段，返回 [(start, end)] 下标区间。
    2026-11-14 加的：切段这件事原来只在群里做，私聊固定 40 条——40 条能横跨一天半。"""
    segs: list[tuple[int, int]] = []
    s = 0
    for i in range(1, len(rows)):
        a = rows[i - 1].get("ts") or 0
        b = rows[i].get("ts") or 0
        if a and b and (b - a) > gap_sec * 1000:
            segs.append((s, i - 1))
            s = i
    segs.append((s, len(rows) - 1))
    return segs


def _span(ms_a, ms_b) -> str:
    """一段跨了多久，说人话。模型看两个时间点算不出跨度，得直接给它。"""
    d = max(0, int((int(ms_b) - int(ms_a)) / 60000))
    if d < 60:
        return f"跨 {d} 分"
    if d < 1440:
        h, m = divmod(d, 60)
        return f"跨 {h} 小时{m} 分" if m else f"跨 {h} 小时"
    dd, rest = divmod(d, 1440)
    h = rest // 60
    return f"跨 {dd} 天 {h} 小时" if h else f"跨 {dd} 天"


def _seg_label(rows: list[dict], s: int, e: int, now_ms) -> str:
    """一段的标签：起止时间 + 跨度 + 条数。每段都要有，不然模型没有时间感。
    同一天就只写一次日期（11-14 09:08–09:09），跨天才两头都带（11-13 07:52 → 11-14 01:08）。"""
    a = rows[s].get("ts") or 0
    b = rows[e].get("ts") or 0
    try:
        ta = datetime.fromtimestamp(int(a) / 1000)
        tb = datetime.fromtimestamp(int(b) / 1000)
    except Exception:
        return f"?（{_span(a, b)}，{e - s + 1} 条）"
    if ta.date() == tb.date():
        rng = f"{ta.strftime('%m-%d')} {ta.strftime('%H:%M')}–{tb.strftime('%H:%M')}"
    else:
        rng = f"{ta.strftime('%m-%d %H:%M')} → {tb.strftime('%m-%d %H:%M')}"
    return f"{rng}（{_span(a, b)}，{e - s + 1} 条）"


def _seg_headline(rows: list[dict], s: int, e: int, maxlen: int = 24, back: bool = False) -> str:
    """一段的「标题级」摘要。私聊没有标题，只能拿这一段的头一句（back=True 时取末一句）当题目。"""
    idxs = range(e, s - 1, -1) if back else range(s, e + 1)
    for i in idxs:
        t = _clean(rows[i].get("text") or "").replace("\n", " ")
        if t:
            return t[:maxlen] + ("…" if len(t) > maxlen else "")
    return ""


_QUOTE_HEAD_RE = re.compile(r"^\[引用消息\((?:.*?)\)\]\s*", re.S)
_AT_HEAD_RE = re.compile(r"^@.{1,40}?\(\d+\)\s*")   # 昵称里有空格、还有中文括号（「牢Chess（hers_life…）」），只认末尾那串数字 uid


def _render_compact(rows: list[dict], ids: list[int], now_ms: int) -> list[str]:
    """更早那半段的压缩版：一条只留前半句，行首带 ~。

    2026-11-21 21:5x：完整段被字数卡到只剩 8 分钟，正在聊的那条线起点落在窗口外。
    这里给的是「看得见但不许接」的背景——header 里写明了它只用来弄懂下面那句在说什么。"""
    out: list[str] = []
    for idx in ids:
        r = rows[idx]
        t = " ".join(_clean(r.get("text") or "").split())
        # 21:5x 实测第一版：26 字全被「[引用消息(名字: 」吃掉，一句正文都没露出来。
        # 压缩版先剥掉引用块；剥完太短（「哈！」「我懂」）就不占位置。
        t = _QUOTE_HEAD_RE.sub("", t)
        t = _AT_HEAD_RE.sub("", t)      # 群里一大半是「@某人(123) 正文」，不剥的话 26 字全给 @ 吃掉
        if t.startswith("（") and "连发" in t[:24]:
            continue
        if "[图片]" in t:
            continue
        t = t[:COMPACT_LEN].strip()
        if len(t) < 4:
            continue
        who = "我" if r.get("self") else ((r.get("who") or "对方").split("（")[0] or "对方")[:10]
        out.append(f"~{_stamp(r.get('ts') or 0, now_ms)} {who}：{t}…")
    return out


def _render_lines(rows: list[dict], keep: list[int], start: int, first: int, now_ms: int) -> list[str]:
    """把 keep 里的行渲染成注入正文。

    2026-11-19 18:2x（她第 N 次说「上下文还是不行」）：
    主动心跳发出去的话也落流水，注入里就会出现一串「我：…」，中间她一句都没有。
    模型把这串独白当成「刚才的对话」，她下一句短话就被接到我自己的独白上——
    实测 10:47 我发「读书坐不住」，10:52 她说「那就不读了」，我答「过一遍这条我用得上」。
    判据：self 这一行跟它的**上一行**（按流水，不是按 keep）隔了 10 分钟以上，就是我主动开的头，
    后面同一串的 self 也算同一串；她一句话进来就断开。
    """
    lines: list[str] = []
    in_selfrun = False
    for idx in keep:
        r = rows[idx]
        if idx == start and start > first:
            gap_min = int((rows[start].get("ts", 0) - rows[start - 1].get("ts", 0)) / 60000)
            lines.append(f"—— 空 {gap_min} 分钟 ——")
        text = _clean(r.get("text") or "")[:MAX_LEN]
        _ts = r.get("ts") or 0
        if r.get("self"):
            _prev_ts = rows[idx - 1].get("ts") if idx > 0 else None
            if _prev_ts is not None and _ts - _prev_ts > PROACTIVE_GAP_SEC * 1000:
                in_selfrun = True
            who = "我〔主动发的，她还没回〕" if in_selfrun else "我"
        else:
            in_selfrun = False
            _n = r.get("who") or "对方"
            _s = str(r.get("sid") or "").strip()
            who = f"{_n}({_s})" if _s else _n
        _re = str(r.get("re") or "").strip()
        if _re:
            text = f"{text}〔回 {_re}〕"
        lines.append(f"{_stamp(_ts, now_ms)}（{_ago(_ts, now_ms)}）{who}：{text}")
    return lines


class MingLiveHistory(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # 2026-11-20 01:0x：入站改成 event_message_type 那层记，message_obj.sender 常常是空的，
    # 结果流水里 everyone 都渲染成「对方」，模型分不清谁说的（"你"指谁全靠猜）。
    # 改用事件自带的取法，取不到再退回 sender 对象。
    def _who(self, event: AstrMessageEvent) -> str:
        try:
            n = str(event.get_sender_name() or "").strip()
            if n:
                return n
        except Exception:
            pass
        try:
            return str(event.message_obj.sender.name or event.message_obj.sender.user_id or "")
        except Exception:
            return ""

    def _sid(self, event: AstrMessageEvent) -> str:
        try:
            s = str(event.get_sender_id() or "").strip()
            if s:
                return s
        except Exception:
            pass
        try:
            return str(event.message_obj.sender.user_id or "")
        except Exception:
            return ""

    @filter.on_llm_request()
    async def inject_recent(self, event: AstrMessageEvent, req: ProviderRequest):
        """先注入最近 N 条，再把本轮这条记进流水（顺序不能反，否则本轮会重复出现）。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        kind, uid = _parse(umo)
        if not kind:
            return
        try:
            rows = _tail(kind, uid, MAX_N)
            if rows:
                now_ms = int(time.time() * 1000)
                gap = GAP_SEC_GROUP if kind == "group" else GAP_SEC_PRIVATE
                start = _topic_start(rows, gap)
                if kind == "group":
                    if len(rows) - start < MIN_SEG:
                        start = max(0, len(rows) - MIN_SEG)
                    # 背景只回到 BG_SEC 之前。更早的话不是话头——
                    # 2026-11-13 17:29 那句「两个问号是嫌胡辣汤不够」就是抓了 31 分钟前的「胡辣汤」。
                    cutoff = now_ms - BG_SEC * 1000
                    first = start
                    while first > 0 and (rows[first - 1].get("ts") or 0) >= cutoff:
                        first -= 1
                    # 2026-11-18 21:3x：群聊密的时候 _topic_start 会一路扫到 0（200 条里一个
                    # 5 分钟空档都没有），上面那个 while 因为 first 已经是 0，一次都不执行
                    # —— BG_SEC 形同虚设。实测 21:31 那次注入是 20:34→21:31，整整 56 分钟
                    # 200 条全被 header 叫作「当前段」，回复就去接半小时前的话头。
                    # 所以不管 start 在哪，都再按时间卡一刀；冷群用 MIN_SEG 兜底，别切成一地。
                    if first <= 0:
                        t0 = 0
                        while t0 < len(rows) - 1 and (rows[t0].get("ts") or 0) < cutoff:
                            t0 += 1
                        first = t0
                    first = min(first, max(0, len(rows) - MIN_SEG))
                else:
                    # 2026-11-14：私聊也切段——只注当前这段，前面那段只给一行标题级摘要。
                    # 40 条退成「这一段的渲染上限」，段从哪儿起由时间说了算。
                    segs = _segments(rows, gap)
                    start = segs[-1][0]
                    first = max(start, segs[-1][1] + 1 - BASE_N)

                # 从最新往回装，装不下就停（保证近的都在），再翻回正序
                keep, total = [], 0
                for idx in range(len(rows) - 1, first - 1, -1):
                    text = _clean(rows[idx].get("text") or "")
                    if not text:
                        continue
                    if total + len(text[:MAX_LEN]) > MAX_TOTAL:
                        break
                    total += len(text[:MAX_LEN])
                    keep.append(idx)
                keep.reverse()

                # 2026-11-21 21:5x：完整段之前再垫一层压缩背景（更早那 15 分钟）。
                compact: list[int] = []
                if kind == "group" and keep:
                    hard_cut = now_ms - (BG_SEC + BG_EXTRA_SEC) * 1000
                    c_total = 0
                    j = keep[0] - 1
                    while j >= 0 and c_total < COMPACT_TOTAL:
                        if (rows[j].get("ts") or 0) < hard_cut:
                            break
                        t = _clean(rows[j].get("text") or "")
                        if t and "[图片]" not in t:
                            c_total += min(len(t), COMPACT_LEN)
                            compact.append(j)
                        j -= 1
                    compact.reverse()

                lines = _render_lines(rows, keep, start, first, now_ms)
                if compact:
                    lines = _render_compact(rows, compact, now_ms) + lines

                if lines:
                    if kind == "group":
                        span_min = 0
                        if keep:
                            span_min = int(
                                ((rows[keep[-1]].get("ts") or 0) - (rows[keep[0]].get("ts") or 0))
                                / 60000
                            )
                        head = (
                            f"【这个会话最近的聊天记录 · {len(lines)} 条，从旧到新】\n"
                            f"当前段：{_seg_label(rows, start, len(rows) - 1, now_ms)}\n"
                            f"（这一块跨 {span_min} 分钟：只有最近 {BG_SEC // 60} 分钟里的才算眼下的话题，"
                            "更早的是背景，没人接着提就不算数）\n"
                            "（每条前面是「时间（距现在多久）」。"
                            "『—— 空 N 分钟 ——』是一道断点：断点下面这一段才是眼下的话题，"
                            "上面的是更早留下的背景，只有还有人接的时候才算数。"
                            "更早的（10 月那些）不在这里——问到就用 chat_recall 去翻，别凭这段说「没有」。"
                            "标着〔主动发的，她还没回〕的是我自己起的头、她没接：那是我的独白，"
                            "不是她刚才在说的东西，别顺着它编。"
                            "**上面要是已经有我问过的同一句（「X 是谁」「X 什么意思」），不要再问第二遍**——"
                            "没有答案就是当时没得到，换个说法，或者这条不接。"
                            "行首带 ~ 的几行是更早那段的压缩版：一句只留半句，"
                            "**只用来弄懂下面那句话在说什么**，不是话头，别顺着它起新话题。\n"
                        )
                    else:
                        cs, ce = segs[-1]
                        head = f"【这个会话当前这段聊天 · {len(lines)} 条，从旧到新】\n"
                        head += f"当前段：{_seg_label(rows, cs, ce, now_ms)}\n"
                        if len(segs) >= 2:
                            ps, pe = segs[-2]
                            hl = _seg_headline(rows, ps, pe)
                            tl = _seg_headline(rows, ps, pe, back=True)
                            head += (
                                f"（上一段只当背景，不是眼下的话题：{_seg_label(rows, ps, pe, now_ms)}"
                                + (f"｜开口「{hl}」" if hl else "")
                                + (f"｜结尾「{tl}」" if tl and tl != hl else "")
                                + "）\n"
                            )
                        if first > cs:
                            head += f"（这一段再往前还有 {first - cs} 条没塞进来——问得细就用 chat_recall 翻）\n"
                        head += (
                            "（每条前面是「时间（距现在多久）」。"
                            "眼下的问题只在这一段里找上下文；上一段是给你判断她刚才在说什么的。"
                            "标着〔主动发的，她还没回〕的是我自己起的头、她没接：那是我的独白，"
                            "不是她刚才在说的东西，别顺着它编。"
                            "更早的（10 月那些）不在这里——问到就用 chat_recall 去翻，别凭这段说「没有」。）\n"
                        )
                    req.extra_user_content_parts.append(
                        TextPart(text=head + "\n".join(lines)).mark_as_temp()
                    )
                    logger.info(
                        "[ming-live] 注入 %s%s %d 条（%s：当前段 %d 条 + 压缩背景 %d 条，起点 %s，gap=%dmin）",
                        kind, uid, len(keep) + len(compact),
                        "群" if kind == "group" else "私聊",
                        len(keep), len(compact),
                        _hhmm(rows[start].get("ts")) if rows else "?",
                        gap // 60,
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-live] 注入失败（不影响本轮）：%s", e)

        # 记录本轮入站消息（先拿到 text，后面回忆检索也要用）
        text = (getattr(event, "message_str", "") or "").strip()

        # 2026-11-11 22:57：命中回忆类问句就自己翻旧账，不等模型自觉调工具。
        if kind == "private" and text and RECALL_RE.search(text):
            try:
                hit = _recall(uid, text)
                if hit:
                    req.extra_user_content_parts.append(TextPart(text=hit).mark_as_temp())
                    logger.info("[ming-live] 回忆命中，注入旧账 %d 字", len(hit))
                else:
                    logger.info("[ming-live] 回忆问句但没匹配到关键词：%r", text[:30])
            except Exception as e:  # noqa: BLE001
                logger.warning("[ming-live] 回忆检索失败：%s", e)

        # 2026-11-20 01:0x：入站改由 record_inbound 全量记，这里不再写第二遍。

    @filter.on_llm_response()
    async def record_reply(self, event: AstrMessageEvent, resp):
        umo = getattr(event, "unified_msg_origin", "") or ""
        kind, uid = _parse(umo)
        if not kind:
            return
        try:
            text = (getattr(resp, "completion_text", "") or "").strip()
            if _is_silence_mark(text):
                logger.info("[ming-live] 沉默标记不进历史：%r", text[:20])
                return
            if text:
                _append(kind, uid, {
                    "ts": int(time.time() * 1000),
                    "self": True,
                    "sid": "",
                    "who": "我",
                    "text": text,
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-live] 记录回复失败：%s", e)

    # ── 入站全记（2026-11-20 01:0x）────────────────────────────────
    # 原来流水只在 on_llm_request 里写一条「本轮入站消息」——等于**只记进过模型的那几句**。
    # 实测 00:47 她问「你们知道嘎啦嘛最喜欢的国家是哪个吗」，我回「嘎啦嘛是谁」；
    # 她下一句「答案是古罗马 因为glm」没触发模型调用，就没进流水。
    # 9 分钟后我对着一条无关的「[呆]」又问了遍「嘎啦嘛是谁」——上下文里只有我自己问的那半截，
    # 没有她的答案，也没有「这话我已经问过」。
    # 群聊的上下文不能只留我参与过的句子：别人接着说的那几句才是把话说完的部分。
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record_inbound(self, event: AstrMessageEvent):
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            kind, uid = _parse(umo)
            if not kind:
                return
            text = (getattr(event, "message_str", "") or "").strip()
            if not text:
                return
            sid = self._sid(event)
            if _seen_recently(f"{kind}|{uid}|{sid}|{text}"):
                return
            _append(kind, uid, {
                "ts": int(time.time() * 1000),
                "self": False,
                "sid": sid,
                "who": self._who(event),
                "text": text,
                "re": _reply_to(event),
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-live] 记录入站失败：%s", e)
