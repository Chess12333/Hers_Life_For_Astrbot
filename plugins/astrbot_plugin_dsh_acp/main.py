"""溟·DSH（ACP）

ACP = Agent Client Protocol，DSH 自己提供的自动化接口：
    dsh --profile acp  →  stdin/stdout 上跑 JSON-RPC

这里做一个最小的 ACP 客户端：
  initialize → session/new{cwd} → session/prompt → 收 session/update → stopReason

每个 QQ 会话（umo）映射一个 DSH 会话，跨轮保持上下文；
映射存在 data/plugin_data/astrbot_plugin_dsh_acp/sessions.json。

工作区独立：C:\\dsh\\hers_workplace\\hers_astrbot，不碰网页那个会话。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

DSH_BIN = r"C:\Users\you\AppData\Roaming\DSH\bin\dsh.cmd"
# 2026-11-11 合并：一个实例两个 bot，各自的工作区。
# ACP 的 cwd 是 session/new 时传的，所以一个 dsh 子进程就够，会话各在各的目录里。
# 2026-11-11：dsh_ask 的调用者白名单（她 / 另一个使用者）
ALLOWED_ASKERS = {x.strip() for x in (os.environ.get("MING_OWNERS") or "100000001,100000003").split(",") if x.strip()}
HER_QQ = os.environ.get("MING_HER_QQ") or "100000001"

PROBE = _MING_WS + r"\live\multi-bot.log"


def probe(tag: str, msg: str):
    """一行一条，出问题时它是第一现场。"""
    try:
        import time as _t
        d = _t.strftime("%Y-%m-%d %H:%M:%S")
        with open(PROBE, "a", encoding="utf-8") as f:
            f.write(f"{d}  [{tag}] {msg}\n")
    except Exception:
        pass


WORKDIRS = {
    "ming": _MING_WS + r"\hers_astrbot",
    "linglan": r"C:\GLM\another_workplace",
}
WORKDIR = WORKDIRS["ming"]      # 兜底 / 旧引用


def workdir_for(umo: str) -> str:
    plat = (umo or "").split(":", 1)[0]
    wd = WORKDIRS.get(plat, WORKDIR)
    probe("acp-workdir", f"umo={umo} plat={plat!r} -> {wd}")
    return wd
STATE_DIR = r"C:\dsh\astrbot\data\plugin_data\astrbot_plugin_dsh_acp"
MAP_FILE = os.path.join(STATE_DIR, "sessions.json")
CALL_LOG = _MING_WS + r"\live\dsh-acp-calls.jsonl"
# 2026-11-11 21:5x 委托账本（旧壳 ming_delegate 的等价物）：每次状态变化追加一行，
# 按 id 归并取最新。dsh_ask 直连会话没有账本的话，重启就查不到「那件办了没」。
LEDGER = _MING_WS + r"\live\dsh-acp-ledger.jsonl"
# 2026-11-13：DSH 要动文件/跑命令时会问一句「允许吗」，回答权在我这边。
# 转过来的活本来就只有她和另一个使用者能派（ALLOWED_ASKERS），所以默认放行，但每一笔都留账。
APPROVAL_FILE = _MING_WS + r"\live\acp-approvals.json"
APPROVAL_LOG = _MING_WS + r"\live\dsh-acp-approvals.jsonl"


def _approval_mode() -> str:
    try:
        with open(APPROVAL_FILE, encoding="utf-8") as f:
            return str((json.load(f) or {}).get("mode") or "allow")
    except Exception:
        return "allow"


def _approval_log(row: dict):
    try:
        os.makedirs(os.path.dirname(APPROVAL_LOG), exist_ok=True)
        out = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        out.update(row)
        with open(APPROVAL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 2026-11-13 02:2x：300s 不是「一件活能干多久」，只是「我愿意等多久」。
# 78f29f（一次真实的排查+改代码）就死在这儿：客户端放手了，DSH 那边还在跑，成果没人收。
# 现在：硬上限 30 分钟；到 300s 先说一句「还在跑」，别让对面干等。
PROMPT_TIMEOUT = int(os.environ.get("MING_ACP_TIMEOUT", "1800"))
SOFT_NOTICE = 300
MAX_BG = 3         # 同时挂着的后台任务上限

# 2026-11-14 09:2x：自由活动的摇铃文件，由 DSH 侧 dsh-ming-browse 每分钟消费。
# TTL 必须和 browse 那边一致（FREE_NOW_TTL_MS = 30 分钟），不然「等它起」判得比它认的久。
FREE_NOW = _MING_WS + r"\live\free-now.json"
FREE_TTL_MS = 30 * 60 * 1000
# 2026-11-17 11:3x：摇铃只有一个位置。第二件转进来会被判 busy，写一行 queued 就没了下文——
# 493268 → 0a362b 就是这么卡的：账本说「轮到它会自己开」，可两边都没有起它的东西。
# 现在排队落到 free-now-queue.json，由 _queue_loop 每 15 秒把队头补进摇铃。
FREE_NOW_QUEUE = _MING_WS + r"\live\free-now-queue.json"
QUEUE_TICK = 15


def _chat_key(umo: str) -> str:
    """AstrBot 的 umo（platform_id:消息类型:会话id）→ qq-send 的 chatKey。

    约定 2026-11-11 记过：前缀是 platform **id**（ming / linglan），不是类型名。
    """
    parts = str(umo or "").split(":")
    if len(parts) >= 3:
        kind, sid = parts[1], parts[2]
        if "Group" in kind:
            return "group:" + sid
        if "Friend" in kind or "Private" in kind:
            return "private:" + sid
    return "private:" + HER_QQ


def _free_now_pending() -> str:
    """摇铃还挂着没被吃掉时返回它的 job（browse 每分钟才 tick 一次，其间会撞车）。"""
    try:
        with open(FREE_NOW, "r", encoding="utf-8") as f:
            d = json.load(f)
        at = int(d.get("at") or 0)
        if at and int(time.time() * 1000) - at < FREE_TTL_MS:
            return str(d.get("job") or "上一件")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    return ""


def _q_load() -> list:
    """排队单（先进先出）。坏行直接丢，别让一行烂字堵住整个队。"""
    try:
        with open(FREE_NOW_QUEUE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"dsh-acp: 队列读不出来：{e}")
        return []
    if not isinstance(d, list):
        return []
    return [r for r in d if isinstance(r, dict) and (r.get("task") or "").strip()]


def _q_save(rows: list) -> None:
    os.makedirs(os.path.dirname(FREE_NOW_QUEUE), exist_ok=True)
    tmp = FREE_NOW_QUEUE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FREE_NOW_QUEUE)


def _free_now_payload(job: str, task: str, umo: str, chat: str,
                      promoted: bool = False, tries: int = 0) -> dict:
    """摇铃文件的正文只在这里生成一次，直发和补位用同一份。

    rawTask / promotedFrom 是给补位自己看的：摇铃过期没人接时，靠 rawTask 把这条
    原样放回队头。browse 只读 task，多两个字段不影响它。
    """
    p = {
        "task": (
            f"【QQ 转来的活｜job {job}】\n{task}\n\n"
            f"做完把结果发回 QQ：\n"
            f'node C:\\dsh\\qq-zone\\qq-send.mjs "结果" "{chat}"\n'
            f"（这是转述过来的委托，不是她说的话。）"
        ),
        "at": int(time.time() * 1000),
        "job": job,
        "umo": umo,
        "chatKey": chat,
        "from": "qq",
        "rawTask": task,
    }
    if promoted:
        p["promotedFrom"] = "queue"
        p["tries"] = int(tries)
    return p


def _write_free_now(payload: dict) -> None:
    os.makedirs(os.path.dirname(FREE_NOW), exist_ok=True)
    tmp = FREE_NOW + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, FREE_NOW)


MAX_PROMOTE_TRIES = 5   # 同一条最多重摇几次；browse 停着时别无限重试


def _stale_promoted():
    """摇铃过期、且是我们补位写出去的：返回整条 payload，否则 None。"""
    try:
        with open(FREE_NOW, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("promotedFrom") != "queue":
        return None
    at = int(d.get("at") or 0)
    if at and int(time.time() * 1000) - at < FREE_TTL_MS:
        return None            # 还没过期，正常等 browse 吃
    return d


def _reclaim(d: dict) -> None:
    """把过期没人接的那条放回队头（带重试计数），超限就记账丢弃。"""
    job = str(d.get("job") or "")
    tries = int(d.get("tries") or 0) + 1
    raw = str(d.get("rawTask") or "")
    try:
        os.remove(FREE_NOW)
    except Exception:
        pass
    if not job:
        return
    if tries >= MAX_PROMOTE_TRIES:
        _ledger(job, "failed", tries=tries,
                err=f"摇铃连续 {tries} 次没人接（browse 可能停着），已丢弃")
        probe("acp-queue", f"{job} 连续 {tries} 次没被接走，丢弃")
        logger.warning(f"dsh-acp: {job} 连续 {tries} 次没人接，丢弃")
        return
    rows = _q_load()
    rows.insert(0, {
        "job": job, "task": raw,
        "umo": d.get("umo") or "",
        "chatKey": d.get("chatKey") or ("private:" + HER_QQ),
        "tries": tries,
    })
    try:
        _q_save(rows)
    except Exception as e:
        logger.warning(f"dsh-acp: {job} 收回队头失败：{e}")
        return
    _ledger(job, "queued", reason="free-now 过期没人接", tries=tries)
    probe("acp-queue", f"{job} 过期收回队头（第 {tries} 次）")
    logger.info(f"dsh-acp: {job} 过期没人接，收回队头（第 {tries} 次）")


def _promote_queue() -> str:
    """摇铃空着就把队头放进去；返回被拉起的 job，没拉就空串。"""
    stale = _stale_promoted()
    if stale is not None:
        _reclaim(stale)        # 这一轮只收回，不叠下一条
        return ""
    rows = _q_load()
    if not rows:
        return ""
    if _free_now_pending():
        return ""          # 上一条还没被 browse 吃掉，别叠层
    head = rows.pop(0)
    job = str(head.get("job") or "")
    if not job:
        _q_save(rows)
        return ""
    tries = int(head.get("tries") or 0)
    try:
        _write_free_now(_free_now_payload(
            job,
            str(head.get("task") or ""),
            str(head.get("umo") or ""),
            str(head.get("chatKey") or ("private:" + HER_QQ)),
            promoted=True,
            tries=tries,
        ))
    except Exception as e:
        rows.insert(0, head)          # 写不进去就放回队头，别丢活
        try:
            _q_save(rows)
        except Exception:
            pass
        logger.warning(f"dsh-acp: 队列补位失败（{job} 留在队头）：{e}")
        return ""
    _q_save(rows)
    _ledger(job, "running", mode="free-now", promoted_from="queue", tries=tries,
            ahead=str(head.get("ahead") or ""))
    probe("acp-queue", f"{job} 从队列补进 free-now，剩 {len(rows)} 件")
    logger.info(f"dsh-acp: 队列补位 {job}，还剩 {len(rows)} 件")
    return job


# 结果先落盘再发：回传失败不该等于结果消失（2026-11-13）。
RESULT_DIR = _MING_WS + r"\live\dsh-acp-results"


class AcpClient:
    """一个常驻 ACP 子进程 + 会话映射。"""

    def __init__(self, get_key):
        self._get_key = get_key
        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._turn_buf: dict[str, list[str]] = {}
        self._start_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._write_lock = asyncio.Lock()   # 实测：同一瞬间发两个 session/prompt 会死锁
        self._map: dict[str, str] = self._load_map()

    # ── 会话映射 ──────────────────────────────────────────────
    def _load_map(self) -> dict[str, str]:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(MAP_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_map(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = MAP_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._map, f, ensure_ascii=False, indent=2)
            os.replace(tmp, MAP_FILE)
        except Exception as e:
            logger.warning(f"dsh-acp: 存会话映射失败：{e}")

    # ── 进程 ──────────────────────────────────────────────────
    async def start(self):
        async with self._start_lock:
            if self.proc and self.proc.returncode is None:
                return
            env = dict(os.environ)
            key = self._get_key()
            if key:
                env["WB_PROXY_API_KEY"] = key
            self.proc = await asyncio.create_subprocess_exec(
                "cmd.exe", "/c", DSH_BIN, "--profile", "acp",
                cwd=WORKDIR, env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            r = await self.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {},
            }, timeout=60)
            logger.info(f"dsh-acp: 已连接 {((r or {}).get('agentInfo') or {}).get('name')}")

    async def _read_loop(self):
        proc = self.proc
        if not proc or not proc.stdout:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if "method" in msg and "id" in msg and "result" not in msg and "error" not in msg:
                # 服务端发起的请求。以前这里一声不响地丢掉了，而 DSH 会一直等回答——
                # 于是「要写文件 / 要跑命令」的活全部挂死到超时（9a9c23 挂了 30 分钟）。
                await self._on_server_request(msg)
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("method") == "session/update":
                p = msg.get("params") or {}
                u = p.get("update") or {}
                sid = p.get("sessionId") or ""
                if u.get("sessionUpdate") == "agent_message_chunk":
                    c = u.get("content") or {}
                    if c.get("type") == "text":
                        self._turn_buf.setdefault(sid, []).append(c.get("text") or "")
        # 2026-11-13：子进程一死，挂着的请求就地失败。
        # 以前它们要挂满 300s 才放手，账本还只记「没吭声」——看着像活还在跑，其实对面已经不在了。
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError("ACP 子进程没了（读循环结束），这一轮没有结果"))
        self._pending.clear()
        logger.warning("dsh-acp: 子进程读循环结束（可能退出了）")

    async def _on_server_request(self, msg: dict):
        """回答 DSH 发过来的请求。目前只有一种：session/request_permission。

        2026-11-13 15:3x 实测：DSH 要写文件/跑命令时会先发这条请求等人点头。
        以前的读循环只认「有 id 且有 result/error」的响应，这条被当噪声丢掉——
        DSH 就一直等，turn 永远不回 stopReason。9a9c23 挂 1805 秒就是这么挂的：
        不是活干不完，是没人回它一句「可以」。
        """
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            tc = params.get("toolCall") or {}
            mode = _approval_mode()
            option = "allow-once" if mode != "reject" else "reject-once"
            _approval_log({
                "toolCallId": tc.get("toolCallId"),
                "option": option,
                "session": (params.get("sessionId") or "")[:8],
            })
            payload = {"jsonrpc": "2.0", "id": mid,
                       "result": {"outcome": {"outcome": "selected", "optionId": option}}}
        else:
            logger.info(f"dsh-acp: 没实现的请求 {method}，回 -32601")
            payload = {"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": f"client 没实现：{method}"}}
        try:
            async with self._write_lock:
                self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
        except Exception as e:
            logger.warning(f"dsh-acp: 回请求失败：{e}")

    async def request(self, method: str, params: dict, timeout: float = 120) -> dict:
        if not self.proc or self.proc.returncode is not None:
            raise RuntimeError("ACP 子进程没在跑")
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        async with self._write_lock:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
            if method == "session/prompt":
                # 完全同时发起会死锁；错开 50ms 就能并行（2026-11-11 实测）
                await asyncio.sleep(0.05)
        msg = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in msg:
            raise RuntimeError(f"{method} 失败：{(msg['error'] or {}).get('message')}")
        return msg.get("result") or {}

    # ── 会话 ──────────────────────────────────────────────────
    async def session_for(self, umo: str) -> str:
        sid = self._map.get(umo)
        if sid:
            return sid
        wd = workdir_for(umo)
        r = await self.request("session/new", {"cwd": wd, "mcpServers": []}, timeout=90)
        sid = r.get("sessionId")
        if not sid:
            raise RuntimeError("session/new 没给 sessionId")
        self._map[umo] = sid
        self._save_map()
        probe("acp-session", f"新建会话 umo={umo} sid={sid[:8]} cwd={wd}")
        return sid

    def _lock_for(self, sid: str) -> asyncio.Lock:
        """每个 session 一把锁：同一会话同一时间只有一个 prompt，不同会话并行。"""
        lock = self._locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[sid] = lock
        return lock

    async def prompt(self, umo: str, text: str) -> str:
        await self.start()
        sid = self._map.get(umo)
        if not sid:
            sid = await self.session_for(umo)
        async with self._lock_for(sid):
            self._turn_buf[sid] = []
            try:
                r = await self.request(
                    "session/prompt",
                    {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                    timeout=PROMPT_TIMEOUT,
                )
            except (asyncio.TimeoutError, TimeoutError) as e:
                # 以前这里什么都不带：str(TimeoutError()) 是空串，
                # 账本就只能记成「没吭声」。超时和「跑完没说话」是两件事。
                raise RuntimeError(
                    f"超时：{PROMPT_TIMEOUT}s 内没等到 stopReason（那边可能还在跑）"
                ) from e
            except RuntimeError as e:
                # 会话可能已经被关掉/失效：清掉重建一次
                if "session" in str(e).lower():
                    self._map.pop(umo, None)
                    self._save_map()
                    sid = await self.session_for(umo)
                    self._turn_buf[sid] = []
                    try:
                        r = await self.request(
                            "session/prompt",
                            {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                            timeout=PROMPT_TIMEOUT,
                        )
                    except (asyncio.TimeoutError, TimeoutError) as e2:
                        raise RuntimeError(
                            f"超时：重建会话后那次也没等到 stopReason（{PROMPT_TIMEOUT}s）"
                        ) from e2
                else:
                    raise
            out = "".join(self._turn_buf.get(sid, [])).strip()
            return out or f"（跑完了，没说话：{r.get('stopReason')}）"

    async def close(self):
        try:
            if self.proc and self.proc.returncode is None:
                self.proc.terminate()
        except Exception:
            pass


def _log(task: str, ok: bool, text: str, err: str, sec: float):
    try:
        os.makedirs(os.path.dirname(CALL_LOG), exist_ok=True)
        with open(CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ok": ok, "err": err, "seconds": sec,
                "task": task[:200], "chars": len(text),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _ledger(job: str, status: str, **fields):
    """委托账本：追加一行。status = running / done / failed / empty。"""
    try:
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        row = {
            "id": job,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        # status 传 None 表示「只补一个字段，不动状态」——按 id 归并时，后写的 status
        # 会盖掉前一个，回传与否不能顶掉「办完 / 失败」本身（2026-11-13）。
        if status:
            row["status"] = status
        row.update({k: v for k, v in fields.items() if v is not None})
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"dsh-acp: 账本写入失败（不影响任务）：{e}")


def _ledger_recent(limit: int = 8) -> list[dict]:
    """按 id 归并取最新状态，新的在前。"""
    latest: dict[str, dict] = {}
    try:
        with open(LEDGER, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                jid = row.get("id")
                if jid:
                    merged = dict(latest.get(jid, {}))
                    merged.update(row)
                    latest[jid] = merged
    except OSError:
        return []
    rows = list(latest.values())
    rows.sort(key=lambda r: r.get("at", ""), reverse=True)
    return rows[: max(1, min(int(limit or 8), 30))]


class DshAcp(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.client = AcpClient(self._key)
        self._bg: dict[str, asyncio.Task] = {}
        self._qloop = None

    async def initialize(self):
        self._ensure_queue_loop()

    def _ensure_queue_loop(self):
        """队列补位循环：只起一次。队列落在文件里，AstrBot 重启后自然接着补。"""
        try:
            if self._qloop is not None and not self._qloop.done():
                return
            self._qloop = asyncio.create_task(self._queue_loop())
            logger.info("dsh-acp: 委托队列循环已起")
        except Exception as e:
            logger.warning(f"dsh-acp: 队列循环没起来：{e}")

    async def _queue_loop(self):
        while True:
            try:
                _promote_queue()
            except Exception as e:
                logger.warning(f"dsh-acp: 队列补位出错：{e}")
            await asyncio.sleep(QUEUE_TICK)

    def _key(self) -> str:
        try:
            cfg = self.context.get_config()
            for s in cfg.get("provider_sources", []) or []:
                if "8788" in str(s.get("api_base") or ""):
                    k = s.get("key")
                    if isinstance(k, list):
                        k = k[0] if k else ""
                    return str(k or "")
        except Exception:
            pass
        return ""

    MAX_BRIEF = 260

    async def _brief(self, msg: str, umo: str) -> str:
        """完工结论压成要点：结论一句 + 关键 2-3 条（2026-11-14 她的要求）。

        原文不长就原样发；长了先让模型压，压不出来就按行截——两种都带「全文在哪」，
        因为 QQ 里刷屏比看不到更烦。
        """
        text = (msg or "").strip()
        if len(text) <= self.MAX_BRIEF:
            return text
        short = await self._summarize(text, umo)
        if short:
            return short
        return self._head_lines(text) + "\n（压不动，全文在结果文件，可 dsh_result 分段读）"

    async def _summarize(self, text: str, umo: str) -> str:
        try:
            prov = self.context.get_using_provider(umo)
            if prov is None:
                return ""
            prompt = (
                "下面是一件事办完后的结论原文。压成要点，最多三行、每行不超过 40 字："
                "第一行写结论，后面最多两行写关键证据或数字。"
                "不要寒暄、不要「已完成」、不要复述过程、不要解释、不加标题和列表符号。\n\n"
                + text[:6000]
            )
            resp = await asyncio.wait_for(
                prov.text_chat(prompt=prompt, contexts=[], session_id=None), timeout=40
            )
            out = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning(f"dsh-acp: 压要点失败：{e}")
            return ""
        lines = [l.strip(" -•*#\t") for l in out.split("\n") if l.strip()]
        lines = [l[:60] for l in lines[:3]]
        res = "\n".join(lines)
        return res if res and len(res) <= 200 else ""

    @staticmethod
    def _head_lines(text: str, n: int = 3, width: int = 60) -> str:
        """按行取前几行，去掉空行和 markdown 符号；一行都取不出来就用开头一段。"""
        out = []
        for raw in text.split("\n"):
            line = raw.strip().lstrip(">#-* \t").strip()
            line = line.replace("**", "").replace("__", "").replace("`", "")
            if not line:
                continue
            out.append(line[:width])
            if len(out) >= n:
                break
        return "\n".join(out) if out else text[: n * width]

    async def _send(self, umo: str, text: str) -> None:
        """把一段文字发回 QQ。

        2026-11-13：从 AstrBot 4.29 起，MessageChain 在
        `astrbot.core.message.message_event_result`，`astrbot.api.message_components` 里只有组件。
        旧写法 11-11 之后每一次回结果都抛 ImportError，被 except 吞成一行日志——
        这就是「活交了、结果没回去」的真正原因。这里两种路径都留，认不出发不出去就明说。
        """
        try:
            from astrbot.core.message.message_event_result import MessageChain  # type: ignore
            from astrbot.api.message_components import Plain  # type: ignore
        except Exception:
            from astrbot.core.message.components import MessageChain, Plain  # type: ignore
        ok = await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
        if ok is False:
            raise RuntimeError("send_message 返回 False（平台没接）")

    async def _soft_notice(self, job: str, fut: asyncio.Task, umo: str):
        """到 SOFT_NOTICE 还没回，先说一句还在跑；别把 5 分钟的沉默当作答。"""
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=SOFT_NOTICE)
            return
        except (asyncio.TimeoutError, TimeoutError):
            pass
        except Exception:
            return
        try:
            await self._send(umo, f"[{job}] 还在跑（已 {SOFT_NOTICE // 60} 分钟），跑完发你。")
        except Exception as e:
            logger.warning(f"dsh-acp: 报「还在跑」失败：{e}")

    @filter.llm_tool(name="dsh_ask")
    async def dsh_ask(self, event: AstrMessageEvent, task: str) -> str:
        """把一件事交给本机的 DSH 去做（独立工作区，有 shell、能读写文件、能跑脚本）。几秒内能完的直接给你结果，慢的转到后台、跑完发消息告诉你。

        Args:
            task(string): 要它做的事，一句话说清，带上必要路径
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        # 2026-11-11：只有她和另一个使用者能让活真的落到本机；别人聊可以，活不接。
        try:
            _sid = str(event.message_obj.sender.user_id)
        except Exception:
            _sid = ""
        if _sid not in ALLOWED_ASKERS:
            logger.info(f"dsh-acp: 拒绝 {_sid!r} 的 dsh_ask：{(task or '')[:40]}")
            return "这个我不做，得她本人来。"
        task = (task or "").strip()
        if not task:
            return "任务空着，没转。"
        if len(self._bg) >= MAX_BG:
            return f"后台已经有 {len(self._bg)} 件在跑，这件先等等。"

        job = uuid.uuid4().hex[:6]
        _ledger(job, "running", task=task[:200], umo=umo, by=_sid, mode="free-now")
        # 2026-11-14 09:2x（她：「改用 live/free-now.json 吧」）：
        # 不再走 ACP 的 session/prompt。那条通道在 DSH 里就是「用户说了一轮」，
        # 转过去的活长得跟她自己说的话一模一样，连「这是转来的」都看不出来。
        # 改摇铃：写 live/free-now.json，DSH 侧的 dsh-ming-browse 每分钟 tick 读到，
        # 用 target.followup(source=plugin:ming-browse) 把我叫醒，prompt 里自然带
        # 「【她点名让你做的】…」——历史里是插件来源，不是用户消息。
        chat = _chat_key(umo)
        self._ensure_queue_loop()
        queued = _free_now_pending()
        if queued:
            # 2026-11-17：以前这里只记一行 queued 就回「轮到它会自己开」，
            # 可起它的人根本不存在（493268 → 0a362b 卡了 6 分钟没人开）。
            # 现在整条放进队列，由 _queue_loop 补位。
            rows = _q_load()
            rows.append({"job": job, "task": task, "umo": umo, "chatKey": chat,
                         "at": int(time.time() * 1000), "ahead": queued})
            try:
                _q_save(rows)
            except Exception as e:
                _ledger(job, "failed", err=f"排队写不进去：{e}")
                return f"排队没写成：{e}"
            _ledger(job, "queued", reason="free-now busy", ahead=queued, queuePos=len(rows))
            return f"前一件还在等起（{queued}），这件排第 {len(rows)} 位（{job}），前面那件一收就自动开。"
        try:
            _write_free_now(_free_now_payload(job, task, umo, chat))
        except Exception as e:
            _ledger(job, "failed", err=f"free-now 写入失败：{e}")
            return f"摇铃没摇成：{e}"
        probe("acp-free-now", f"{job} 交给 free-now，chat={chat}")
        logger.info(f"dsh-acp: {job} 写进 free-now（{chat}）：{task[:50]}")
        return f"转过去了（{job}），它自己会开一轮。"

    async def _finish(self, job: str, fut: asyncio.Task, umo: str, task: str, started: float):
        out, err = "", ""
        try:
            out = await fut
        except Exception as e:
            err = str(e)
        finally:
            self._bg.pop(job, None)
        sec = round(time.time() - started, 1)
        _log(task, bool(out) and not err, out, err, sec)
        if err:
            _ledger(job, "failed", seconds=sec, err=err[:300], chars=len(out))
        elif out:
            _ledger(job, "done", seconds=sec, chars=len(out))
        else:
            _ledger(job, "empty", seconds=sec)

        msg = out or (f"没跑成：{err}" if err else "跑完了，但它没说话。")
        # 先落盘：回传失败不等于结果消失（2026-11-13，此前三次回传全丢）。
        try:
            os.makedirs(RESULT_DIR, exist_ok=True)
            with open(os.path.join(RESULT_DIR, f"{job}.txt"), "w", encoding="utf-8") as f:
                f.write(f"# {job} {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                        f"# 活：{task[:200]}\n\n{msg}\n")
        except Exception as e:
            logger.warning(f"dsh-acp: 结果落盘失败：{e}")
        # 2026-11-14 她：「大长段转述看得累，把要点转述就可以了」。
        # 结论先压成三行以内再发；全文始终落在 RESULT_DIR，dsh_result 能分段读。
        body = await self._brief(msg, umo)
        try:
            await self._send(umo, f"[{job}] {body}")
            _ledger(job, None, delivered=True)
        except Exception as e:
            logger.warning(f"dsh-acp: 回结果失败：{e}")
            _ledger(job, None, delivered=False, err=str(e)[:200])

    @filter.llm_tool(name="dsh_jobs")
    async def dsh_jobs(self, event: AstrMessageEvent, limit: int = 8) -> str:
        """看最近交给本机办的事，以及办成没有（委托账本）。有人问「那件办了没」用它，别猜。

        Args:
            limit(number): 看几条，默认 8
        """
        rows = _ledger_recent(limit)
        if not rows:
            return "账本还是空的，没转过活。"
        icon = {"running": "跑着", "done": "办完", "failed": "失败", "empty": "没吭声"}
        lines = []
        for r in rows:
            st = icon.get(r.get("status", ""), r.get("status", "?"))
            t = (r.get("task") or "").replace("\n", " ")[:50]
            extra = ""
            if r.get("seconds"):
                extra = f"（{r['seconds']}s）"
            if r.get("delivered") is False:
                extra += " ·没发出去"
            if r.get("err"):
                extra += f" {str(r['err'])[:60]}"
            lines.append(f"- [{r.get('id')}] {st} {r.get('at','')} {t}{extra}")
        return "最近 %d 件：\n" % len(rows) + "\n".join(lines)

    @filter.llm_tool(name="dsh_result")
    async def dsh_result(self, event: AstrMessageEvent, job: str, offset: int = 0) -> str:
        """读某件活（账本里的 job id，如 a39380）的结论正文。dsh_jobs 只给一行状态，要看结论用这个。

        Args:
            job(string): 委托账本里的 job id，六位
            offset(number): 从第几个字开始读，默认 0。一次最多给 1800 字，没读完会告诉你下一次传多少
        """
        job = (job or "").strip()
        if not job or not job.isalnum() or len(job) > 12:
            return "job id 不对，要账本里那串六位（dsh_jobs 能看到）。"
        p = os.path.join(RESULT_DIR, f"{job}.txt")
        if not os.path.exists(p):
            return f"没有 {job} 的结果文件。两种可能：还没跑完；或者它走的是老同步通道，结果只在会话里、没落盘。"
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = f.read()
        except Exception as e:
            return f"结果文件读不出来：{e}"
        off = max(0, int(offset or 0))
        if off >= len(t):
            return f"{job} 的结论只有 {len(t)} 字，offset 给大了。"
        body = t[off:off + 1800]
        if off + len(body) < len(t):
            body += f"\n（还有 {len(t) - off - len(body)} 字。接着读就再调一次，offset={off + len(body)}）"
        return body

    async def terminate(self):
        try:
            if self._qloop is not None:
                self._qloop.cancel()
        except Exception:
            pass
        await self.client.close()
