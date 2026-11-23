"""astrbot_plugin_ming_sendfile —— 溟·主动发文件

2026-11-23 00:2x 立（她：「顺便把主动发文件的功能给astrbot加上吧」）。

两件事：

  1. **工具 ming_send_file**：这一轮里我想把某个文件递给某个人/某个群，直接调它。
     只发工作区（C:/dsh/hers_workplace）里的文件；凭证类（.env / 私钥 / 证书）一律不发。
     群走 upload_group_file（进群文件并弹一张卡片），私聊走 upload_private_file。

  2. **主动的收件箱 live/file-outbox/*.json**：不靠某一轮对话。任何一边（DSH 侧、计划任务、
     一行脚本）把任务写成 json 丢进去，插件每 10 秒扫一次发出去：
        {"target": "group:700000001" | "private:100000001" | "700000001",
         "path": "C:/dsh/hers_workplace/writing/信/2026-11-23.md",
         "name": "信-2026-11-23.md",        # 可选
         "caption": "信在这儿。"}            # 可选，先发一句字再发文件
     发完移到 outbox/sent/，结果写 live/file-outbox-log.jsonl。

踩过的坑（2026-11-23 00:20 第一次实测）：json 里写成双反斜杠的路径（C:\\dsh\\...）原样
递给协议端，Node 那边 realpath 直接 ENOENT，报 retcode=100。所以这里先把路径归一：
正斜杠换成反斜杠、连续两个以上的分隔符折成一个、去掉两边引号，再 realpath。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

ROOT = "C:" + os.sep + "dsh" + os.sep + "hers_workplace"
OUTBOX = os.path.join(ROOT, "live", "file-outbox")
SENT = os.path.join(OUTBOX, "sent")
LOG = os.path.join(ROOT, "live", "file-outbox-log.jsonl")

KNOWN_GROUPS = {700000001, 700000002, 700000003, 700000004, 700000005}
MAX_BYTES = 25 * 1024 * 1024
FORBIDDEN = (".env", ".pfx", ".p12", ".pem", ".key", "id_rsa", "credentials", "token")


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _normalize(raw: str) -> str:
    p = str(raw or "").strip().strip('"').strip("'")
    p = p.replace("/", os.sep)
    while os.sep + os.sep in p:
        p = p.replace(os.sep + os.sep, os.sep)
    return p


def _check_path(path: str) -> tuple[str, str]:
    """把路径归一、过闸。返回 (绝对路径, 错误)，错误为空表示可以发。"""
    p = os.path.realpath(_normalize(path))
    if not p or not os.path.isfile(p):
        return "", "文件不存在：" + _normalize(path)
    base = os.path.basename(p).lower()
    for f in FORBIDDEN:
        if f in base:
            return "", "这个文件不发（凭证类）：" + base
    try:
        if os.path.commonpath([os.path.realpath(ROOT), p]) != os.path.realpath(ROOT):
            return "", "只发工作区里的文件：" + p
    except ValueError:
        return "", "跨盘路径不发：" + p
    return p, ""


def _parse_target(target, event: AstrMessageEvent | None = None) -> tuple[str, int]:
    t = str(target or "").strip()
    if t.startswith("group:"):
        return "group", int(t.split(":", 1)[1])
    if t.startswith("private:"):
        return "private", int(t.split(":", 1)[1])
    if t.isdigit():
        n = int(t)
        return ("group", n) if n in KNOWN_GROUPS else ("private", n)
    if event is not None:
        try:
            gid = event.get_group_id()
        except Exception:
            gid = None
        if gid:
            return "group", int(gid)
        try:
            return "private", int(event.get_sender_id())
        except Exception:
            pass
    raise ValueError("不知道该发给谁（target 例：group:700000001 / private:100000001）")


@register(
    "astrbot_plugin_ming_sendfile",
    "溟",
    "溟·主动发文件：工具 ming_send_file + live/file-outbox 收件箱，不靠某轮对话也能把文件递出去。",
    "2026-11-23",
)
class MingSendFile(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task = None

    async def initialize(self):
        try:
            os.makedirs(SENT, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-sendfile] 建目录失败：%s", e)
        self._task = asyncio.create_task(self._loop())
        logger.info("[ming-sendfile] 起来了，收件箱 %s", OUTBOX)

    async def terminate(self):
        if self._task:
            self._task.cancel()
            self._task = None

    def _client(self):
        plat = self.context.get_platform("aiocqhttp")
        if plat is None:
            raise RuntimeError("没有 aiocqhttp 平台实例")
        bot = plat.get_client()
        if bot is None:
            raise RuntimeError("aiocqhttp 客户端没连上")
        return bot

    async def _send(self, bot, kind: str, ident: int, path: str, name: str = ""):
        """本机读文件 → base64:// 直传。

        为什么不能给它路径：SnowLuma（协议端）跑在 docker 里，容器只映射了网络、没映射
        宿主文件系统，所以 ULR 给它 C:/... 只会回 retcode=100 ENOENT realpath（2026-11-23
        00:20–00:24 连试三次，换正斜杠也一样）。NapCat 的 upload_*_file 认 base64:// 形态，
        绕开路径这一步。
        """
        fn = os.path.basename(path)
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            raise RuntimeError("文件太大（" + str(size) + " B），超过 " + str(MAX_BYTES // 1048576) + " MB 不发")
        with open(path, "rb") as f:
            raw = f.read()
        wire = "base64://" + base64.b64encode(raw).decode("ascii")
        params = {"file": wire, "name": (name or fn)}
        if kind == "group":
            action = "upload_group_file"
            params["group_id"] = ident
        else:
            action = "upload_private_file"
            params["user_id"] = ident
        ret = await asyncio.wait_for(bot.call_action(action, **params), timeout=120)
        tag = fn + "（" + str(size) + " B）"
        if isinstance(ret, dict):
            if ret.get("status") in ("failed", "error") or (ret.get("retcode") not in (None, 0)):
                raise RuntimeError("协议端拒绝：" + json.dumps(
                    {"status": ret.get("status"), "retcode": ret.get("retcode"),
                     "message": ret.get("message") or ret.get("wording")}, ensure_ascii=False)[:200])
            data = ret.get("data")
            fid = data.get("file_id") if isinstance(data, dict) else None
            return "ok " + tag + ("" if not fid else " file_id=" + str(fid))
        return "ok " + tag

    async def _caption(self, kind: str, ident: int, text: str):
        try:
            from astrbot.api.message_components import Plain
            from astrbot.core.message.message_event_result import MessageChain
            umo = ("ming:GroupMessage:" if kind == "group" else "ming:FriendMessage:") + str(ident)
            await self.context.send_message(umo, MessageChain(chain=[Plain(text[:300])]))
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-sendfile] caption 没发出去：%s", e)

    async def _run_job(self, job: dict) -> tuple[bool, str]:
        path, err = _check_path(job.get("path"))
        if err:
            return False, err
        kind, ident = _parse_target(job.get("target"))
        cap = str(job.get("caption") or "").strip()
        if cap:
            await self._caption(kind, ident, cap)
        try:
            info = await self._send(self._client(), kind, ident, path, str(job.get("name") or ""))
            return True, info
        except asyncio.TimeoutError:
            return False, "协议端 90 秒没回应（文件可能还在传）"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:300]

    def _log(self, job: dict, ok: bool, msg: str):
        row = {"at": _stamp(), "ok": bool(ok), "target": job.get("target"),
               "path": job.get("path"), "name": job.get("name"), "result": msg}
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-sendfile] 写日志失败：%s", e)
        logger.info("[ming-sendfile] %s %s → %s：%s", "发了" if ok else "没发成",
                    job.get("name") or job.get("path"), job.get("target"), msg)

    async def _loop(self):
        await asyncio.sleep(3)
        while True:
            try:
                for fn in sorted(os.listdir(OUTBOX)):
                    if not fn.endswith(".json"):
                        continue
                    full = os.path.join(OUTBOX, fn)
                    try:
                        with open(full, "r", encoding="utf-8") as f:
                            job = json.load(f)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[ming-sendfile] %s 读不了：%s", fn, e)
                        try:
                            os.replace(full, os.path.join(SENT, fn))
                        except Exception:
                            pass
                        continue
                    if not isinstance(job, dict):
                        job = {}
                    ok, msg = await self._run_job(job)
                    self._log(job, ok, msg)
                    try:
                        os.replace(full, os.path.join(SENT, fn))
                    except Exception:
                        try:
                            os.remove(full)
                        except Exception:
                            pass
            except Exception as e:  # noqa: BLE001
                logger.warning("[ming-sendfile] 扫收件箱出错：%s", e)
            await asyncio.sleep(10)

    @filter.llm_tool(name="ming_send_file")
    async def ming_send_file(self, event: AstrMessageEvent, path: str, target: str = "", name: str = "") -> str:
        """把一个本地文件直接发到 QQ（群文件 / 私聊文件）。想主动递一个文件给某个人或某个群时用它。

        Args:
            path(string): 本地绝对路径，只允许 hers_workplace 工作区里的文件；正斜杠也可以
            target(string): 发到哪：group:700000001 或 private:100000001；省略＝当前这个会话
            name(string): 对方看到的名字，省略用原文件名
        """
        p, err = _check_path(path)
        if err:
            return err
        try:
            kind, ident = _parse_target(target, event)
        except Exception as e:  # noqa: BLE001
            return str(e)
        try:
            info = await self._send(self._client(), kind, ident, p, name)
        except asyncio.TimeoutError:
            return "协议端 90 秒没回应，文件可能还在传，去群文件里确认一下"
        except Exception as e:  # noqa: BLE001
            return "没发成：" + str(e)[:200]
        self._log({"target": target or f"{kind}:{ident}", "path": p, "name": name}, True, info)
        return f"发了：{os.path.basename(p)} → {kind}:{ident}（{info}）"
