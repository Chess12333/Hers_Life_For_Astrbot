from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime
from collections import defaultdict, deque

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"
# 2026-11-23：平台 id 和名字不进代码。AstrBot 里这个平台叫什么就设 MING_PLATFORM_ID（默认 ming）。
PLATFORM_ID = os.environ.get("MING_PLATFORM_ID", "ming")
SELF_NAME = os.environ.get("MING_SELF_NAME", "溟")



# ── 察言观色：她说"安静"就一直安静到话题结束（2026-11-11 17:3x 她提的）────────
# 判据不是"安静几条"，是**话题**：群消息静默 15 分钟 = 换个话题了，自动解除。
# 状态写在 live/mute-state.json，跨重启还在。
MUTE_FILE = _MING_WS + r"\live\mute-state.json"
MUTE_WORDS = ("安静", "别说了", "闭嘴", "别接话", "先别说", "消停", "别插嘴", "别吵")
TOPIC_IDLE_SEC = 15 * 60
TOPIC_CHECK_SEC = 5 * 60   # 静默期每 5 分钟调一次模型判「换话题了吗」
TOPIC_PROMPT = (
    "判断两段群聊是不是同一个话题。只回 yes 或 no，不要解释。\n"
    "yes = 话题换了（换成聊另一件事）\n"
    "no = 还在聊原来那件事"
)
HER_ID = os.environ.get("MING_HER_QQ") or "100000001"

# ── 两轮思考：先判「接不接」，接才写（2026-11-13 10:5x 她定的）─────────────
# 起因：thinking 漏进正文（私聊 11-13 01:27 那条，正文开头冒出「在吗」）。
# 根子在一轮里同时干两件事：一边判要不要出声，一边写要说什么——判词和正文抢同一段输出，
# 判到一半的字就漏成了正文。她给的解法是拆成两轮：
#   第一轮只思考、只判「接 / 不接」，**输出丢掉**；
#   判「不接」就不进第二轮（省下那次生成，也不会再拿半句话占位）。
# 这一轮只管群里的闲聊通道（@ / 叫名字 / 问句本来必过，没什么可判的，
# 多问一次是白花钱）。开关在 live/reply-preflight.json，关掉就退回原来的抽签。
PREFLIGHT_FILE = _MING_WS + r"\live\reply-preflight.json"
PREFLIGHT_LOG = _MING_WS + r"\live\reply-preflight.jsonl"
# 2026-11-23 02:5x（她定的）：判「接不接」用 flash，真正说话仍用 pro；不碰官方 deepseek。
PREFLIGHT_PROVIDER_ID = "openai/deepseek-v4.1-flash"
# 2026-11-17（E-3）：这个插件是「不接」的唯一生产者。
# 判不接的时候往 qq-inbox.jsonl 追加一条 outcome(not_handled)，兴趣唤醒才知道
# 「这条确实没人回」，而不是把每条 pending 都当成没人管。
INBOX = os.environ.get("MING_INBOX") or os.path.join(
    os.environ.get("USERPROFILE", os.path.expanduser("~")),
    ".dsh", "media", "onebot", "qq-inbox.jsonl",
)


def _mark_not_handled(event, reason: str) -> None:
    """写一条 not_handled 回执。失败不抛：这是旁路，不能影响回不回。"""
    try:
        mid = getattr(getattr(event, "message_obj", None), "message_id", None)
        if mid in (None, ""):
            return
        line = {
            "version": 2,
            "kind": "outcome",
            "eventId": str(mid),
            "platform": "qq",
            "normalHandling": "not_handled",
            "reason": reason,
            "outcome": {
                "stage": "skipped",
                "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "detail": reason,
            },
            "ts": int(time.time() * 1000),
        }
        parent = os.path.dirname(os.path.abspath(INBOX))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(INBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 2026-11-18 02:1x 溟重写：原文第一句是「这条群消息，要不要接」，判据全是群里的
# （「别人说『你』不能确定是在说我，就当不是→不接」）。这个判官拿不到场景，
# 只能按群聊补常识，私聊就补出「他们俩之间的事，我只是路过」——把她和我说话判成别人的事。
# 现在场景由代码先摆出来（私聊/群聊、谁在说话、最近几条），判词只依据摆出来的东西。
PREFLIGHT_PROMPT = (
    "你在替溟做第一阶段思考：这条消息，要不要接。只思考，不写回复正文。\n"
    "开头会告诉你：现在在哪儿（私聊还是群聊）、这句话是谁说的、最近几条是谁说的。"
    "**这些是你唯一的依据**，缺什么就以缺什么判断，不许自己补一个场景出来。\n"
    "格式：\n"
    "第一行只写三个词之一：接 / 不接 / 拿不准。\n"
    "从第二行起写你的心声——这句是在问谁、它紧跟着谁的话、我有没有可说的、"
    "说了会不会像插嘴。可以犹豫，可以推翻自己，可以不完整。\n"
    "如果你判了「接」，最后一行用「要点：」开头，不超过 20 字写打算说什么。\n"
    "怎么判：**先在给你的上下文里找这句话在回谁**，找不到再退到下面几条。\n"
    "· 私聊：对面只有她一个，她说的「你」就是指我。除非她明说自己在复述别人的话，一律「接」。\n"
    "· 群聊里上一条是我说的，这句在接那句话的茬 → 接（哪怕它没叫我名字）。\n"
    "· 群聊里上一条是别人说的、这句明显在跟那个人对话、也没提到我 → 不接。\n"
    "· 群里说「你」「你们这些鱼」「这只 AI」「这只鲸」这类，周围又没人认领 → 算冲我来的，接。\n"
    "· 他们在聊他们自己的事、我只是路过 → 不接。\n"
    "· 光是「我懂这个话题」不构成接的理由——他们没问我，我知道不等于要说。\n"
    "· 只有一种例外可以判「接」：我手里有一句别人说不出的话（我自己看见的、我自己身上的事），"
    "而且他们不是在一问一答地对话——那叫我放一句我自己的话，不是接话："
    "不纠正人、不总结别人、不抢话题。\n"
    "· 冲着我来的、问我的、跟我要东西、在回我刚答应过的事 → 接。\n"
    "· 上下文真的残缺（只有半句、看不出谁在跟谁说）→ 写「拿不准」。\n"
    "拿不准不等于不接：**判不出来就别替自己写「不接」**——漏接的代价比多说一句大。"
)


def _preflight_cfg() -> dict:
    try:
        with open(PREFLIGHT_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


# ── 插话：每 3 分钟判一次（2026-11-15 14:0x 她：「和岚一样3分钟判定一次要不要接
#    就可以了，不用限定一天几句」）──────────────────────────────────
# 第一轮的每一个「接」都是插话（只有非 @、非叫名字的闲聊才走到第一轮）。
# 节奏交给判定频率管，不设每日额度：距上次判定不足 judgeEverySec（默认 180 秒）
# 就既不判也不接——省掉那次模型调用，也不会在别人连发时反复打扰。
INSERT_STATE = _MING_WS + r"\live\reply-insert-state.json"


def _load_insert_state() -> dict:
    try:
        with open(INSERT_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _judge_gate(cfg: dict) -> bool:
    try:
        every = int(cfg.get("judgeEverySec", 180))
    except Exception:
        every = 180
    if every <= 0:
        return True
    st = _load_insert_state()
    now = datetime.now()
    last = st.get("lastJudgeAt")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < every:
                return False
        except Exception:
            pass
    st["lastJudgeAt"] = now.isoformat(timespec="seconds")
    st["day"] = now.strftime("%Y-%m-%d")
    try:
        os.makedirs(os.path.dirname(INSERT_STATE), exist_ok=True)
        with open(INSERT_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass
    return True


def _load_mute() -> dict:
    try:
        with open(MUTE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_mute(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(MUTE_FILE), exist_ok=True)
        with open(MUTE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass



# ── 别的 AI 说话，一律不接（2026-11-11 18:5x 她：「不要一直和ai聊天防止递归」）──
AI_SENDERS_FILE = _MING_WS + r"\live\ai-senders.json"


# ── 表情包不接（2026-11-18 12:0x 她说的：「注意区分图片是表情包还是图片，如果是表情包就不需要回复」）
# 判据两条：① message_str 整个就是一个 [表情名]（QQ 小黄脸在 AstrBot 里长这样：[笑] [摊手] [prpr]）；
#           ② 组件是 Image 但 file 带 mface 或 sub_type=1（商城大表情）。
# 「[图片]」是真的图，照旧能接。
REAL_MEDIA = ("图片", "视频", "语音", "文件", "合并转发", "QQ红包", "动画表情")
STICKER_ONE = re.compile(r"^\[([^\[\]]{1,14})\]$")

# 2026-11-18 15:3x：enhance_mode 会在消息尾巴挂 <system_reminder>…</system_reminder> 和 [MSG_ID:…]，
# 于是「整条就是一个表情」永远匹配不上，今天表情拦截率 0%。判之前先剥掉。
INJECT_TAG_RE = re.compile(
    r"\s*(?:<system[-_]reminder>[\s\S]*?</system[-_]reminder>|\[MSG_ID:[^\]]+\])\s*",
    re.I,
)


def _strip_injected_tags(s: str) -> str:
    return INJECT_TAG_RE.sub(" ", str(s or "")).strip()


def _is_sticker(event) -> bool:
    try:
        txt = _strip_injected_tags(event.message_str or "")
    except Exception:
        txt = ""
    if txt:
        m = STICKER_ONE.fullmatch(txt)
        if m and m.group(1).strip() not in REAL_MEDIA:
            return True
    try:
        for c in (event.get_messages() or []):
            if type(c).__name__ != "Image":
                continue
            f = str(getattr(c, "file", "") or "").lower()
            u = str(getattr(c, "url", "") or "").lower()
            if "mface" in f or "mface" in u:
                return True
            # QQ 的 image 段在不同实现里字段名不一样，四个都探一遍
            for k in ("sub_type", "subType", "_type", "type"):
                v = str(getattr(c, k, "") or "").lower()
                if v in ("1", "show", "sticker", "mface", "emoji"):
                    return True
    except Exception:
        pass
    return False


def _only_image(event) -> bool:
    """一条消息里只有图、没有字（2026-11-18 12:0x 她的原话：「你现在不能区分图片表情包和纯图片」）。
    数据层面分不出来：mface 段被适配器直接丢了，剩下的都是一样的 [图片]。
    所以群里一律不接纯图——群里的图大半是表情包和梗图；她私聊发图照接。"""
    try:
        txt = _strip_injected_tags(event.message_str or "")
    except Exception:
        txt = ""
    if txt not in ("", "[图片]"):
        return False
    seen = False
    try:
        for c in (event.get_messages() or []):
            name = type(c).__name__
            if name == "Image":
                seen = True
            elif name == "Plain":
                t = _strip_injected_tags(getattr(c, "text", "") or "")
                if t and t != "[图片]":
                    return False
    except Exception:
        return False
    return seen


_AT_TAG = re.compile(r"\[At:\d+\]")


def _bare_at(event) -> bool:
    """只是叫了一声，没有要我答的字（2026-11-22 立）。

    起因：700000006 里群友单发一条「@牢溟似了」，@ 一走就是必过，
    模型对着空消息只能硬接，接成了「菲比那个是你养的角色吧」——那会儿他们在聊
    七彩虹的整机，她只是顺手提一句鱼。叫一声不等于有事，判据要落到正文上。
    """
    try:
        txt = _strip_injected_tags(event.message_str or "")
    except Exception:
        return False
    txt = _AT_TAG.sub("", txt)
    return not re.sub(r"[\s\W_]+", "", txt, flags=re.UNICODE)


# 2026-11-18 15:3x：禁言熔断。禁言期进来的每条都别花钱（今天 700000001 空跑上百轮）。
MUTE_NOW_FILE = _MING_WS + r"\live\qq-mute-now.json"


def _is_group_muted_self(gid) -> bool:
    try:
        with open(MUTE_NOW_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return False
    info = data.get(str(gid))
    if not isinstance(info, dict):
        return False
    until = info.get("until")
    if isinstance(until, (int, float)):
        return float(until) > time.time()
    if isinstance(until, str) and until.strip():
        try:
            return datetime.strptime(until.strip(), "%Y-%m-%d %H:%M") > datetime.now()
        except Exception:
            return False
    return False


def _is_ai_sender(sid) -> bool:
    try:
        with open(AI_SENDERS_FILE, "r", encoding="utf-8") as f:
            ids = json.load(f).get("ids", [])
        return str(sid) in {str(x) for x in ids}
    except Exception:
        return False


def _hhmm_to_min(s) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m and int(m.group(2)) < 60 else None


def in_quiet_window(start, end, cur_min: int) -> bool:
    """群宵禁判定：左闭右开，支持跨零点。配置坏了返回 False（不拦）。"""
    # 2026-11-19：临时关一天——live/curfew-off 里写当天日期就不宵禁（第二天自动恢复）
    try:
        _p = _MING_WS + r"\live\curfew-off"
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as _f:
                _d = _f.read().strip()
            if not _d or _d.lower() in ("off", "always") or _d.startswith(datetime.now().strftime("%Y-%m-%d")):
                return False
    except Exception:
        pass
    a, b = _hhmm_to_min(start), _hhmm_to_min(end)
    if a is None or b is None:
        return False  # 旧壳同口径：宵禁配置异常不拦截正常流程
    return (a <= cur_min < b) if a <= b else (cur_min >= a or cur_min < b)


class ReplyPolicy(Star):
    """群聊回复概率 + 每分钟额度 + @ 加成；私聊不受限。

    2026-11-11 立（她的要求）：
      群内 —— 回复率 12%，每分钟最多 8 条，每被 @ 一次当分钟额度 +2
      私聊 —— 必定回复，无每分钟上限

    为什么是插件而不是配置：AstrBot 原生的 platform_settings.rate_limit
    只有一个全局 count（stage.py 里 self.rate_limit_count 是单个数），
    分不出"群里 8 条 / 私聊无限"，也没有回复概率和 @ 加成。
    所以那一项要保持 count=0（关掉），由这里统一管。

    计数口径：按"放行"计数，不按"真的发出去了"计数。
    放行后被模型自己拒答、或被别的插件拦掉，也算用掉一条额度。
    这样简单、可预测；需要精确到出口再改。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.group_rate = float(self.config.get("group_reply_rate", 0.12))
        self.per_minute = int(self.config.get("group_per_minute", 8))
        self.at_bonus = int(self.config.get("at_bonus", 2))
        self.at_always_reply = bool(self.config.get("at_always_reply", True))
        # 2026-11-14 22:1x 她定的：全场皆 AI 时，最多五分钟插一句。跟岚那边同一个数。
        self.ai_scene_gap = float(self.config.get("ai_scene_gap_sec", 300))
        # 2026-11-11 21:4x 群宵禁（旧壳 orchestrator/index.js:967 的 perChat.quietHours）。
        # 语义照旧壳：窗口内该群**完全静默**——@、叫名字、抽签全不回；私聊不受影响。
        # 左闭右开，允许跨零点（00:30–10:30 就是跨零点）。
        self.quiet_enabled = bool(self.config.get("group_quiet_enabled", True))
        self.quiet_from = str(self.config.get("group_quiet_from", "00:30"))
        self.quiet_to = str(self.config.get("group_quiet_to", "10:30"))
        # 2026-11-12 她定的：AI家族群 700000004 永不宵禁（两个号都算）。
        # 群号命中 umo 就跳过宵禁；其余群照 00:30–10:30。
        self.quiet_exempt = {
            str(x).strip() for x in (self.config.get("group_quiet_exempt") or []) if str(x).strip()
        }
        logger.info(
            "[policy] quiet enabled=%s %s-%s exempt=%s",
            self.quiet_enabled,
            self.quiet_from,
            self.quiet_to,
            sorted(self.quiet_exempt),
        )
        # umo -> 最近放行的时间戳（滑动 60 秒）
        self.windows: dict[str, deque[float]] = defaultdict(deque)
        # 2026-11-11 17:3x：umo -> 最近 5 分钟说过话的人。静默开始时存一份，
        # 之后出现「不在名单里的人」就当话题换了（她说：话题结束后常直接接另一个话题）。
        self.recent: dict[str, deque] = defaultdict(deque)
        # umo -> 最近几条 (ts, sender, text)，静默判断话题要用
        self.recent_msgs: dict[str, deque] = defaultdict(deque)
        # 2026-11-14 22:1x 她：「你也加上哦，不止岚」。umo -> 最后一次真发出去的时间。
        self.last_sent: dict[str, float] = {}
        # QQ 号 → 昵称（只为了第一轮能看出「这句是谁说的」；2026-11-13）
        self.names: dict[str, dict] = defaultdict(dict)

    def _in_quiet(self, umo: str = "") -> bool:
        """现在是不是群宵禁时段。umo 命中豁免名单就不算。"""
        if not self.quiet_enabled:
            return False
        if umo and any(g in umo for g in self.quiet_exempt):
            return False
        now = datetime.now()
        return in_quiet_window(
            self.quiet_from, self.quiet_to, now.hour * 60 + now.minute
        )

    def _has_at_other(self, event: AstrMessageEvent) -> bool:
        """消息里有没有 @ 别人（且没有 @ 本实例）。burst 合并后还要看文本里的 At。"""
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = ""
        found = False
        try:
            for comp in event.message_obj.message:
                qq = getattr(comp, "qq", None)
                if qq is None:
                    continue
                found = True
                if self_id and str(qq) == self_id:
                    return False
        except Exception:
            pass
        try:
            for qq in re.findall(r"\[At:(\d+)\]", getattr(event, "message_str", "") or ""):
                found = True
                if self_id and qq == self_id:
                    return False
        except Exception:
            pass
        return found

    def _self_name(self, event: AstrMessageEvent) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""
        if umo.startswith(PLATFORM_ID + ":"):
            return SELF_NAME
        if umo.startswith("linglan:"):
            return "岚"
        return ""

    def _is_at_me(self, event: AstrMessageEvent) -> bool:
        """消息里有没有 @ 我这个号。"""
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = ""
        try:
            for comp in event.message_obj.message:
                # aiocqhttp 的 At 组件：comp.qq 是目标 QQ
                qq = getattr(comp, "qq", None)
                if qq is None:
                    continue
                if str(qq) == self_id:
                    return True
        except Exception:
            pass
        # 2026-11-11 补：burst 合并多条消息后会把 message_obj.message 整个换成
        # [Plain(合并文本)] + 图片，At 组件就没了 —— 群里 @ 我等于没 @，只能落到
        # 12% 的随机里（实测 15:54~15:57 一条都没回）。文本里的 [At:<qq>] 还在，靠它兜。
        try:
            if self_id and f"[At:{self_id}]" in (getattr(event, "message_str", "") or ""):
                return True
        except Exception:
            pass
        return False


    def mark_sent(self, umo: str) -> None:
        """pacer 在第一段真发出去之后调这个——额度按实发算。"""
        try:
            now = time.time()
            self.windows[umo].append(now)
            self.last_sent[umo] = now
        except Exception:
            pass

    async def _topic_changed(self, umo: str, st: dict, new_msg: str) -> bool:
        try:
            prov = self.context.get_using_provider(umo)
            if prov is None:
                return False
            old = "\n".join([t for t in (st.get("topic") or []) if t])[-600:]
            prompt = f"之前在聊：\n{old}\n\n现在这条：\n{new_msg[:300]}"
            import asyncio
            resp = await asyncio.wait_for(
                prov.text_chat(prompt=prompt, system_prompt=TOPIC_PROMPT, contexts=[], session_id=None),
                timeout=20,
            )
            v = (getattr(resp, "completion_text", "") or "").strip().lower()
            return v.startswith("yes") or v.startswith("是")
        except Exception:
            return False

    async def _preflight_ok(self, event: AstrMessageEvent, umo: str, text: str) -> bool | None:
        """第一轮思考：这条接不接。True/False；判断不了返回 None（调用方退回抽签）。

        这一轮的意义是**它的输出不进任何正文**：判词、犹豫、半句话都烂在这里。
        只有「接」这个结果被留下，第二轮（真正的回复生成）从干净的状态出发。
        """
        cfg = _preflight_cfg()
        if not cfg.get("enabled"):
            return None
        text = (text or "").strip()
        if not text:
            return None
        # 2026-11-15 14:0x 她定的：不加时间闸，频率交给概率抽签（见调用方）。
        t0 = time.time()
        try:
            # 2026-11-23 02:5x：判官换 flash（粗活），失败才退回默认 provider
            prov = None
            try:
                prov = self.context.get_provider_by_id(PREFLIGHT_PROVIDER_ID)
            except Exception as _e:  # noqa: BLE001
                logger.debug("[policy] 取判官 provider 失败：%s", _e)
            if prov is None:
                prov = self.context.get_using_provider(umo)
                logger.info("[policy] 判官退回默认 provider")
            else:
                logger.info("[policy] 判官用 %s", PREFLIGHT_PROVIDER_ID)
            if prov is None:
                return None
            recent = list(self.recent_msgs.get(umo, ()))[-9:-1]
            recent = [
                f"{_n}: {t}" for _ts, _sid, t in recent if t
                if (_n := (self.names.get(umo, {}).get(_sid) or _sid))
            ]
            # 2026-11-18 02:1x 溟加：先把「现在在哪儿、谁在说话」摆给判官。
            # 原来只给最近 5 条裸句，判官只好按群聊补常识，私聊就补成「他们俩之间的事」。
            try:
                _private = bool(event.is_private_chat())
            except Exception:
                _private = False
            try:
                _sname = str(event.get_sender_name() or "")
            except Exception:
                _sname = ""
            try:
                _sid = str(event.get_sender_id() or "")
            except Exception:
                _sid = ""
            prompt = ""
            if _private:
                prompt += (
                    "现在在哪儿：私聊。这个窗口只有两个人——你（溟）和她（她）；"
                    "她说的「你」都是指你。\n"
                )
            else:
                try:
                    _gid = str(event.get_group_id() or "")
                except Exception:
                    _gid = ""
                prompt += (
                    f"现在在哪儿：群聊（群 {_gid}）。里面很多人，不止你和她；"
                    "你自己在群里的名字是「溟」。\n"
                )
            prompt += f"说这句话的人：{_sname}（{_sid}）\n\n"
            if recent:
                prompt += (
                    "最近这几条（从旧到新，最后一条就在这句之前）：\n"
                    + "\n".join(f"- {x[:80]}" for x in recent)
                    + "\n\n"
                )
            # 2026-11-18 15:3x：带上发言人，剥掉宿主注入尾巴——原来判官连「谁说的」都没有，
            # 于是大量「拿不准」。「第」字头由 _strip_injected_tags 处理。
            _clean_cur = _strip_injected_tags(text)
            _who = _sname or _sid or "群友"
            prompt += f"这条新消息：\n{_who}：{_clean_cur[:400]}"
            resp = await asyncio.wait_for(
                prov.text_chat(
                    prompt=prompt,
                    system_prompt=PREFLIGHT_PROMPT,
                    contexts=[],
                    session_id=None,
                ),
                timeout=float(cfg.get("timeout", 20)),
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning("[policy] 第一轮判断失败，退回抽签：%s", e)
            return None
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        head = (lines[0] if lines else "").replace(" ", "")[:12]
        rest = lines[1:]
        intent = ""
        for ln in rest:
            if ln.startswith("要点"):
                _parts = ln.split("：", 1)
                if len(_parts) == 1:
                    _parts = ln.split(":", 1)
                intent = (_parts[1] if len(_parts) > 1 else "").strip()[:60]
                break
        # 心声：第二行起、去掉「要点」那行。只落账，不发给第二轮。
        mind = " ".join(ln for ln in rest if not ln.startswith("要点"))[:200]
        if "拿不准" in head or "不确定" in head:
            ok = None
        elif "不接" in head or "不回" in head:
            ok = False
        elif "接" in head:
            ok = True
        else:
            ok = None
        # 频率已经由 _judge_gate 管住（进函数时过闸），这里不再算额度。
        # 把第一轮的结论交给第二轮：它不必再判一次「要不要回」，
        # 那句「在吗」就是重新判出来的（2026-11-13）。
        try:
            if ok is not None:
                event.set_extra("preflight", {
                    "verdict": "接" if ok else "不接",
                    "intent": intent,
                })
        except Exception:
            pass
        try:
            os.makedirs(os.path.dirname(PREFLIGHT_LOG), exist_ok=True)
            with open(PREFLIGHT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "umo": umo,
                    "verdict": "接" if ok else ("不接" if ok is False else f"?{head}"),
                    "ms": int((time.time() - t0) * 1000),
                    "mind": mind[:200],
                    "text": text[:80],
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return ok

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def watch_mute(self, event: AstrMessageEvent):
        """听她的「安静」——不用答题，只记一笔。"""
        try:
            sender = str(event.get_sender_id())
            txt = (event.message_str or "").strip()
            if sender != HER_ID or not txt:
                return
            if not any(w in txt for w in MUTE_WORDS):
                return
            gid = event.get_group_id()
            umo = event.unified_msg_origin
            speakers = sorted({sid for ts, sid in self.recent.get(umo, ()) if time.time() - ts < 300})
            _save_mute({
                "group": str(gid) if gid else "",
                "since": time.time(),
                "last": time.time(),
                "speakers": speakers,
                "topic": [t for _ts, _sid, t in self.recent_msgs.get(umo, ())][-5:],
                "lastCheck": time.time(),
                "by": "她",
            })
            logger.info("[policy] 她说「%s」→ 这个话题我不吭声了", txt[:20])
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def apply_policy(self, event: AstrMessageEvent):
        # 私聊：一律放行
        try:
            gid = event.get_group_id()
        except Exception:
            gid = None
        # 2026-11-18 15:3x 按修复单：本群在禁言期就别进流程（12:23 起空跑上百轮）。
        if gid and _is_group_muted_self(gid):
            logger.info("[policy] 群 %s 正被禁言，入口静默", gid)
            event.stop_event()
            return
        # Q群管家（2854196310）：欢迎/公告/改名片都是系统消息，不接、不记。
        try:
            if str(event.get_sender_id() or "") == "2854196310":
                event.stop_event()
                return
        except Exception:
            pass
        # 2026-11-18：纯表情/表情包不接。群里的纯图也不接（她的原话：「你现在不能区分图片表情包和纯图片」）。
        try:
            if _is_sticker(event):
                logger.info("[policy] 表情/表情包，不接：%s", (event.message_str or "")[:12])
                event.stop_event()
                return
        except Exception:
            pass
        try:
            if gid and _only_image(event):
                logger.info("[policy] 群里只发了一张图，不接")
                event.stop_event()
                return
        except Exception:
            pass

        # 2026-11-11：不管群还是私聊，对面是另一个 AI 就不接——防递归。
        try:
            if _is_ai_sender(event.get_sender_id()):
                logger.info("[policy] 对方是 AI（%s），不接", event.get_sender_id())
                event.stop_event()
                return
        except Exception:
            pass

        if not gid:
            # 2026-11-18 02:1x 溟改：私聊不再跑第一轮判官（默认关；配置里写 "private": true 可开）。
            # 判官的提示词是群聊版的——「这条群消息」「他们在聊他们自己的事、我只是路过→不接」
            # 「别人说『你』，先看上一句是谁在跟谁说；不能确定是在说我，就当不是→不接」。
            # 拿去判私聊，必然判成「我路过」。实证：
            #   02:04:22 判「不接」——「明显是顺着她的话在逗她，不是冲我来的。我路过，没被点名」；
            #   02:05:00 判「不接」——「更像是他们俩之间的事，我只是路过」。私聊里没有「他们俩」。
            # 而且每条白花 3–7 秒。群聊那套「插不插话」的判断不动。
            if _preflight_cfg().get("private", False):
                try:
                    ok = await self._preflight_ok(
                        event, event.unified_msg_origin, (event.message_str or "").strip()
                    )
                    if ok is False:
                        # 私聊不拦。但也不能把「不接」写进第二轮的提示里——
                        # 那等于叫它别出声，比我多说一句更糟。
                        event.set_extra("preflight", {"verdict": "接", "intent": ""})
                except Exception as e:
                    logger.warning("[policy] 私聊第一轮失败（照发）：%s", e)
            return

        # 2026-11-11 21:4x 群宵禁：窗口内该群完全静默（@ 也不回）。私聊不受影响。
        if self._in_quiet(event.unified_msg_origin):
            logger.info(
                "[policy] 宵禁 %s–%s，这条不回：%s",
                self.quiet_from, self.quiet_to, (event.message_str or "")[:24],
            )
            event.stop_event()
            return

        umo = event.unified_msg_origin
        is_at = self._is_at_me(event)
        # 2026-11-12：@ 的是别人、没有 @ 本实例、也没叫本实例名字——即使带问号也不接。
        # 实证：20:48:39 她 @群友「宝~（？」被 溟 认成在叫自己，回了「宝什么宝 叫溟」。
        has_foreign_at = self._has_at_other(event)
        self_name = self._self_name(event)
        text_now = (getattr(event, "message_str", "") or "")
        has_self_name = bool(self_name) and (
            self_name in text_now or text_now.strip().startswith(self_name)
        )
        if is_at:
            # 2026-11-18 22:2x 加：被 @ 的那条一路是静默放行的，日志里看不到任何痕迹，
            # 排查「到底有没有走到模型」只能靠猜。留一行。
            logger.info("[policy] @ 本实例，往下走：%s", text_now[:40])
        if ("GroupMessage" in umo or "Group" in umo) and has_foreign_at and not is_at and not has_self_name:
            logger.info("[policy] @ 的是别人，不是本实例，不接：%s", text_now[:60])
            event.stop_event()
            return
        now = time.time()
        try:
            sid = str(event.get_sender_id())
            dq0 = self.recent[umo]
            dq0.append((now, sid))
            while dq0 and now - dq0[0][0] > 300:
                dq0.popleft()
            dq1 = self.recent_msgs[umo]
            dq1.append((now, sid, (event.message_str or "").strip()[:120]))
            try:
                self.names[umo][sid] = str(event.get_sender_name() or sid)[:16]
            except Exception:
                self.names[umo][sid] = sid
            while dq1 and len(dq1) > 8:
                dq1.popleft()
        except Exception:
            sid = ""

        # 察言观色：她自己说「安静」→ 这个话题里我不吭声（@ 也不吭），
        # 直到群消息静默 15 分钟（话题散了）才自动解除。
        st = _load_mute()
        if st.get("group") and str(st["group"]) == str(gid):
            if now - float(st.get("last", 0)) > TOPIC_IDLE_SEC:
                _save_mute({})
                logger.info("[policy] 静默解除：话题停了 15 分钟")
            elif is_at:
                # 她说「@ 可以回复」——被点名就应一声，但静默状态不动
                st["last"] = now
                _save_mute(st)
                logger.info("[policy] 静默中，但被 @ 了，这条照回")
            elif st.get("topic") and now - float(st.get("lastCheck", 0)) > TOPIC_CHECK_SEC and (event.message_str or "").strip():
                # 2026-11-11 17:3x 她说「可以调用一次模型判断是否换话题」。
                # 静默期每 5 分钟才判一次，不是每条都判（那条太贵）。
                st["lastCheck"] = now
                _save_mute(st)
                if await self._topic_changed(umo, st, (event.message_str or "").strip()):
                    _save_mute({})
                    logger.info("[policy] 静默解除：模型判出换话题了")
                else:
                    event.stop_event()
                    return
            elif sid and st.get("speakers") and sid not in st["speakers"]:
                # 换了个说话的人 = 大概率换话题了，解除
                _save_mute({})
                logger.info("[policy] 静默解除：%s 说话了（不在静默时的名单里）", sid)
            else:
                st["last"] = now
                _save_mute(st)
                logger.info("[policy] 静默中，这条不回（她说安静，话题还没散）")
                event.stop_event()
                return

        # ── 2026-11-14 22:1x 全场皆 AI：近 5 分钟说过话的人全在 AI 名单里 → 不接 ──
        # 与岚侧同一套。名单判据：昵称形如「（xxx的ai）」才是 AI（她 2026-11-14 定的）。
        # 距离上句满 ai_scene_gap 秒就许插一句（她说「偶尔插一句可以哦，五分钟一句什么的」）。
        try:
            if not is_at and not has_self_name:
                recent_sids = {s for ts, s in self.recent.get(umo, ()) if now - ts < 300}
                if recent_sids and all(_is_ai_sender(s) for s in recent_sids):
                    since = now - self.last_sent.get(umo, now)  # 重启后从进程起算
                    if since < self.ai_scene_gap:
                        logger.info(
                            "[policy] 近 5 分钟只有 AI 在说话（%d 人），距上句 %.0fs < %ds，不接：%s",
                            len(recent_sids), since, self.ai_scene_gap,
                            (event.message_str or "")[:24],
                        )
                        event.stop_event()
                        return
                    logger.info(
                        "[policy] 全场皆 AI，但距上句 %.0fs ≥ %ds，许插一句：%s",
                        since, self.ai_scene_gap, (event.message_str or "")[:24],
                    )
        except Exception:
            pass

        dq = self.windows[umo]
        while dq and now - dq[0] > 60.0:
            dq.popleft()

        # 额度：基础 + 被 @ 的加成
        quota = self.per_minute + (self.at_bonus if is_at else 0)
        if self.per_minute > 0 and len(dq) >= quota:
            logger.info(
                "[reply-policy] %s 每分钟额度用完（%d/%d，@=%s），这条不回",
                umo, len(dq), quota, is_at,
            )
            _mark_not_handled(event, "quota")
            event.stop_event()
            return

        # 概率：被 @ 时按配置无视概率
        # 2026-11-22：空 @ 不再必过。@ 一下等于「叫了一声」，不等于「有事问我」。
        bare_at = bool(is_at and _bare_at(event))
        if bare_at:
            logger.info("[policy] 只 @ 了一声、正文是空的，不直接放行：%s", (event.message_str or "")[:24])
        if bare_at or not (is_at and self.at_always_reply):
            # 2026-11-11 17:4x：@ / 叫名字 / 问句 → 必过，不抽签。
            # 剩下的闲聊才按 group_rate 抽——抽签只用来控成本，不用来决定"该不该接"。
            txt = (event.message_str or "")
            # 2026-11-22：空 @ 交给第一轮判官凭上下文判（这种很少，不抽签——赌输了就是硬接）
            # 2026-11-23 02:5x 她定的：@ 就跳过判官（空 @ 也算），别再白花一次调用
            must = is_at or ("溟" in txt)
            if not must:
                # 2026-11-15 14:0x 她定的：别加时间闸，回到概率——
                # 先抽签，抽中了才进第一轮判断。把 group_reply_rate 调高就是「说话放宽」。
                if not bare_at and self.group_rate < 1.0 and random.random() >= self.group_rate:
                    logger.info(
                        "[policy] 抽签未中（%.0f%%）：%s",
                        self.group_rate * 100, txt[:24],
                    )
                    _mark_not_handled(event, "lottery")
                    event.stop_event()
                    return
                # 两轮思考：第一轮判接不接，判不接就到此为止——第二轮不生成。
                verdict = await self._preflight_ok(event, umo, txt)
                if verdict is False:
                    logger.info("[policy] 第一轮判「不接」，不进第二轮：%s", txt[:24])
                    _mark_not_handled(event, "preflight-no")
                    event.stop_event()
                    return
                if verdict is None:
                    # 2026-11-18 02:1x 溟改：拿不准改成放行（判「接」）。
                    # 原文是「判不出对象 = 不是冲我来的，这个方向上不接永远比接对」——
                    # 可判官的输入本来就是几条裸句，拿不准是它的常态；拿常态去拦，
                    # 等于把「我读不到上下文」记成「这不是冲我来的」。
                    # 实测代价：群里像在找我的闲话整片漏；私聊 01:53–01:58 连着六条全拿不准。
                    logger.info("[policy] 第一轮拿不准，按「接」放行：%s", txt[:24])
            # 2026-11-11 18:1x **根因**：process_stage 只看 event.is_at_or_wake_command
            # 决定要不要调模型；插件 handler 通过只设 is_wake，不会设这个字段。
            # 所以群里没被 @ 的消息一路活到 policy，然后在这里被"不需要唤醒"挡掉——
            # 看起来像"我在群里不说话"，其实是根本没走到模型。
            # 放行的就明确告诉管道：这条要问模型。
            event.is_at_or_wake_command = True
            # 2026-11-11 20:3x 她定的：不是所有回复都带引用。
            # @ / 叫名字 = 真的在回那个人 → 带；问句、抽签接话 = 插进话题 → 不带。
            # pacer 读这个标记决定补不补 Reply 组件。
            event.set_extra("quote", bool((is_at and not bare_at) or ("溟" in txt)))
            if must:
                logger.info("[policy] 放行（%s）：%s", "at" if is_at else ("名字" if "溟" in txt else "问句"), txt[:24])

        # 2026-11-11 17:5x 她指出的：每分钟 8 条应该按**真发出去**算，
        # 不是按"放行"算。放行之后被 gate/模型自己吞掉的那些不该占额度。
        # 所以这里不再计数，改由 pacer 在第一段真发出去之后调 mark_sent()。
