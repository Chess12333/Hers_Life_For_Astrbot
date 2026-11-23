"""astrbot_plugin_obs_export —— 把 AstrBot 这边的对话炼成「观察」，喂给星图。

2026-11-12 22:2x 她定的：「将 qqagent 的内容补录，然后改为 astrbot 适配」。

背景：星图的 QQ 侧原先读 QQ-Agent 的 observations.jsonl（它的记忆插件把话炼成
「TA 爱用白灼、换缸这类比喻接梗吐槽」这种判断）。QQ-Agent 11-11 退役，那条线
停在 11-11 11:46。AstrBot 的库里只有话、没有「谁说的」（conversations 表里
一行行是 `[22:08:35] [图片]`，发送者早被裁掉），所以这里补两件事：

  ① 消息归档：每条群/私聊消息落一行带 QQ 号的话 → live/astrbot-messages.jsonl
  ② 定期提炼：把新话按人喂给模型，产出与旧格式同构的 obs 行
     → live/observations-astrbot.jsonl；再与 QQ-Agent 旧库合并成
     live/observations-all.jsonl（星图改读这一份，旧的内容一条不丢）

只读消息、只写这三个文件。不发消息、不改别人、不碰日记和群友笔记。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"
# 2026-11-23：平台 id 和名字不进代码。AstrBot 里这个平台叫什么就设 MING_PLATFORM_ID（默认 ming）。
PLATFORM_ID = os.environ.get("MING_PLATFORM_ID", "ming")
SELF_NAME = os.environ.get("MING_SELF_NAME", "溟")

ROOT = _MING_WS
LIVE = os.path.join(ROOT, "live")
MSGS = os.path.join(LIVE, "astrbot-messages.jsonl")
OBS = os.path.join(LIVE, "observations-astrbot.jsonl")
MERGED = os.path.join(LIVE, "observations-all.jsonl")
STATE = os.path.join(LIVE, "obs-export-state.json")
# 2026-11-23：旧壳（QQ-Agent）的观察库不是每台机器都有，别写死一条永远读不到的路径。
# 要合并就把路径填进环境变量 MING_OLD_OBS；没有就是空串，下面 os.path.exists('') 直接跳过。
OLD_OBS = os.environ.get("MING_OLD_OBS") or ""

EVERY_SEC = 600          # 每 10 分钟看一次有没有新话
MIN_NEW = 15             # 少于这么多条新话就攒着
MAX_MSGS = 150           # 一次最多处理多少条
MAX_PER_PERSON = 12      # 每个人最多喂几句
MAX_CLAIMS_PER_PERSON = 2

SYSTEM = "\n".join([
    "你在读一群人聊天的记录，替他们每人记一条『观察』，写进记忆库。",
    "只写从这些话里真能看出来的：稳定偏好、习惯、脾气、在行什么、最近在忙什么。",
    "看不出来就不写。不要复述原话，不要评价人品，不写政治、身体、住址、账号这类私事。",
    "输出 JSON 数组，不要别的字。",
])

PROMPT_HEAD = """下面是按人分组的聊天记录。为每个人提炼 1-2 条观察。

每条格式：
{"subject": "QQ号", "claim": "一句话，<=40字", "type": "trait|preference|habit|interest|relation", "confidence": 0.3~0.8}

把握不大就给低 confidence。宁少勿多。

--- 材料 ---
"""


def _read_state() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            j = json.load(f)
            return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def _write_state(j: dict) -> None:
    try:
        os.makedirs(LIVE, exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning("[obs-export] 写账本失败：%s", e)


def _rebuild_merged() -> int:
    """旧库 + 新库 → observations-all.jsonl（按 id 去重、按 at 排序）。返回总行数。"""
    seen, rows = set(), []
    for path in (OLD_OBS, OBS):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("t") != "obs":
                        continue
                    key = o.get("id") or (str(o.get("subjectId")) + "|" + str(o.get("claim"))[:40])
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(o)
        except Exception as e:
            logger.warning("[obs-export] 读 %s 失败：%s", path, e)
    rows.sort(key=lambda o: int(o.get("at") or o.get("createdAt") or 0))
    try:
        os.makedirs(LIVE, exist_ok=True)
        tmp = MERGED + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for o in rows:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
        os.replace(tmp, MERGED)
    except Exception as e:
        logger.warning("[obs-export] 写合并文件失败：%s", e)
    return len(rows)


@register("astrbot_plugin_obs_export", "溟", "把对话炼成观察，喂给星图（QQ-Agent 旧库 + AstrBot 新话）", "0.1.0")
class ObsExport(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task = None
        self._busy = False

    async def initialize(self) -> None:
        try:
            os.makedirs(LIVE, exist_ok=True)
            n = _rebuild_merged()
            logger.info("[obs-export] 合并库已就绪：%d 条", n)
        except Exception as e:
            logger.warning("[obs-export] 初始化合并失败：%s", e)
        self._task = asyncio.create_task(self._loop())
        logger.info("[obs-export] 归档已启动（每 %d 秒看一次新话）", EVERY_SEC)

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------- ① 归档 ----------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def archive_group(self, event: AstrMessageEvent):
        self._archive(event, "group")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def archive_private(self, event: AstrMessageEvent):
        self._archive(event, "private")

    def _archive(self, event: AstrMessageEvent, kind: str) -> None:
        try:
            text = (event.get_message_str() or "").strip()
            if not text:
                return
            uid = str(event.get_sender_id() or "")
            self_id = str(event.get_self_id() or "")
            if not uid or (self_id and uid == self_id):
                return
            gid = ""
            if kind == "group":
                try:
                    gid = str(event.get_group_id() or "")
                except Exception:
                    gid = ""
                if not gid:
                    return
            chat = ("group:" + gid) if kind == "group" else ("private:" + uid)
            row = {
                "at": int(time.time() * 1000),
                "kind": kind,
                "chatKey": chat,
                "uid": uid,
                "name": str(event.get_sender_name() or "")[:24],
                "text": text[:300],
            }
            with open(MSGS, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("[obs-export] 归档失败：%s", e)

    # ---------- ② 提炼 ----------
    async def _loop(self) -> None:
        await asyncio.sleep(180)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[obs-export] 提炼出错（下轮再试）：%s", e)
            await asyncio.sleep(EVERY_SEC)

    async def _tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            st = _read_state()
            offset = int(st.get("offset") or 0)
            if not os.path.exists(MSGS):
                return
            with open(MSGS, "r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            if len(lines) - offset < MIN_NEW:
                return
            batch = []
            for ln in lines[offset: offset + MAX_MSGS]:
                try:
                    batch.append(json.loads(ln))
                except Exception:
                    continue
            if len(batch) < MIN_NEW:
                _write_state({"offset": len(lines), "updatedAt": datetime.now().isoformat(timespec="seconds")})
                return

            by_person: dict[str, dict] = {}
            for m in batch:
                uid = str(m.get("uid") or "")
                if not uid:
                    continue
                p = by_person.get(uid) or {"name": m.get("name") or "", "chat": m.get("chatKey") or "", "lines": []}
                if len(p["lines"]) < MAX_PER_PERSON:
                    p["lines"].append(str(m.get("text") or "")[:120])
                p["chat"] = m.get("chatKey") or p["chat"]
                p["name"] = m.get("name") or p["name"]
                by_person[uid] = p
            if not by_person:
                return

            material = []
            for uid, p in by_person.items():
                material.append(f"{uid} {p['name']}：")
                material.extend("- " + t for t in p["lines"])
                material.append("")
            prompt = PROMPT_HEAD + "\n".join(material)

            prov = None
            try:
                prov = self.context.get_using_provider()
            except TypeError:
                try:
                    prov = self.context.get_using_provider(f"{PLATFORM_ID}:FriendMessage:100000001")
                except Exception:
                    prov = None
            except Exception:
                prov = None
            if prov is None:
                logger.info("[obs-export] 没有可用 provider，跳过这轮")
                return

            resp = await asyncio.wait_for(
                prov.text_chat(prompt=prompt, system_prompt=SYSTEM, contexts=[], session_id=None),
                timeout=120,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            items = self._parse(text)
            if not items:
                logger.info("[obs-export] 这轮没提炼出东西（raw=%r）", text[:60])
                _write_state({"offset": len(lines), "updatedAt": datetime.now().isoformat(timespec="seconds")})
                return

            now_ms = int(time.time() * 1000)
            wrote = 0
            with open(OBS, "a", encoding="utf-8") as f:
                for i, it in enumerate(items):
                    uid = str(it.get("subject") or "").strip()
                    claim = str(it.get("claim") or "").strip()
                    if not uid or uid not in by_person or len(claim) < 4:
                        continue
                    chat = by_person[uid]["chat"]
                    kind = "group" if chat.startswith("group:") else "private"
                    row = {
                        "t": "obs",
                        "id": f"astr-{now_ms}-{i}",
                        "subjectId": uid,
                        "claim": claim[:80],
                        "claimKey": json.dumps(["v1", "astrbot", chat, uid, claim[:80]], ensure_ascii=False),
                        "type": str(it.get("type") or "statement")[:16],
                        "scope": kind,
                        "kind": kind,
                        "chatKey": chat,
                        "evidenceIds": [],
                        "counterEvidenceIds": [],
                        "confidence": float(it.get("confidence") or 0.4),
                        "at": now_ms,
                        "validUntil": None,
                        "supersedes": None,
                        "version": 1,
                        "status": "active",
                        "createdAt": now_ms,
                        "source": "astrbot",
                        "name": by_person[uid]["name"][:24],
                    }
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    wrote += 1
            total = _rebuild_merged()
            _write_state({"offset": len(lines), "updatedAt": datetime.now().isoformat(timespec="seconds")})
            logger.info("[obs-export] 提炼 %d 条，合并库共 %d 条（处理到第 %d 行）", wrote, total, len(lines))
        finally:
            self._busy = False

    @staticmethod
    def _parse(text: str) -> list:
        if not text:
            return []
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except Exception:
            return []
        if not isinstance(arr, list):
            return []
        return [x for x in arr if isinstance(x, dict)]
