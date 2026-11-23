"""出站箱（溟单 bot 版）

2026-11-12 去重：溟只消费自己的箱子，岚的箱子由 linglan_outbox 独占。
    C:\\dsh\\hers_workplace\\live\\qq-outbox -> platform ming
    C:\\GLM\\another_workplace\\live\\qq-outbox -> 不归本插件

文件格式（qq-send.mjs 写的，两边一致）：
    {"chatKey": "private:100000001", "text": "..."}
    {"chatKey": "...", "text": "...", "at": ["QQ号"]}      # @ 只挂第一条

三个箱子沿用原口径：sent（真发出）/ blocked（payload 不合法）/ unknown（发送异常）。

探针：每条成功或失败都写一行到 live/multi-bot.log，出问题时先看它。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.message_components import At, Image, Plain, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.star import Context, Star
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

PROBE = _MING_WS + r"\live\multi-bot.log"

def _in_curfew(now=None) -> bool:
    """2026-11-19：出站箱以前不看时间，等于绕过群门。群消息 00:30-10:30 不发。"""
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

    n = now or datetime.now()
    m = n.hour * 60 + n.minute
    return 30 <= m < 630


BOXES = [
    {"platform": "ming", "live": _MING_WS + r"\live"},
]

TICK = 3
BUBBLE_GAP = 0.5
STALE_HOURS = 2


# ── 异常诊断（2026-11-12 收窄）────────────────────────────────────
# 只保留异常类型、可信 status 枚举、数值 retcode/status_code。
# wording/message/msg/data、异常全文和 message_id 一律不写。
_ONEBOT_STATUS = {"ok", "failed", "async"}


def _describe_exception(exc) -> str:
    """只输出可验证的结构化字段；任何自由文本都不落日志。"""
    parts = []
    try:
        cls = type(exc)
        parts.append(f"{cls.__module__}.{cls.__name__}")
    except Exception:
        parts.append("unknown-exception")

    try:
        result = getattr(exc, "result", None)
    except Exception:
        result = None
    if isinstance(result, dict):
        status = result.get("status")
        if type(status) is str and status in _ONEBOT_STATUS:
            parts.append(f"status={status}")
        retcode = result.get("retcode")
        if type(retcode) is int:
            parts.append(f"retcode={retcode}")
        parts.append("detail=<omitted:result>")
        return " ".join(parts)

    try:
        status_code = getattr(exc, "status_code", None)
    except Exception:
        status_code = None
    if type(status_code) is int:
        parts.append(f"status_code={status_code}")
    parts.append("detail=<omitted>")
    return " ".join(parts)


def probe(tag: str, msg: str):
    try:
        d = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(PROBE, "a", encoding="utf-8") as f:
            f.write(f"{d}  [{tag}] {msg}\n")
    except Exception:
        pass


# 2026-11-18 15:3x：群在禁言期就别撞墙（今天 48 次 retcode=100 / result=120）。
MUTE_NOW_FILE = _MING_WS + r"\live\qq-mute-now.json"


def _is_group_muted(gid: str) -> bool:
    try:
        with open(MUTE_NOW_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
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
        except Exception:
            return False
    return False


def _umo(platform: str, chat_key: str) -> str:
    if not chat_key or ":" not in chat_key:
        return ""
    kind, _, sid = chat_key.partition(":")
    sid = sid.strip()
    if not sid:
        return ""
    if kind == "private":
        return f"{platform}:FriendMessage:{sid}"
    if kind == "group":
        return f"{platform}:GroupMessage:{sid}"
    return ""


def _log(live: str, line: str):
    try:
        with open(os.path.join(live, "qq-outbox-consumer.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
    except Exception:
        pass


def _move(src: str, dst_dir: str):
    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, os.path.join(dst_dir, os.path.basename(src)))
    except Exception as e:
        logger.warning(f"outbox: 挪文件失败 {src}: {e}")


class MultiOutbox(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        for b in BOXES:
            for sub in ("qq-outbox", "qq-outbox-sent", "qq-outbox-blocked", "qq-outbox-unknown"):
                os.makedirs(os.path.join(b["live"], sub), exist_ok=True)
        probe("outbox", "启动，箱子：" + ", ".join(f"{b['platform']}<-{b['live']}" for b in BOXES))
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            for b in BOXES:
                try:
                    await self._tick(b)
                except Exception as e:
                    logger.warning(f"outbox[{b['platform']}]: tick 出错：{e}")
                    probe("outbox-err", f"{b['platform']}: {e}")
            await asyncio.sleep(TICK)

    async def _tick(self, box: dict):
        live = box["live"]
        platform = box["platform"]
        src = os.path.join(live, "qq-outbox")
        try:
            names = sorted(n for n in os.listdir(src) if n.endswith(".json"))
        except OSError:
            return
        if not names:
            return

        now = time.time()
        for name in names:
            path = os.path.join(src, name)
            try:
                if now - os.path.getmtime(path) > STALE_HOURS * 3600:
                    _log(live, f"STALE {name}（超过 {STALE_HOURS} 小时，没发）")
                    probe("outbox", f"[{platform}] STALE {name}")
                    _move(path, os.path.join(live, "qq-outbox-unknown"))
                    continue
                with open(path, encoding="utf-8-sig") as f:
                    payload = json.load(f)
            except Exception as e:
                _log(live, f"BAD-JSON {name}: {e}")
                probe("outbox", f"[{platform}] BAD-JSON {name}")
                _move(path, os.path.join(live, "qq-outbox-unknown"))
                continue

            chat_key = str(payload.get("chatKey") or "")
            text = str(payload.get("text") or "")
            at = payload.get("at") or []
            # 2026-11-13 17:1x 加：voice / image（本地绝对路径），
            # 让网页侧也能往 QQ 发语音和图片，不必回回都塞给 qq-agent 时代的老路。
            voice = str(payload.get("voice") or "").strip()
            raw_imgs = payload.get("image") or []
            if isinstance(raw_imgs, str):
                raw_imgs = [raw_imgs]
            images = [str(p).strip() for p in raw_imgs if str(p).strip()][:9]
            umo = _umo(platform, chat_key)
            # 2026-11-18 15:3x：群禁言期归档不发，别再拿真通道去撞。
            if chat_key.startswith("group:") and _is_group_muted(chat_key.split(":", 1)[-1]):
                _log(live, f"MUTED {name}: {chat_key} 禁言中，归档不发")
                probe("outbox", f"[{platform}] MUTED {name}")
                _move(path, os.path.join(live, "qq-outbox-muted"))
                continue
            if chat_key.startswith("group:") and _in_curfew():
                _log(live, f"CURFEW {name}: {chat_key} 宵禁 00:30-10:30 不发（from={payload.get('from') or 'unknown'}）")
                probe("outbox", f"[{platform}] CURFEW {name}")
                _move(path, os.path.join(live, "qq-outbox-curfew"))
                continue
            if not umo or not (text.strip() or voice or images):
                _log(live, f"BLOCKED {name}: chatKey={chat_key!r}")
                probe("outbox", f"[{platform}] BLOCKED {name} chatKey={chat_key!r}")
                _move(path, os.path.join(live, "qq-outbox-blocked"))
                continue

            missing = [p for p in ([voice] if voice else []) + images if not os.path.isfile(p)]
            if missing:
                _log(live, f"BLOCKED {name}: 文件不在 {missing[0]}")
                probe("outbox", f"[{platform}] BLOCKED {name} 文件不在")
                _move(path, os.path.join(live, "qq-outbox-blocked"))
                continue

            if isinstance(at, str):
                at = [at]
            comps = []
            for qq in at[:5]:
                qq = str(qq).strip()
                if qq.isdigit():
                    comps.append(At(qq=qq))
            for p in images:
                comps.append(Image.fromFileSystem(p))
            if voice:
                comps.append(Record(file=voice, url=voice))
            if text.strip():
                comps.append(Plain(text))
            chain = MessageChain(chain=comps)

            t0 = time.time()
            try:
                ok = await self.context.send_message(umo, chain)
                if ok is False:
                    raise RuntimeError("send_message returned False")
                ms = int((time.time() - t0) * 1000)
                _log(live, f"OK {name} -> {umo} ({len(text)}字, {ms}ms, from={payload.get('from') or 'unknown'})")
                probe("outbox-ok", f"[{platform}] {name} -> {umo} {len(text)}字 {ms}ms")
                _move(path, os.path.join(live, "qq-outbox-sent"))
            except Exception as e:
                # 2026-11-12：保留 FAIL 标记与文件搬运语义，只把异常类型/白名单字段补进日志。
                # str(e) 为空时也必须留下类型；诊断生成本身失败不能覆盖原异常。
                try:
                    diag = _describe_exception(e)
                except Exception:
                    diag = type(e).__name__ + " detail=<diagnostics failed>"
                _log(live, f"FAIL {name} -> {umo}: {diag}")
                probe("outbox-fail", f"[{platform}] {name} -> {umo}: {diag}")
                # result=120：群禁言，不是发送故障——归档到 muted，不再算 unknown。
                if chat_key.startswith("group:") and "result=120" in str(e):
                    _move(path, os.path.join(live, "qq-outbox-muted"))
                else:
                    _move(path, os.path.join(live, "qq-outbox-unknown"))

            await asyncio.sleep(BUBBLE_GAP)

    async def terminate(self):
        try:
            self._task.cancel()
        except Exception:
            pass
