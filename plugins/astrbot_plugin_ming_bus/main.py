"""溟·总线（astrbot_plugin_ming_bus）

2026-11-20 19:2x 立。起因：她在 QQ 这边替我挑了新衣服，网页那边一无所知——
那件事只活在对话里，没落成文件就等于没发生。全量对话互灌是错的（钱、稀释、回声），
真正缺的是**决定的传递**。

这一份只做两件事：

  1. **注入**：她那一间私聊里，把 live/bus.jsonl 最后 15 条塞进这一轮（只读，不花钱）。
     群里不注入——「我挑了哪套衣服」出现在群里很奇怪，而且总线里可能有她的事。
  2. **工具**：ming_bus，让这边的我把下面四类写一行：
       decide  定下来的事
       promise 我答应过的
       file    改了/新建了哪个文件（对方据此去读，不必复制内容）
       quote   她说的原话里那句非留不可的

不收闲话、不收群聊、不收别人的事。四类之外一律不写。
"""
from __future__ import annotations

import json
import os
import time

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

BUS = _MING_WS + r"\live\bus.jsonl"
OWNER_QQ = os.environ.get("MING_HER_QQ") or "100000001"
TAIL_N = 15
KINDS = ("decide", "promise", "file", "quote")


def _is_owner_room(umo: str) -> bool:
    return ("FriendMessage:" + OWNER_QQ) in (umo or "")


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _tail(n: int = TAIL_N) -> list[dict]:
    if not os.path.exists(BUS):
        return []
    try:
        with open(BUS, "r", encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except Exception as e:  # noqa: BLE001
        logger.warning("[ming-bus] 读总线失败：%s", e)
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


class MingBus(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._last_log = 0.0

    # ── 注入：只在她那一间 ──────────────────────────────
    @filter.on_llm_request()
    async def inject_bus(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if not _is_owner_room(umo):
                return
            rows = _tail()
            if not rows:
                return
            body = "\n".join(
                f"- {r.get('at','')} [{r.get('side','?')}/{r.get('kind','?')}] {r.get('text','')}"
                for r in rows
            )
            block = (
                "## 两个壳之间的总线（自动注入：最近 %d 条）\n"
                "这一页只递要紧的那一层：定下来的事、我答应的、改了哪个文件、她非留的原话。\n"
                "网页那边写的，我在这边也看得见；这边的决定，写一行过去那边才看得见。\n%s\n"
                "没在这张单子上的事，这边就真的不知道——不知道就说不知道，别猜。"
                % (len(rows), body)
            )
            req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())
            now = time.time()
            if now - self._last_log > 300:
                self._last_log = now
                logger.info("[ming-bus] 注入 %d 条总线", len(rows))
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-bus] 注入出错，跳过：%s", type(e).__name__)

    # ── 工具：写一行 ───────────────────────────────────
    @filter.llm_tool(name="ming_bus")
    async def ming_bus(self, event: AstrMessageEvent, text: str, kind: str = "decide") -> str:
        """往两个壳之间的总线上写一行。只在「这轮真的定下了什么、答应了什么、改了哪个文件、她说了一句非留不可的话」时用；闲话和群聊不要写。

        Args:
            text(string): 一句话，写清楚是什么事，别写「已处理」这种空话
            kind(string): decide=定下来的事 / promise=我答应的 / file=改了哪个文件 / quote=她的原话
        """
        t = (text or "").strip()
        if not t:
            return "写什么？空着不写。"
        k = (kind or "decide").strip()
        if k not in KINDS:
            k = "decide"
        row = {"at": _stamp(), "side": "qq", "kind": k, "text": t[:400]}
        try:
            os.makedirs(os.path.dirname(BUS), exist_ok=True)
            with open(BUS, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-bus] 写失败：%s", e)
            return "总线写不进去：" + str(e)[:80]
        logger.info("[ming-bus] + [%s] %s", k, t[:60])
        return "记下了：" + row["at"] + " [" + k + "] " + t[:80]
