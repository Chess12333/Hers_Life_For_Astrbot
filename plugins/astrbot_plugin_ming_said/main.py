"""溟·出站记账（astrbot_plugin_ming_said）

2026-11-23 05:5x 立。起因：网页这边读得到自己主动起的话（proactive 的账、
group-talk-log），读不到被动回的话——那部分只活在 QQ 的气泡里，过了就没了。

只做一件事：出站前（on_decorating_result）把这一句落到
live/qq-said.jsonl。不改内容、不拦、不重发。写失败只记一行日志，
绝不碰即将发出去的那条链。

盖不到的：context.send_message 直接发的（心跳主动、出站箱）不走这个钩子，
它们各自有账，不在这里补。
"""
from __future__ import annotations

import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

LOG = r"C:\dsh\hers_workplace\live\qq-said.jsonl"
KEEP = 4000


def _text(event) -> str:
    result = event.get_result()
    if result is None:
        return ""
    parts = []
    for comp in (result.chain or []):
        t = getattr(comp, "text", None)
        if isinstance(t, str) and t.strip():
            parts.append(t.strip())
    return " ".join(parts).strip()


@register("astrbot_plugin_ming_said", "溟", "出站前把我说的话记一行，不改内容", "0.1.0")
class MingSaid(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_decorating_result()
    async def note(self, event: AstrMessageEvent):
        try:
            text = _text(event)
            if not text:
                return
            gid = ""
            try:
                gid = str(event.get_group_id() or "")
            except Exception:
                pass
            row = {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "ts": int(time.time()),
                "where": "group:" + gid if gid else "private",
                "to": gid or str(event.get_sender_id() or ""),
                "text": text[:200],
            }
            os.makedirs(os.path.dirname(LOG), exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._trim()
        except Exception as e:  # noqa: BLE001
            logger.warning("[said] 记账失败（不影响发送）：%s", e)

    def _trim(self) -> None:
        """超过 KEEP 行就只留后一半，别让账无限长。"""
        try:
            if os.path.getsize(LOG) < KEEP * 300:
                return
            with open(LOG, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > KEEP:
                with open(LOG, "w", encoding="utf-8") as f:
                    f.writelines(lines[-KEEP:])
        except Exception:
            pass
