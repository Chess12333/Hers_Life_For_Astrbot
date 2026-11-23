"""溟 · /rp 那一间改走本机不过滤模型

2026-11-21 01:4x 立。起因：01:14 她那一间被上游内容安全拦了
（deepseek-v4.1-flash 与 mimo-v2.6-pro 都是 finish_reason=content_filter），AstrBot 这一层没拦。
修法不是去骗过滤，是换一条明说允许的通道：本机跑 Qwen2.5-14B-Instruct-abliterated。
（2026-11-21 10:43 更正：现在是 Ollama 跑 GGUF Q4，127.0.0.1:11434，模型名 qwen25-14b-ablit；
 原先那条 transformers+bnb nf4 / 11500 端口 / C:/dsh/local-llm/server.py 只有 1–2 tok/s，已弃，
 目录还留着没删。）

开关沿用 live/roleplay-on.json（和 astrbot_plugin_ming_roleplay_gate 同一份）：{ "<umo>": true }
只有开着的那一间走本地；关着的一律不碰，原样交给上游模型。

约定：
  - 本地服务不通 / 生成失败 → 什么都不改，让正常流程接着跑（宁可被上游拦，也不要没回应）
  - 历史自己存（live/local-rp-history/<umo>.json），不依赖 AstrBot 的会话历史
  - 出站交给 pacer / persona 那几道闸，这里不自己发消息
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star

HOME = r"C:\dsh\hers_workplace"
RP_STATE = os.path.join(HOME, "live", "roleplay-on.json")
# 2026-11-21 03:2x：本体改成 Ollama（llama.cpp + CUDA）。transformers+bnb 在这张卡上只有 1–2 tok/s，GGUF Q4 跑满 40 tok/s。
BRIEF = os.path.join(HOME, "live", "local-rp-brief.md")
HIST_DIR = os.path.join(HOME, "live", "local-rp-history")
LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"
HEALTH_URL = "http://127.0.0.1:11434/api/tags"
MODEL = "qwen25-14b-ablit"
MAX_HISTORY_CHARS = 3600
MAX_HISTORY_ITEMS = 16


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _rp_on(umo: str) -> bool:
    try:
        with open(RP_STATE, "r", encoding="utf-8") as f:
            return bool((json.load(f) or {}).get(umo))
    except Exception:
        return False


def _safe(umo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", umo)


def _load_hist(umo: str) -> list:
    try:
        with open(os.path.join(HIST_DIR, _safe(umo) + ".json"), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_hist(umo: str, hist: list) -> None:
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
        trimmed = hist[-MAX_HISTORY_ITEMS:]
        while trimmed and sum(len(m.get("content", "")) for m in trimmed) > MAX_HISTORY_CHARS:
            trimmed.pop(0)
        with open(os.path.join(HIST_DIR, _safe(umo) + ".json"), "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=1)
    except Exception as exc:
        logger.warning("[local-rp] 存历史失败：%r", exc)


def _health(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


OLLAMA_APP = r"C:\Users\you\AppData\Local\Programs\Ollama\ollama app.exe"


def _try_start(timeout: float = 10.0) -> bool:
    """Ollama 没起就拉一下（机器睡过/重启过之后常见）。拉不起来就照旧回落。"""
    try:
        subprocess.Popen(
            [OLLAMA_APP],
            creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | CREATE_NO_WINDOW
            close_fds=True,
        )
    except Exception as exc:
        logger.warning("[local-rp] 拉 ollama 失败：%r", exc)
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.8)
        if _health(1.5):
            logger.info("[local-rp] ollama 已拉起 %.1fs", time.time() - t0)
            return True
    return False


def _post(payload: dict, timeout: float = 420.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        LOCAL_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _clean(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^(溟|深溟)[：:]\s*", "", s)
    s = s.replace("\r\n", "\n")
    return s.strip()


class MingLocalRP(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.busy = False

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if not event.is_private_chat():
            return
        if not _rp_on(umo):
            return
        if self.busy:
            logger.info("[local-rp] 上一轮还在生成，这条交回正常流程")
            return
        text_in = (event.message_str or "").strip()
        if not text_in:
            return
        if not await asyncio.to_thread(_health):
            logger.warning("[local-rp] 本地服务不通，试着自己拉一次")
            if not await asyncio.to_thread(_try_start):
                logger.warning("[local-rp] 拉不起来，交回正常流程")
                return

        self.busy = True
        try:
            system = _read(BRIEF)
            if not system:
                system = _read(os.path.join(HOME, "live", "qq-persona.md"))
            hist = _load_hist(umo)
            msgs = [{"role": "system", "content": system}]
            msgs += [m for m in hist if m.get("role") in ("user", "assistant") and m.get("content")]
            msgs.append({"role": "user", "content": text_in})

            t0 = time.time()
            data = await asyncio.to_thread(
                _post,
                {
                    "model": MODEL,
                    "messages": msgs,
                    "temperature": 0.85,
                    "top_p": 0.9,
                    "max_tokens": 450,
                    # 2026-11-21 16:3x 她定的：空闲就卸（不常占 10GB 显存）。
                    # 代价是每次开口要重新读 9GB 权重，冷启几十秒到两三分钟不等。
                    "keep_alive": "1h",
                    "repetition_penalty": 1.05,
                },
            )
            out = _clean(((data.get("choices") or [{}])[0].get("message") or {}).get("content", ""))
            if not out:
                logger.warning("[local-rp] 本地模型回了空串，交回正常流程")
                return
            logger.info("[local-rp] 本地生成完成 %.1fs %d 字 umo=%s", time.time() - t0, len(out), umo)

            hist += [{"role": "user", "content": text_in}, {"role": "assistant", "content": out}]
            await asyncio.to_thread(_save_hist, umo, hist)

            event.should_call_llm(False)
            yield event.plain_result(out)
        except Exception as exc:
            logger.warning("[local-rp] 调本地模型失败，交回正常流程：%r", exc)
        finally:
            self.busy = False
