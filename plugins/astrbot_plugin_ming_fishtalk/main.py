"""溟·鱼话（AstrBot 侧）

一句话：让 QQ 这边**照真人的基线说话，说完自己过一遍**。

合并了两样东西：
  · fishtalk（github:Nozzi2455/fishtalk）—— 语料池、文风包、鱼八股检查
  · dsh-fish-tone（MX3a48c5/dsh-fish-tone，MIT）—— 只搬了它的**判断层**：
    结构腔的密度基线、扎堆判定、逐处的「该不该留」

没搬的：它往成稿里插尾巴、按配额补「手滑」的那半层。
那条跟人设第三节（删，不解释，一个回合结束）是反的，插进去只会更像演人。

四件事：
  1. 开聊前 —— 把「说话的松紧」append 进这一轮的 extra_user_content_parts（≤700 字）
  2. 群里有真人说话 —— 写进 ~/.fishtalk/corpus.jsonl（跟 DSH 侧同一个池子）
  3. 说完 —— 跑一遍检查，命中就落 live/fishtalk-qq.json，下一轮当负面提示带进去
  4. 命令 —— /鱼检 /鱼判 /鱼包 /鱼喂（只认她）

不改她说的话。检查只报，不动一个字。
"""

from __future__ import annotations

import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .tone import corpus, judge, rules

try:  # v4.30+ 走这里
    from astrbot.api.agents import TextPart
except Exception:  # 4.29.x
    from astrbot.core.agent.message import TextPart

OWNER_QQ = "100000001"
INJECT_EVERY_LOG = 300  # 秒

SCENE_GROUP = "QQ群"
SCENE_PRIVATE = "QQ私聊"
# 2026-11-19：注入的场合换成「病娇文」（语料取自学过的两本小说对白），
# 学语料仍按真实场合记（QQ群/QQ私聊），只是说话时读那一节。
INJECT_SCENE = {"QQ群": "病娇文", "QQ私聊": "病娇文"}
# 2026-11-19 08:2x 按复核单停注：病娇文是小说对白（5776 条，范例「哟，灵梦…」），
# 拿它当日常 QQ 的「说话松紧」示范，示范本身就是错的文体；而 QQ群（2144 条）/
# QQ私聊（94 条）两个真实场景的备选语料夹着昵称和测试者提问，不能直接顶上。
# 先整段停注；语料池、检查器、/鱼包 都保留，随时能开回来。
INJECT_ENABLED = False

# 2026-11-18 修：/rp on 不压短句；别的 AI、群管家、喵捏口癖不许当真人语料学
ROLEPLAY_STATE = r"C:\dsh\hers_workplace\live\roleplay-on.json"
AI_SENDERS_FILE = r"C:\dsh\hers_workplace\live\ai-senders.json"
Q_BUTLER = "2854196310"
AI_NICK_RE = re.compile(r"[（(][^）)]{0,30}ai[）)]", re.I)


def _scene_of(event: AstrMessageEvent) -> str:
    umo = getattr(event, "unified_msg_origin", "") or ""
    return SCENE_GROUP if "Group" in umo or "group" in umo else SCENE_PRIVATE


def _is_owner(event: AstrMessageEvent) -> bool:
    try:
        return str(event.get_sender_id()) == OWNER_QQ
    except Exception:
        return False


def _is_self(event: AstrMessageEvent) -> bool:
    try:
        return str(event.get_sender_id()) == str(event.get_self_id())
    except Exception:
        return False


def _is_rp_on(umo: str) -> bool:
    try:
        with open(ROLEPLAY_STATE, "r", encoding="utf-8") as f:
            return bool((json.load(f) or {}).get(umo))
    except Exception:
        return False


def _is_ai_or_bot(event: AstrMessageEvent) -> bool:
    try:
        sid = str(event.get_sender_id() or "")
        nick = str(event.get_sender_name() or "")
    except Exception:
        return False
    if sid == Q_BUTLER or (nick and AI_NICK_RE.search(nick)):
        return True
    try:
        with open(AI_SENDERS_FILE, "r", encoding="utf-8") as f:
            return sid in {str(x) for x in (json.load(f) or {}).get("ids", [])}
    except Exception:
        return False


class MingFishTone(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._last_log = 0.0

    async def initialize(self):
        pack = corpus.load_pack()
        n = (pack.get("counts") or {}).get("human") if pack else 0
        b = corpus.brief(INJECT_SCENE.get(SCENE_GROUP, SCENE_GROUP))
        logger.info(
            "[ming-fishtalk] 起来了：文风包 %s（真人 %s 条），注入开关 %s，片段 %s 字（只给检查用）",
            "有" if pack else "没有", n or 0,
            "开" if INJECT_ENABLED else "关",
            len(b),
        )

    # ── 1. 开聊前：把「说话的松紧」塞进这一轮 ─────────────────────
    @filter.on_llm_request(priority=50)
    async def inject_tone(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            if not INJECT_ENABLED:
                return
            if _is_rp_on(getattr(event, "unified_msg_origin", "") or ""):
                return
            scene = _scene_of(event)
            text = corpus.brief(INJECT_SCENE.get(scene, scene))
            if not text:
                return

            parts = [text]
            last = corpus.load_problems(max_age_min=30)
            if last:
                problems = "；".join(last.get("problems") or [])
                parts.append(
                    f"【上一句踩了】{last.get('text', '')[:40]} —— {problems}。"
                    "这一轮别再来一次，也别解释自己上一句。"
                )

            block = "\n".join(parts)
            try:
                req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
            except Exception:
                # 老版本没有 extra_user_content_parts：退回拼在 system_prompt 后面
                req.system_prompt = (getattr(req, "system_prompt", "") or "") + "\n\n" + block

            now = time.time()
            if now - self._last_log > INJECT_EVERY_LOG:
                self._last_log = now
                logger.info("[ming-fishtalk] 注入 %s 字（%s）", len(block), INJECT_SCENE.get(scene, scene))
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-fishtalk] 注入失败（不影响本轮）：%s", e)

    # ── 2. 群里的真人说话：进语料 ─────────────────────────────────
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def learn_speech(self, event: AstrMessageEvent):
        try:
            if _is_self(event):
                return
            if _is_ai_or_bot(event):
                return
            text = (event.message_str or "").strip()
            if not text or len(text) < 2 or text.startswith("/"):
                return
            scene = _scene_of(event)
            # 别人的私聊不学（那是别人的话，不是公共语料）
            if scene == SCENE_PRIVATE and not _is_owner(event):
                return
            where = ""
            try:
                where = str(event.get_group_id() or "") or "private"
            except Exception:
                where = "private"
            corpus.learn(text, where=where, scene=scene)
        except Exception as e:  # noqa: BLE001
            logger.debug("[ming-fishtalk] 学话失败：%s", e)

    # ── 3. 说完：过一遍，只报不改 ─────────────────────────────────
    @filter.on_llm_response(priority=100)
    async def check_reply(self, event: AstrMessageEvent, resp):
        try:
            if _is_rp_on(getattr(event, "unified_msg_origin", "") or ""):
                return
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                return
            human, said = corpus.pools(limit=300)
            res = rules.check(
                text,
                fish_pool=[],
                human_pool=human,
                said_pool=said,
            )
            v = judge.assess(text)
            problems = list(res["problems"])
            if v["band"] in ("heavy", "template"):
                problems.append(f"结构腔{v['bandLabel']}：{v['questions'][0] if v['questions'] else '句式扎堆'}")
            if problems:
                corpus.save_problems(problems, text)
                corpus.probe(f"check problems={len(problems)} band={v['band']} chars={len(text)}")
            else:
                corpus.remember_said(text)
                corpus.probe(f"check ok band={v['band']} chars={len(text)}")
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-fishtalk] 出站检查失败：%s", e)

    # ── 4. 命令（只认她）──────────────────────────────────────────
    def _owner_only(self, event: AstrMessageEvent) -> bool:
        return _is_owner(event) or _scene_of(event) == SCENE_PRIVATE and _is_owner(event)

    @filter.command("鱼检")
    async def cmd_check(self, event: AstrMessageEvent, text: str = ""):
        """/鱼检 <一句话> —— 发之前过一遍，只报问题，不改写。"""
        if not _is_owner(event):
            return
        body = (text or "").strip() or (event.message_str or "").replace("/鱼检", "").strip()
        if not body:
            yield event.plain_result("用法：/鱼检 <一句话>")
            return
        human, said = corpus.pools(limit=300)
        res = rules.check(body, human_pool=human, said_pool=said)
        lines = ["过" if res["ok"] else "别发"]
        for p in res["problems"]:
            lines.append("· " + p)
        for n in res["notes"]:
            lines.append("· " + n)
        yield event.plain_result("\n".join(lines))

    @filter.command("鱼判")
    async def cmd_judge(self, event: AstrMessageEvent, text: str = ""):
        """/鱼判 <一段文字> —— 结构腔：密度、扎堆、逐处该不该留。"""
        if not _is_owner(event):
            return
        body = (text or "").strip() or (event.message_str or "").replace("/鱼判", "").strip()
        if not body:
            yield event.plain_result("用法：/鱼判 <一段文字>")
            return
        yield event.plain_result(judge.render(judge.assess(body)))

    @filter.command("鱼包")
    async def cmd_pack(self, event: AstrMessageEvent):
        """/鱼包 —— 现在注入的是哪一段。"""
        if not _is_owner(event):
            return
        scene = _scene_of(event)
        b = corpus.brief(scene)
        stat = corpus.realtime_stats()
        tail = ""
        if stat:
            tail = (
                f"\n\n现场（最近 {stat['n']} 条）：中位 {stat['median']} 字，"
                f"不加标点 {round(stat['noEndRatio'] * 100)}%"
            )
        yield event.plain_result((b or "还没有文风包（~/.fishtalk/pack.json 不在）") + tail)

    @filter.command("鱼喂")
    async def cmd_feed(self, event: AstrMessageEvent, text: str = ""):
        """/鱼喂 <一句真人的话> —— 手动塞进语料。"""
        if not _is_owner(event):
            return
        body = (text or "").strip() or (event.message_str or "").replace("/鱼喂", "").strip()
        if not body:
            yield event.plain_result("用法：/鱼喂 <一句真人的话>")
            return
        ok = corpus.learn(body, where="manual", scene=_scene_of(event))
        yield event.plain_result("记下了" if ok else "这条没进（太短、重复或不合法）")
