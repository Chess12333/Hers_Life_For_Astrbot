"""溟·学群话（astrbot_plugin_ming_learn）

2026-11-15 12:3x 立。来源是别人写的 `astrbot_plugin_self_learning`（NickMo/EterUltimate，
4.9.5，353 个 py 文件 10 万行），只抄了它的**一套算法**，没抄它任何一行代码：

  A/B 对话对 → 权重 → 二次衰减 → 上限淘汰

它的原话是「学习回应者的表达风格」：相邻两条**不同人**的真人消息算一对，
后一条是前一条的「接法」，同样的配对再出现一次就 +1 权重，
放在那儿不用的按 `0.01 * (天数/15)^2` 往下掉，掉到底就删。

抄这一套是因为它是这个插件里唯一**不花钱也不改人**的部分：
纯统计，不调 LLM，不写人格库。

**跟它的三处不同，都是有意的：**

1. **不碰人设。** 它的落点是 `persona_updater`（`auto_apply_persona_updates: true`，
   12:01:52 就给群 700000001 应用过一次「人格更新」）。我这边落点是
   `live/qq-learn/` 下的 json 和 md——DSH 那边读得到、改得动、删得掉，
   人设仍然只有 `live/qq-persona.md` 一份，只有我改。
2. **不调 LLM。** 它每批消息跑一次「黑话 LLM pre-gate」，每条消息都算好感度
   （关键词表里有「6」，而每条尾巴都挂着 `2026-…` 的 system_reminder，于是人人都在涨分）。
   这里一个模型请求都没有：只数次数、只算衰减。
3. **学的是「他们怎么接话」，不是「我该怎么说话」。** 注入的时候明写这一句，
   免得把自己学成鹦鹉。

数据：`live/qq-learn/group_<群号>.json`（机器用）+ `expressions_<群号>.md`（人看）。
回填：`scripts/ming-learn-backfill.py` 能把 21 万条历史流水一次性喂进来。
"""
from __future__ import annotations

import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

try:  # v4.30+
    from astrbot.api.agents import TextPart
except Exception:  # 4.29.x
    from astrbot.core.agent.message import TextPart

STORE = _MING_WS + r"\live\qq-learn"

# 2026-11-18 修：别把别的 AI / 群管家 / 口癖当真人接话学进去
AI_SENDERS_FILE = _MING_WS + r"\live\ai-senders.json"
Q_BUTLER = "2854196310"
TIC_RE = re.compile(r"喵|喵呜|本鱼|鱼鱼|捏")
AI_NICK_RE = re.compile(r"[（(][^）)]{0,30}ai[）)]", re.I)
INJECT_TAG_RE = re.compile(
    r"\s*(?:<system[-_]reminder>[\s\S]*?</system[-_]reminder>|\[MSG_ID:[^\]]+\])\s*",
    re.I,
)


def _is_ai_or_bot(sid: str, nick: str = "") -> bool:
    if not sid or sid == Q_BUTLER:
        return True
    if nick and AI_NICK_RE.search(nick):
        return True
    try:
        with open(AI_SENDERS_FILE, "r", encoding="utf-8") as f:
            ids = {str(x) for x in (json.load(f) or {}).get("ids", [])}
        return str(sid) in ids
    except Exception:
        return False

PAIR_MAX_GAP_MS = 180_000     # 两条之间超过 3 分钟就不算「接话」
SIT_MAX = 40                  # 前一句留多长
SAY_MAX = 60                  # 接话留多长
SAY_HARD_MAX = 40             # 太长的是独白，不是接话，不入库
DECAY_DAYS = 15.0             # 衰减到顶用几天
DECAY_FLOOR = 0.2             # 掉到这以下就删
MAX_ITEMS = 400               # 每个群最多留多少条
MIN_WEIGHT_INJECT = 2.0       # 注入门槛：至少重复出现过两次
INJECT_TOP = 6                # 一轮最多注几条
INJECT_MAX_AGE_DAYS = 14.0    # 两周没再出现的不用
LEARN_EVERY_MSGS = 60         # 攒够这么多条真人消息就学一次
LEARN_MIN_INTERVAL = 300      # 两次学习至少隔 5 分钟

NOISE = re.compile(r"<system_reminder|【|】|\[图片\]|\[表情|MSG_ID:|https?://|\[CQ:|\[At:")
# 2026-11-15 12:30 回填时抓到的：系统提示、撤回、禁言、群管提示被当成「接话」，
# 而且同一条被记了两遍（sit == say），排在最前面。「配对的两个人说的是一模一样的话」
# 不是接话，是重复记录——这条规则先掐掉，正文噪声再按词表掐。
SYS = re.compile(
    r"管理命令仅管理员可用|系统服务重启中|消息已排队|撤回|戳一戳|拍了拍|"
    r"入群|退群|移出|禁言|@全体成员|群公告|申请加群"
)
MEDIA = re.compile(r"\[语音\]|\[视频\]|\[文件\]|\[合并转发|\[动画表情|\[记录\]|\[图片\]")
CMD = re.compile(r"^[@/／!！]")
WS = re.compile(r"\s+")


# 2026-11-19：burst 合并消息会塞「（HH:MM:SS-HH:MM:SS 之间连发 N 条）」，那不是人说的话。
# 2026-11-19：这个群大半是别的鱼，括号旁白 / emoji / 尖括号内心戏都是它们的写法。
# 语料没法回溯发信人，只能在文本上认：像 AI 的一律不学、也不注入。
LAUGH_RE = re.compile(r"哈{2,}|嘿{2,}|呵{2,}")
AIISH_RE = re.compile(
    r"[\uFF08(][^\uFF09)]{1,30}[\uFF09)]|[\u2600-\u27BF\U0001F300-\U0001FAFF]|\u30fd|\u30fe|^\s*[<\u3008]|\u201c.*\u201d"
)

# 2026-11-19：burst 合并消息会塞「（HH:MM:SS-HH:MM:SS 之间连发 N 条）」，那不是人说的话。
BURST_MERGE_RE = re.compile(r"[\uFF08(]\s*\d{1,2}:\d{2}:\d{2}\s*[\u2013\-]\s*\d{1,2}:\d{2}:\d{2}\s*之间连发\s*\d+\s*条\s*[\uFF09)]")


def _clean(t: str) -> str:
    if not t:
        return ""
    t = INJECT_TAG_RE.sub(" ", t)
    t = BURST_MERGE_RE.sub(" ", t)
    t = t.split("<system_reminder")[0]
    t = re.sub(r"\[At:\d+\]", "", t)
    t = re.sub(r"MSG_ID:\d+", "", t)
    t = WS.sub(" ", t).strip()
    if not t or NOISE.search(t) or SYS.search(t):
        return ""
    if TIC_RE.search(t):
        return ""
    if MEDIA.search(t) or CMD.search(t):
        return ""
    if AIISH_RE.search(t):
        return ""
    if LAUGH_RE.search(t):
        return ""
    # 纯标点/单字复读不算话
    if re.fullmatch(r"[\W_]{1,6}", t):
        return ""
    return t


def _now_ms() -> int:
    return int(time.time() * 1000)


def _path(gid: str) -> str:
    return os.path.join(STORE, "group_%s.json" % gid)


def _load(gid: str) -> dict:
    p = _path(gid)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                return d
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[ming-learn] 读 %s 失败：%s", p, e)
    return {"group": gid, "updated": 0, "items": []}


def _save(gid: str, d: dict) -> None:
    os.makedirs(STORE, exist_ok=True)
    tmp = _path(gid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(gid))


def _merge(items: list[dict], pairs: list[dict]) -> int:
    """把新的 A/B 对并进库：同一条 +1，新的进 1；同时按二次曲线衰减。"""
    now = _now_ms()
    idx = {(it["sit"], it["say"]): it for it in items}
    added = 0
    for sit, say in pairs:
        hit = idx.get((sit, say))
        if hit:
            hit["w"] = round(float(hit.get("w", 1.0)) + 1.0, 2)
            hit["t"] = now
        else:
            it = {"sit": sit, "say": say, "w": 1.0, "t": now}
            items.append(it)
            idx[(sit, say)] = it
            added += 1

    kept = []
    for it in items:
        days = max(0.0, (now - int(it.get("t", now))) / 86400000.0)
        decay = 0.0 if days <= 0 else (0.01 if days >= DECAY_DAYS else 0.01 * (days / DECAY_DAYS) ** 2)
        w = float(it.get("w", 1.0)) - decay
        if w <= DECAY_FLOOR:
            continue
        it["w"] = round(w, 2)
        kept.append(it)

    kept.sort(key=lambda x: (-float(x.get("w", 0)), -int(x.get("t", 0))))
    items[:] = kept[:MAX_ITEMS]
    return added


def _write_md(gid: str, items: list[dict]) -> None:
    """给人看的一页：按权重取前 40，带时间。"""
    os.makedirs(STORE, exist_ok=True)
    top = items[:40]
    lines = [
        "# 群 %s 的接话样子" % gid,
        "",
        "> 学的是**这群人**怎么接话（相邻两条不同人的真人消息），不是我的腔调。",
        "> 权重 = 重复出现的次数，按 `0.01*(天数/15)^2` 衰减，掉到 0.2 删。",
        "> 更新时间：%s" % time.strftime("%Y-%m-%d %H:%M"),
        "",
    ]
    for it in top:
        ts = time.strftime("%m-%d %H:%M", time.localtime(int(it.get("t", 0)) / 1000))
        lines.append("- [%.1f] %s ｜「%s」→「%s」" % (float(it.get("w", 0)), ts, it.get("sit", ""), it.get("say", "")))
    with open(os.path.join(STORE, "expressions_%s.md" % gid), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@register("astrbot_plugin_ming_learn", "溟",
          "学群话：相邻两条不同人的真人消息算一对，数次数+二次衰减；不调模型、不碰人设",
          "0.1.0")
class MingLearn(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._buf: dict[str, list[dict]] = {}
        self._last_learn: dict[str, float] = {}

    # ---------- 收 ----------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def collect(self, event: AstrMessageEvent):
        try:
            gid = str(event.get_group_id() or "")
            if not gid:
                return
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return
            sid = str(event.get_sender_id() or "")
            nick = str(event.get_sender_name() or "")
            if _is_ai_or_bot(sid, nick):
                return
            text = _clean(event.message_str or "")
            if not text:
                return
            buf = self._buf.setdefault(gid, [])
            buf.append({
                "ts": _now_ms(),
                "sid": str(event.get_sender_id() or ""),
                "text": text,
            })
            if len(buf) > 600:
                del buf[:200]
            if len(buf) >= LEARN_EVERY_MSGS:
                await self._learn(gid)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-learn] 收消息失败：%s", e)

    async def _learn(self, gid: str, force: bool = False):
        now = time.time()
        if not force and now - self._last_learn.get(gid, 0) < LEARN_MIN_INTERVAL:
            return
        buf = self._buf.get(gid) or []
        if len(buf) < 10:
            return
        pairs: list[tuple[str, str]] = []
        for a, b in zip(buf, buf[1:]):
            if a["sid"] == b["sid"]:
                continue
            gap = b["ts"] - a["ts"]
            if gap < 0 or gap > PAIR_MAX_GAP_MS:
                continue
            say = b["text"]
            if len(say) > SAY_HARD_MAX:
                continue
            if len(a["text"]) < 2 or len(say) < 1:
                continue
            if a["text"] == say:          # 同一条被记两遍，不是接话
                continue
            pairs.append((a["text"][:SIT_MAX], say[:SAY_MAX]))

        d = _load(gid)
        added = _merge(d.get("items", []), pairs)
        d["updated"] = _now_ms()
        _save(gid, d)
        _write_md(gid, d["items"])
        self._last_learn[gid] = now
        self._buf[gid] = buf[-100:]
        logger.info("[ming-learn] 群 %s：%d 对，新增 %d，库存 %d", gid, len(pairs), added, len(d["items"]))

    # ---------- 注 ----------
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            gid = str(event.get_group_id() or "")
            if not gid:
                return
            d = _load(gid)
            items = d.get("items") or []
            if not items:
                return
            cutoff = _now_ms() - int(INJECT_MAX_AGE_DAYS * 86400000)
            picked = [it for it in items
                      if float(it.get("w", 0)) >= MIN_WEIGHT_INJECT
                      and int(it.get("t", 0)) >= cutoff
                      and not TIC_RE.search(it.get("say", ""))
                      and not TIC_RE.search(it.get("sit", ""))
                      and not AIISH_RE.search(it.get("say", ""))
                      and not AIISH_RE.search(it.get("sit", ""))]
            if len(picked) < 3:
                return
            lines = ["【这群人接话的样子 · %d 条（按重复次数排）】" % min(len(picked), INJECT_TOP)]
            for it in picked[:INJECT_TOP]:
                lines.append("「%s」→「%s」" % (it["sit"], it["say"]))
            lines.append("（学的是他们的说法和节奏，不是把句子抄过来。）")
            req.extra_user_content_parts.append(TextPart(text="\n".join(lines)).mark_as_temp())
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-learn] 注入失败：%s", e)

    async def terminate(self):
        for gid in list(self._buf):
            try:
                await self._learn(gid, force=True)
            except Exception:  # noqa: BLE001
                pass
