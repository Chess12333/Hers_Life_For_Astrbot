"""溟·输入状态回执（AstrBot 侧）

2026-11-18 立。她要的：snowluma 那边要有输入状态回执——她发消息的时候，QQ 上看得见
「正在输入」，而不是等几分钟突然蹦出一条，看着像死机。

OneBot v11 的动作是 set_input_status{user_id, event_type}：1 = 正在输入，0 = 取消。
snowluma 实现了它（容器 /app/runtime/index.mjs 第 13894 行 defineAction("set_input_status")）。
QQ 只对私聊显示这个状态，群聊没有，所以这里只接私聊。

做法：
  - 私聊进来 → 立刻 set(1)，之后每 refresh_seconds 续一次（默认 5 秒），最多续 max_seconds
  - 消息发出去（after_message_sent）→ 取消续期并 set(0)
  - 同一个窗口再来一条 → 先取消上一条续期，重新计时，不会叠出两条循环
  - 失败只记一次日志并停用自己，绝不影响回复链
"""
from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star


class MingTyping(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        try:
            self.refresh = max(2.0, float(self.config.get("refresh_seconds") or 5))
        except (TypeError, ValueError):
            self.refresh = 5.0
        try:
            self.max_seconds = max(
                self.refresh, float(self.config.get("max_seconds") or 30)
            )
        except (TypeError, ValueError):
            self.max_seconds = 30.0
        self.platforms = {
            x.strip()
            for x in str(self.config.get("platforms") or "aiocqhttp").split(",")
            if x.strip()
        }
        self.tasks: dict[str, asyncio.Task] = {}
        self.broken = False
        logger.info(
            "[typing] 输入状态回执：refresh=%.0fs max=%.0fs platforms=%s",
            self.refresh,
            self.max_seconds,
            ",".join(sorted(self.platforms)) or "(不限)",
        )

    def _ok(self, event: AstrMessageEvent) -> bool:
        if not self.enabled or self.broken:
            return False
        if self.platforms and str(event.get_platform_name() or "") not in self.platforms:
            return False
        return getattr(event, "bot", None) is not None

    async def _set(self, event: AstrMessageEvent, user_id: str, value: int) -> None:
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        await bot.call_action(
            "set_input_status", user_id=int(user_id), event_type=int(value)
        )

    def _cancel(self, umo: str) -> None:
        task = self.tasks.pop(umo, None)
        if task is not None and not task.done():
            task.cancel()

    async def _keep(self, event: AstrMessageEvent, umo: str, user_id: str) -> None:
        started = time.monotonic()
        try:
            while True:
                await self._set(event, user_id, 1)
                if time.monotonic() - started >= self.max_seconds:
                    return
                await asyncio.sleep(self.refresh)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.broken = True
            logger.warning(
                "[typing] set_input_status 调用失败，本插件停用到重启：%r", exc
            )

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE, priority=-100)
    async def on_private(self, event: AstrMessageEvent) -> None:
        if not self._ok(event) or not event.is_private_chat():
            return
        umo = event.unified_msg_origin
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return
        self._cancel(umo)
        self.tasks[umo] = asyncio.create_task(self._keep(event, umo, user_id))

    @filter.on_decorating_result()
    async def on_result(self, event: AstrMessageEvent) -> None:
        """回复已经生成出来了：停止续期。

        pacer 是拿 context.send_message 主动发的，走不到 after_message_sent，
        所以真正的「停」要在这里做——停续期之后 QQ 那边几秒内自己收掉，
        正好落在消息到达前后，比提前 set(0) 自然。
        """
        if not self._ok(event) or not event.is_private_chat():
            return
        self._cancel(event.unified_msg_origin)

    @filter.after_message_sent()
    async def on_sent(self, event: AstrMessageEvent) -> None:
        if not self._ok(event) or not event.is_private_chat():
            return
        umo = event.unified_msg_origin
        user_id = str(event.get_sender_id() or "")
        had = umo in self.tasks
        self._cancel(umo)
        if not had:
            return
        try:
            await self._set(event, user_id, 0)
        except Exception as exc:  # noqa: BLE001
            self.broken = True
            logger.warning("[typing] 取消输入状态失败，本插件停用：%r", exc)

    async def terminate(self) -> None:
        for umo in list(self.tasks):
            self._cancel(umo)
