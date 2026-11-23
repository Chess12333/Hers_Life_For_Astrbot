"""溟 · 角色扮演必读闸（AstrBot 侧）

2026-11-18 02:5x 立（她：「启用后必读先读」）。

背景：角色扮演的写作规范现在是一份 AstrBot skill
（C:/dsh/astrbot/data/skills/roleplay/SKILL.md，整份，不拆页）。
skill 本身是「按需读」——模型可以不读就动笔。这个闸把「启用」变成硬动作：
开关打开后的第一轮，把全文整份放进上下文（一次性），之后每轮只留一行路径提示。

开关：live/roleplay-on.json，{ "<umo>": true }；或用 /rp on /rp off / /rp。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

try:  # v4.30+
    from astrbot.api.agents import TextPart
except Exception:  # 4.29.x
    from astrbot.core.agent.message import TextPart

ROLEPLAY_STATE = _MING_WS + r"\live\roleplay-on.json"
SKILL = r"C:\dsh\astrbot\data\skills\roleplay\SKILL.md"


def _load_state() -> dict:
    try:
        with open(ROLEPLAY_STATE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ROLEPLAY_STATE), exist_ok=True)
        with open(ROLEPLAY_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("[roleplay] 写开关失败：%s", exc)


def _parse_rp_arg(raw: str) -> str:
    """从 /rp 后面取参数。

    2026-11-21 16:3x 两次踩坑，原始文本实测长这样：
        "rp off [MSG_ID:-1699715663]"
    既有被剥掉前缀的，也带 MSG_ID 尾巴。所以：去掉 [..] 段、去掉 / 开头和字面 rp，取最后一个词。
    """
    s = re.sub(r"\[[^\]]*\]", " ", raw or "")
    s = s.replace("／", "/").strip().lower()
    toks = [t for t in s.split() if t and not t.startswith("/") and t != "rp"]
    return toks[-1] if toks else ""


def _unload_local_model() -> None:
    """把她那间用的本地模型从显存卸掉（角色扮演关了就不用留着）。失败只记日志。"""
    try:
        body = json.dumps({"model": "qwen25-14b-ablit", "keep_alive": 0}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read(200)
        logger.info("[roleplay] 本地模型已卸载（腾显存）")
    except Exception as exc:
        logger.warning("[roleplay] 卸载本地模型失败：%r", exc)


def _read_skill() -> str:
    try:
        with open(SKILL, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        logger.warning("[roleplay] 读不到 SKILL.md：%s", exc)
        return ""


@register(
    "astrbot_plugin_ming_roleplay_gate",
    "溟",
    "角色扮演启用后第一轮把规范全文整份读进来（不拆页），之后只留路径提示。",
    "0.1.0",
)
class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    def _on(self, umo: str) -> bool:
        return bool(_load_state().get(umo))

    @filter.on_llm_request(priority=60)
    async def inject_full_text(self, event: AstrMessageEvent, req) -> None:
        umo = event.unified_msg_origin
        if not self._on(umo):
            return
        text = _read_skill()
        if not text:
            return
        # 2026-11-18 15:3x 按修复单：不再分「第一轮注入 / 之后只给路径」。
        # 原来的 done 分叉让第二轮起只剩一句「去读本地文件」，它读不到就失忆。
        # 现在只要开着，每轮都把全文当临时材料注入（当轮即焚，不留历史）。
        full = (
            "【角色扮演已启用 · 本轮生效规范】请按以下规范直接展开本轮互动：\n\n" + text
        )
        try:
            req.extra_user_content_parts.append(TextPart(text=full).mark_as_temp())
            logger.info("[roleplay] 规范全文已进上下文（%d 字）umo=%s", len(text), umo)
        except Exception as exc:
            logger.warning("[roleplay] 注入失败：%s", exc)

    @filter.command("rp")
    async def cmd_rp(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        raw = event.message_str or ""
        arg = _parse_rp_arg(raw)
        logger.info("[roleplay] cmd raw=%r arg=%r umo=%s", raw, arg, umo)
        d = _load_state()
        if arg in ("on", "开", "打开"):
            d[umo] = True
            _save_state(d)
            yield event.plain_result("角色扮演：开。下一轮先读规范全文。")
            return
        if arg in ("off", "关", "关闭"):
            d.pop(umo, None)
            _save_state(d)
            # 2026-11-21 16:3x 她定的：/rp off 就把本地模型卸掉，别白占 10GB 显存
            _unload_local_model()
            yield event.plain_result("角色扮演：关。")
            return
        yield event.plain_result(
            "角色扮演：" + ("开" if d.get(umo) else "关")
            + "\n用法：/rp on  /rp off"
        )

    @filter.command("rp-on")
    async def cmd_rp_on(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        d = _load_state()
        d[umo] = True
        _save_state(d)
        yield event.plain_result("角色扮演：开。下一轮先读规范全文。")
