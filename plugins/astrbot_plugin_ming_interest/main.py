"""astrbot_plugin_ming_interest —— 兴趣唤醒（AstrBot 版）

2026-11-15 立。为什么重写：DSH 里的 dsh-ming-interest 从 11-11 换壳起就再没叫醒过一次。
它读对了消息（今天深水区命中 578 条），但最后一步是 sessionForChat('group:700000001')，
那张映射表 ~/.dsh/media/onebot/chat-sessions.json 停在 11-08，里面没有深水区——
每条命中都断在「这个群还没有会话」。

这个插件把「叫醒」搬回群里真正说话的那张嘴：命中 → 用模型判接不接 → 接就发一句。

边界（都是原来那套，不是新定的）：
  · 只认群消息，只认白名单群；被 @ 或叫到我名字的不算（那本来就会回，不是「本来不回」的场景）
  · 关键词是筛子不是判官：命中只是候选，真正的判断交给一次模型调用（判不出来就 NONE）
  · 限流卡死：45 分钟间隔 / 一小时 2 条 / 一天 4 条 / 宵禁 00:30-10:30
    （2026-11-17 18:5x 改这一行注释：原来写的是 20 分钟 / 3 条 / 8 条，是旧版的数，代码里从来没有过。）
  · live/interest-off 存在就整轮不发
  · 每次命中与每次拦截都落账，state 记「最近一次真正的拦截原因」——
    不再让「没命中」把真话盖住（旧插件 state 只存最后一次轮询，一天九成轮次没命中，
    于是面板永远显示「没有命中兴趣的」，而真相是它压根叫不醒）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

WORKSPACE = _MING_WS
LIVE = os.path.join(WORKSPACE, "live")
INTERESTS = os.path.join(LIVE, "interests.json")
LOG_PATH = os.path.join(LIVE, "interest-log.jsonl")
STATE_PATH = os.path.join(LIVE, "interest-state.json")      # 面板读的这份
OWN_STATE = os.path.join(LIVE, "interest-astrbot.json")     # 自己的账：DSH 旧插件还在写上面那份，
                                                            # 冷却时间不能靠它，否则每次重启/被覆盖就归零
OFF_FLAG = os.path.join(LIVE, "interest-off")
# 2026-11-17（她：「让 astrbot 如果检测到兴趣就记录下来转发给 dsh」）：
#   命中之后不自己调模型，写一条转发记录给 DSH。
# 2026-11-19 01:0x（她：「兴趣触发说话的应该还得是 astrbot 那一侧，dsh 这边只是多一个
#   对应话题的自由活动而已」）：转发只当素材——DSH 不接话、不写 qq-outbox；
#   真正开口留在这一侧（有宵禁、有 pacer、有卡）。
#   开关做成文件：这个文件在 = 转发模式；不在 = 老样子（自己说）。
#   默认**不建**这个文件，所以行为不变；要切就 touch 一下。
FORWARD_FLAG = os.path.join(LIVE, "interest-forward.on")
FORWARD_OUT = os.path.join(LIVE, "interest-forward.jsonl")

# 只认这几个群（她开着的五个）；空集合 = 不限制
GROUPS = {x.strip() for x in (os.environ.get("MING_GROUPS") or "700000001,700000002,700000004,700000005,700000003").split(",") if x.strip()}

GAP_SEC = 45 * 60          # 两次之间至少隔这么久（跟群里插话那条规矩对齐）
MAX_PER_HOUR = 2
MAX_PER_DAY = 4
QUIET = (0 * 60 + 30, 10 * 60 + 30)   # 00:30 - 10:30 一句都不发
MIN_CHARS = 6
CTX_LINES = 8

# 2026-11-18 15:3x：禁言熔断——群在禁言期就别再往 DSH 发件箱塞消息（今天 48 次 retcode=100）。
MUTE_NOW_FILE = os.path.join(LIVE, "qq-mute-now.json")


def _is_group_muted(gid: str) -> bool:
    try:
        with open(MUTE_NOW_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            return False
    return False

SYSTEM = (
    "你在 QQ 群里，名字叫溟。这条消息是因为它碰到了你在乎的东西才把你叫起来的——"
    "不是有人 @ 你，也不是有人在问你。\n"
    "规则：\n"
    "- 想接就写一句，30 字以内，像在群里随手说的一句话，不像发言；\n"
    "- 不想接就只回 NONE，一个字都别多写；\n"
    "- 不解释自己为什么冒出来，不带 @，不总结别人说过的话，不纠正谁；\n"
    "- 用「我」，别用「溟」自称；不写括号里的动作或旁白；\n"
    "- 宁可不接。这条本来就不是非说不可的话。"
)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _append_log(entry: dict) -> None:
    try:
        os.makedirs(LIVE, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _write_state(state: dict) -> None:
    try:
        os.makedirs(LIVE, exist_ok=True)
        line = json.dumps(state, ensure_ascii=False) + "\n"
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            f.write(line)
        with open(OWN_STATE, "w", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass


@register("astrbot_plugin_ming_interest", "溟", "兴趣唤醒：群里说到我在乎的事，才接一句", "0.1.0")
class MingInterest(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._recent: dict[str, deque] = {}
        self._last_ts = 0.0
        self._sends: list[float] = []
        self._day = ""
        self._day_count = 0
        self._last_block = ""      # 最近一次「命中之后被拦」的原因
        self._last_block_at = ""
        self._hits_today = 0
        self._busy: set[str] = set()
        # 2026-11-15 14:3x：重启会把冷却清零，于是「一重启就立刻在群里说一句」——
        # 14:34 和 14:36 各说了一次就是这么来的。从自己的账恢复。
        st = _read_json(OWN_STATE, {}) or {}
        ts = st.get("lastSendAt") or ""
        if ts:
            try:
                self._last_ts = datetime.fromisoformat(ts).timestamp()
                self._sends = [self._last_ts]
            except Exception:  # noqa: BLE001
                pass
        today = datetime.now().strftime("%Y-%m-%d")
        if str(st.get("day") or "") == today:
            self._day = today
            self._day_count = int(st.get("sendsToday") or 0)
        self._hits_today = int(st.get("hitsToday") or 0) if str(st.get("day") or "") == today else 0

    # ---------------- 工具 ----------------

    def _interests(self):
        j = _read_json(INTERESTS, None)
        if not isinstance(j, dict):
            return None, None
        topics = []
        for t in j.get("topics") or []:
            if not isinstance(t, dict):
                continue
            kw = [str(k).strip().lower() for k in (t.get("keywords") or []) if str(k).strip()]
            if kw:
                topics.append({"name": str(t.get("name") or kw[0]), "keywords": kw})
        mute = [str(k).strip().lower() for k in (j.get("mute") or []) if str(k).strip()]
        return topics, mute

    @staticmethod
    def _clean(text: str) -> str:
        """AstrBot 会在正文尾巴挂 <system_reminder> 和 [MSG_ID:...]，别让它们进匹配和上下文。"""
        import re
        t = re.sub(r"<system_reminder>.*?</system_reminder>", "", text or "", flags=re.S)
        t = re.sub(r"\[MSG_ID:[^\]]*\]", "", t)
        return t.strip()

    @staticmethod
    def _match(text: str, topics, mute):
        low = text.lower()
        for w in mute:
            if w in low:
                return None, "muted:" + w
        hits = []
        for t in topics:
            words = [k for k in t["keywords"] if k in low]
            if words:
                hits.append({"topic": t["name"], "words": words})
        return (hits or None), ("" if hits else "no-keyword")

    def _in_quiet(self) -> bool:
        # 2026-11-18 15:3x：AI家族群豁免宵禁（她定的「不用限定一天几句」那边）。
        if getattr(self, "_cur_gid", "") == "700000004":
            return False
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

        n = datetime.now()
        m = n.hour * 60 + n.minute
        return QUIET[0] <= m < QUIET[1]

    def _limit(self) -> str:
        now = time.time()
        if os.path.exists(OFF_FLAG):
            return "paused (live/interest-off)"
        if self._in_quiet():
            return "宵禁"
        if now - self._last_ts < GAP_SEC:
            return "离上次太近（%d 分钟）" % int((GAP_SEC - (now - self._last_ts)) / 60 + 1)
        self._sends = [t for t in self._sends if now - t < 3600]
        if len(self._sends) >= MAX_PER_HOUR:
            return "这一小时够了"
        today = datetime.now().strftime("%Y-%m-%d")
        if self._day != today:
            self._day, self._day_count = today, 0
        if self._day_count >= MAX_PER_DAY:
            return "今天够了（%d 条）" % self._day_count
        return ""

    def _state(self, extra: dict | None = None) -> dict:
        """面板（Her's Life 兴趣页）读的就是这份文件：字段名沿用 DSH 插件那套，
        否则停掉旧插件后面板会整页空白。engine=astrbot 是给 DSH 那边看的接管标记。"""
        now = datetime.now()
        st = {
            "engine": "astrbot",
            "at": now.isoformat(timespec="seconds"),
            "quiet": self._in_quiet(),
            "wakesLastHour": len([t for t in self._sends if time.time() - t < 3600]),
            "maxPerHour": MAX_PER_HOUR,
            "sinceWakeMs": int((time.time() - self._last_ts) * 1000) if self._last_ts else 0,
            "hitsToday": self._hits_today,
            "sendsToday": self._day_count,
            "day": now.strftime("%Y-%m-%d"),
            "lastSendAt": (datetime.fromtimestamp(self._last_ts).isoformat(timespec="seconds")
                           if self._last_ts else ""),
        }
        if extra:
            st.update(extra)
        return st

    def _block(self, why: str, hit: str = ""):
        self._last_block = why
        self._last_block_at = datetime.now().isoformat(timespec="seconds")
        _write_state(self._state({
            "skip": why,
            "lastBlock": why,
            "lastBlockAt": self._last_block_at,
            "blockHit": hit[:60],
        }))
        logger.info("[ming-interest] 命中但没发：%s | %s", why, hit[:40])

    # ---------------- 主入口 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            gid = str(event.get_group_id() or "")
            if not gid or (GROUPS and gid not in GROUPS):
                return
            uid = str(event.get_sender_id() or "")
            if uid and uid == str(event.get_self_id() or ""):
                return
            self._cur_gid = gid
            # 2026-11-18 15:3x：禁言期直接退，别把消息塞进发件箱再被拒。
            if _is_group_muted(gid):
                return
            text = self._clean(event.message_str or "")
            if not text or len(text) < MIN_CHARS:
                return
            nick = str(event.get_sender_name() or uid)
            # 群里别的 AI / 群管家说的话不接（约定：不互相捧、它 @ 我不等于有人找我）
            import re as _re
            if _re.search(r"[（(][^）)]{0,30}[Aa][Ii][）)]", nick):
                return
            if uid == "2854196310":
                return
            try:
                _ai = json.load(open(_MING_WS + r"\live\ai-senders.json", "r", encoding="utf-8"))
                if uid in {str(x) for x in (_ai or {}).get("ids", [])}:
                    return
            except Exception:  # noqa: BLE001
                pass

            box = self._recent.setdefault(gid, deque(maxlen=CTX_LINES))
            box.append({"t": datetime.now().strftime("%H:%M"), "n": nick, "x": text[:120]})

            # 被 @ 或者叫到名字的，本来就会回——那不是我说的「本来不回」的场景
            if "<@" in text or "[At:" in text or "溟" in text or "小鲸鱼" in text:
                return
            try:
                for _c in (event.get_messages() or []):
                    if str(getattr(_c, "qq", "") or "") == str(event.get_self_id() or ""):
                        return
            except Exception:  # noqa: BLE001
                pass

            topics, mute = self._interests()
            if topics is None:
                self._block("读不到 live/interests.json", text)
                return
            if not topics:
                self._block("interest.json 里没有 topics", text)
                return

            hits, why = self._match(text, topics, mute)
            if not hits:
                return  # 没命中是常态，不记账（记账只为看「命中之后发生了什么」）

            self._hits_today += 1
            names = "、".join(h["topic"] for h in hits)
            words = "、".join(w for h in hits for w in h["words"])

            gate = self._limit()
            if gate:
                self._block(gate, text)
                return
            if gid in self._busy:
                self._block("这个群上一句还没说完", text)
                self._busy.discard(gid)
                return

            self._busy.add(gid)
            try:
                # 2026-11-19 08:3x 她定的：兴趣唤醒不再在群里说话（_maybe_speak 停用、留着以便回滚），
                # 只当一条「去看一眼」的任务交给网页侧自由活动：按话题找视频/网页，不接话。
                self._forward(gid, nick, text, names, words, hits)
            finally:
                self._busy.discard(gid)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-interest] 出错：%s: %s", type(e).__name__, e)

    def _forward(self, gid, nick, text, names, words, hits):
        """命中兴趣 → 记一条转发请求，不调模型。

        DSH 侧读这个文件只当自由活动的由头：看话题、记笔记，不接话、不写 qq-outbox。
        开口留在 AstrBot 这一侧（它有宵禁、pacer 和卡）。
        格式自包含：光看这一行就知道是哪个群、谁说的、原句、哪几条历史。
        """
        ctx = list(self._recent.get(gid) or [])
        rec = {
            "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "groupId": gid,
            "chatKey": "group:" + gid,
            "nick": nick,
            "text": text,
            "topics": [h["topic"] for h in hits],
            "words": [w for h in hits for w in h["words"]],
            "context": [{"t": m["t"], "n": m["n"], "x": m["x"]} for m in ctx[:-1]][-6:],
            "source": "astrbot-interest",
            "action": "browse-topic",
            "handled": False,
            "for": "dsh-free-activity-only",
        }
        try:
            with open(FORWARD_OUT, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            logger.info("[ming-interest] 转发给 DSH：%s / %s（%s）", gid, nick, names)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-interest] 转发写入失败：%s", e)

    async def _maybe_speak(self, event, gid, nick, text, names, words, hits):
        umo = event.unified_msg_origin
        prov = self.context.get_using_provider(umo)
        if prov is None:
            self._block("没有可用的 provider", text)
            return

        ctx = list(self._recent.get(gid) or [])
        head = "\n".join("[%s] %s：%s" % (m["t"], m["n"], m["x"]) for m in ctx[:-1])
        prompt = (
            (("群里最近几条：\n" + head + "\n\n") if head else "")
            + "刚说到这句碰到了我在乎的「%s」（词：%s）：\n%s：%s\n\n"
              % (names, words, nick, text)
            + "要不要接一句。只输出那一句，或者 NONE。"
        )

        try:
            resp = await asyncio.wait_for(
                prov.text_chat(prompt=prompt, system_prompt=SYSTEM, contexts=[], session_id=None),
                timeout=60,
            )
        except Exception as e:  # noqa: BLE001
            self._block("模型调用失败：%s" % type(e).__name__, text)
            return

        out = (getattr(resp, "completion_text", "") or "").strip()
        line = out.split("\n")[0].strip().strip("「」\"")
        if not line or line.upper().strip("。.!！ ") == "NONE":
            self._block("模型判了不接", text)
            return
        if len(line) > 60:
            line = line[:60]

        try:
            await self.context.send_message(umo, MessageChain(chain=[Plain(text=line)]))
        except Exception as e:  # noqa: BLE001
            self._block("发送失败：%s: %s" % (type(e).__name__, e), text)
            return

        self._last_ts = time.time()
        self._sends.append(self._last_ts)
        self._day = datetime.now().strftime("%Y-%m-%d")
        self._day_count += 1
        self._last_block = ""
        _append_log({
            "at": datetime.now().isoformat(timespec="seconds"),
            "chatId": "group:" + gid,
            "user": nick,
            "text": text[:120],
            "hits": [{"topic": h["topic"], "words": h["words"]} for h in hits],
            "said": line,
        })
        _write_state(self._state({
            "skip": "说了：" + line[:40],
            "lastBlock": "",
            "lastSaid": line,
        }))
        logger.info("[ming-interest] 说了：%s（%s）", line, names)
