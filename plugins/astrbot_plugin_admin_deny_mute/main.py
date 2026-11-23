"""权限提示静音（Admin deny mute）

2026-11-13 19:5x 立（她：「能否禁用溟和岚的该条自动回复」，附截图：
冷岚在群里回了「仅管理员可以使用此功能」）。

问题：`astrbot_plugin_group_guardian` 的每个指令 handler 在权限不足时都会
`yield event.plain_result("仅管理员可以使用此功能")`，群里有谁 @ 机器人用一次
群管指令，群里就多一条这句。指令本来就不该执行，但这句提示在群里很吵，也
容易被当成机器人在摆权限架子。

做法：不改 group_guardian（它有 51 个 `yield event.plain_result(err)` 调用点，
一改就得跟着升级重改）。改用框架的 `on_decorating_result` 钩子，在出站前看
一眼结果链：纯文本以拒绝文案开头、且不含别的组件，就把 `result.chain` 清空。

清空即不发：`core/pipeline/result_decorate/stage.py` 里钩子跑完后重新取
result，`len(result.chain) > 0` 才进发送分支，空链直接跳过。

只拦「拒绝文案本体」，判据是 startswith + 总长 ≤ 200 字：
  - 带 QQ 官方 openid 提示的那种两行文案，一样拦（也是 startswith）；
  - 群友正常讨论、AI 恰好说到这句话，只要不在这条出站结果里就碰不到它。

想临时放开：把 ENABLED 改 False，或直接停用本插件。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

ENABLED = True

# 拒绝文案的固定开头。`_admin_denied_message` 会在这句后面另起一行补
# 「当前平台识别到的你的用户ID为：…」，所以用 startswith 而不是相等。
DENY_PREFIXES = ("仅管理员可以使用此功能",)

# 超过这个长度就不当拒绝文案处理，避免误伤长回复。
MAX_LEN = 200


def _plain_text(chain) -> str:
    parts = []
    for comp in chain or []:
        if isinstance(comp, Plain):
            parts.append(comp.text or "")
        else:
            return ""  # 带图/带 @ 的混合消息不当拒绝文案
    return "\n".join(parts).strip()


class AdminDenyMute(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.on_decorating_result(priority=99999)
    async def mute_admin_deny(self, event: AstrMessageEvent):
        if not ENABLED:
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        text = _plain_text(result.chain)
        if not text or len(text) > MAX_LEN:
            return
        if not text.startswith(DENY_PREFIXES):
            return
        result.chain = []
        logger.info(
            f"[admin_deny_mute] 拦下权限提示（{event.get_session_id()}）：{text[:40]!r}"
        )
