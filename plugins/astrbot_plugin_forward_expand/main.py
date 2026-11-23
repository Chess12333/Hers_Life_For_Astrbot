"""合并转发展开（2026-11-11）

问题：
    QQ 的合并转发到 AstrBot 只剩一个 `Forward` 段，里面只有一个 id，没有内容。
    `event.message_str` 因此为空，内置 runner 在 astr_main_agent.py 里判
    「没有 prompt / 没有图 / 没有语音」→ return None，模型完全不被调用。
    现象就是：发一条合并转发给 bot，它一声不响。

做法：
    消息进 LLM 之前（event_message_type 层，早于 LLM 阶段），
    把这一个 Forward 段用 get_forward_msg 逐层递归展开，
    拼成「<昵称> 内容」的多行文本：
      ① 写进 event.message_str（这是 req.prompt 的来源）
      ② 同时在 message 里补一个 Plain 段（别的插件读 message 也能看到）
      ③ 转发里的图片补成 Image 段（会走 caption 或直接喂多模态模型）
      ④ 原来的 Forward 段从 message 里摘掉，免得下游再当空壳

边界：
    - 深度上限 4 层，条数上限 120 行，单层失败只记一行不中断
    - 内层 id 有时效（实测 90 秒后 ApiNotAvailable），所以必须收到就展开
    - 展开失败时兜底写「[合并转发·展开失败]」，不让它继续装成空消息
"""

from __future__ import annotations

import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Forward, Image, Plain, Reply
from astrbot.api.star import Context, Star

PROBE = r"C:\dsh\hers_workplace\live\forward-expand.log"
MAX_DEPTH = 4
MAX_LINES = 120


def probe(tag: str, msg: str):
    try:
        d = time.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(os.path.dirname(PROBE), exist_ok=True)
        with open(PROBE, "a", encoding="utf-8") as f:
            f.write(f"{d}  [{tag}] {msg}\n")
    except Exception:
        pass


class ForwardExpand(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def _bot(self):
        for p in self.context.platform_manager.platform_insts:
            b = getattr(p, "bot", None)
            if b is not None:
                return b
        return None

    async def _walk(self, bot, fid, depth, out, imgs, seen):
        if depth > MAX_DEPTH or not fid or fid in seen or len(out) >= MAX_LINES:
            return
        seen.add(fid)
        try:
            r = await bot.call_action("get_forward_msg", message_id=fid)
        except Exception as e:
            out.append(f"{'  ' * depth}[第{depth}层展开失败：{type(e).__name__}]")
            return
        for node in (r or {}).get("messages", []):
            if len(out) >= MAX_LINES:
                return
            who = (node.get("sender") or {}).get("nickname") or node.get("user_id") or "?"
            for seg in node.get("message", []):
                t = seg.get("type")
                d = seg.get("data", {}) or {}
                pad = "  " * depth
                if t in ("text", "plain"):
                    tx = (d.get("text") or "").strip()
                    if tx:
                        out.append(f"{pad}<{who}> {tx}")
                elif t == "image":
                    url = d.get("url") or d.get("file") or ""
                    out.append(f"{pad}<{who}> [图片]")
                    if isinstance(url, str) and url.startswith("http"):
                        imgs.append(url)
                elif t in ("face", "mface"):
                    out.append(f"{pad}<{who}> [表情]")
                elif t == "xml":
                    raw = d.get("data") or ""
                    # 表情包 / 卡片只留一句提示，不倒 xml 原文
                    out.append(f"{pad}<{who}> [卡片消息]" if "emoji" not in raw else f"{pad}<{who}> [表情包]")
                elif t == "json":
                    out.append(f"{pad}<{who}> [卡片消息]")
                elif t in ("forward", "nodes"):
                    nid = d.get("id")
                    if nid:
                        out.append(f"{pad}<{who}> [转发 ↓]")
                        await self._walk(bot, nid, depth + 1, out, imgs, seen)
                    else:
                        out.append(f"{pad}<{who}> [转发(无id)]")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def expand(self, event: AstrMessageEvent):
        try:
            segs = event.message_obj.message or []
            fw = [c for c in segs if isinstance(c, Forward)]
            if not fw:
                await self.expand_reply(event)
                return
            bot = self._bot()
            if bot is None:
                probe("err", "拿不到 bot")
                return

            out: list[str] = []
            imgs: list[str] = []
            for c in fw:
                await self._walk(bot, getattr(c, "id", ""), 0, out, imgs, set())

            body = "\n".join(out).strip() or "[合并转发·展开为空]"
            header = "[这是一条 QQ 合并转发，下面是展开后的内容]"
            block = f"{header}\n{body}"

            # ① 摘掉原来的空壳
            segs[:] = [c for c in segs if not isinstance(c, Forward)]
            # ② 补 Plain（下游读 message 也能看到）
            segs.insert(0, Plain(text=block))
            # ③ 图片补回来
            for u in imgs[:8]:
                try:
                    segs.append(Image.fromURL(u))
                except Exception:
                    pass
            # ④ 写进 message_str —— req.prompt 的来源
            old = (event.message_obj.message_str or "").strip()
            _merged = (block + ("\n" + old if old else "")).strip()
            event.message_obj.message_str = _merged
            # AstrMessageEvent.message_str 是独立字段，pipeline 读的是它
            # （astr_message_event.py:88 赋值、:194 返回）；只写 message_obj 那个没用
            event.message_str = _merged

            probe(
                "expanded",
                f"umo={event.unified_origin if hasattr(event,'unified_origin') else event.unified_msg_origin} "
                f"行={len(out)} 图={len(imgs)}\n{block[:600]}",
            )
        except Exception as e:
            logger.warning(f"forward-expand: {type(e).__name__}: {e}")
            probe("err", f"{type(e).__name__}: {e}")

    # ── 引用展开（2026-11-18 22:1x 加）────────────────────────────────
    # 问题：引用一条「只有图片 / 合并转发」的消息时，AstrBot 的引用解析得到空壳，
    # message_str 只剩 "[引用消息] [At:100000005]"，下游当成空消息送过去，
    # 模型一声不响。她 21:58 在深水区连着踩了两次（「qq侧不会回复引用的图片」）。
    # 做法：拿 Reply.id 调 get_msg 取原消息，逐段翻成文本写进 message_str，
    # 图补成 Image 段（主模型是纯文本，AstrBot 会拿 mimo 把图转述成字）。
    MAX_REPLY_IMAGES = 2

    @staticmethod
    def _core_text(text: str) -> str:
        t = re.sub(r"<system_reminder>.*?</system_reminder>", "", text or "", flags=re.S)
        t = re.sub(r"\[At:\d+\]", "", t)
        t = re.sub(r"\[引用消息[^\]]*\]", "", t)
        return t.strip()

    PLACEHOLDERS = ("", "图片", "[图片]", "表情", "[表情]", "转发", "[转发]", "文件", "[文件]")

    def _need_reply_expand(self, event) -> bool:
        segs = event.message_obj.message or []
        if not any(isinstance(c, Reply) for c in segs):
            return False
        txt = event.message_str or ""
        if "[引用消息(" in txt:
            # 2026-11-18 22:1x 实测：引用一条图片消息，核心解析只给出占位符
            # "[引用消息(昵称: 图片 MSG_ID:-1547805976)]" —— 有壳没内容，图也没挂上去。
            # 只有占位符（图片/表情/转发/空 + MSG_ID）才自己展开，真正文不碰。
            m = re.search(r"\[引用消息\((.*?)\)\]", txt, re.S)
            inner = (m.group(1) if m else "").strip()
            body = inner.split(": ", 1)[1] if ": " in inner else inner
            body = re.sub(r"MSG_ID:\s*-?\d+", "", body).strip()
            if body not in self.PLACEHOLDERS:
                return False
            return True  # 壳里只有占位符 → 自己展开（哪怕后面还跟着正文）
        # 2026-11-18 22:1x 修正：原来只在「整条没字」时才展开，可她那句是
        # "[引用消息] [At:100000005] 这是什么" —— 有正文、引用壳还是空的，
        # 照样什么都没展开（22:09:23 那次仍没回）。有正文也补。
        if "[引用消息]" in txt or "[引用消息]" in (getattr(event.message_obj, "message_str", "") or ""):
            return True
        return not self._core_text(txt)

    async def expand_reply(self, event) -> None:
        try:
            segs0 = event.message_obj.message or []
            if any(isinstance(c, Reply) for c in segs0):
                probe(
                    "seen",
                    f"umo={event.unified_msg_origin} str={((event.message_str or '')[:120])!r} "
                    f"types={[type(c).__name__ for c in segs0]}",
                )
            if not self._need_reply_expand(event):
                return
            segs = event.message_obj.message or []
            replies = [c for c in segs if isinstance(c, Reply)]
            bot = self._bot()
            if bot is None or not replies:
                return
            rp = replies[0]
            mid = getattr(rp, "id", None) or getattr(rp, "message_id", None)
            if not mid:
                return
            out: list[str] = []
            imgs: list[str] = []
            try:
                raw = await bot.call_action("get_msg", message_id=mid)
            except Exception as e:
                out.append(f"[引用展开失败：{type(e).__name__}]")
                raw = {}
            sender = ""
            try:
                sd = (raw or {}).get("sender") or {}
                sender = sd.get("card") or sd.get("nickname") or ""
            except Exception:
                sender = ""
            for seg in ((raw or {}).get("message") or []):
                t = seg.get("type")
                d = seg.get("data") or {}
                who = f"<{sender}> " if sender else ""
                if t in ("text", "plain"):
                    tx = (d.get("text") or "").strip()
                    if tx:
                        out.append(f"  {who}{tx}")
                elif t == "image":
                    out.append(f"  {who}[图片]")
                    url = d.get("url") or d.get("file") or ""
                    if isinstance(url, str) and url.startswith("http"):
                        imgs.append(url)
                elif t in ("face", "mface"):
                    out.append("  [表情]")
                elif t in ("forward", "nodes"):
                    nid = d.get("id")
                    if nid:
                        await self._walk(bot, nid, 1, out, imgs, set())

            if not out and not imgs:
                out = ["  [引用了一条消息，内容我这边读不到]"]
            block = "[对方引用了这条消息]\n" + "\n".join(out)
            old = (event.message_str or "").strip()
            # 把核心留下的占位壳摘掉，别让它跟展开内容重复
            old = re.sub(r"\[引用消息\(.*?\)\]", "", old, flags=re.S)
            old = re.sub(r"\[引用消息\]", "", old).strip()
            merged = (block + ("\n" + old if old else "")).strip()
            event.message_obj.message_str = merged
            event.message_str = merged
            if imgs:
                segs.insert(0, Plain(text=block))
                for u in imgs[: self.MAX_REPLY_IMAGES]:
                    try:
                        segs.append(Image.fromURL(u))
                    except Exception:
                        pass
            probe("reply", f"umo={event.unified_msg_origin} 行={len(out)} 图={len(imgs)}\n{block[:400]}")
        except Exception as e:
            logger.warning(f"forward-expand(reply): {type(e).__name__}: {e}")
            probe("err", f"reply {type(e).__name__}: {e}")

