"""溟·群角色识别（AstrBot 侧）

2026-11-17 08:2x 立。她要的：「所有群都加识别群主/管理员」，让回复能判断
「这句是管理说的」，而不是把群主当普通群友顶回去。

做法：
  1. 拉清单：get_group_list → 逐群 get_group_member_list（串行，间隔 1 秒，抗 OneBot 频控），
     把 role=owner/admin 的人存成缓存 data/ming/group-roles.json
  2. 查身份：role_of(group_id, user_id) → owner / admin / member / unknown
     （缓存里有这个群的条目、又不是 owner/admin，就按 member 算——能发群消息的人本来就在群里）
  3. 注入：群里每轮 LLM 请求前，把「你正在跟谁说话 + 本群群主/管理员是谁」拼一小段塞进上下文
  4. 刷新：启动拉一遍；每群 6 小时过期重拉；每 30 分钟用 get_group_info 的 member_count
     比一次（成员增减的代理信号），变了就重拉那一群；拉失败保留旧缓存、只记日志、不阻塞回复

缓存结构（她点名的形状）：
  {
    "700000001": {
      "updated_at": "2026-11-17 08:31:02",
      "_ts": 1763... ,                      # epoch 秒，判过期用，别拿字符串算
      "members_count": 36,
      "owner":  [{"qq": "...", "card": "...", "nick": "..."}],
      "admins": [{"qq": "...", "card": "...", "nick": "..."}]
    }
  }
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

try:  # v4.30+
    from astrbot.api.agents import TextPart
except Exception:  # 4.29.x
    from astrbot.core.agent.message import TextPart

DATA_DIR = r"C:\dsh\astrbot\data\ming"
CACHE_PATH = os.path.join(DATA_DIR, "group-roles.json")

STALE_SEC = 6 * 3600        # 每群缓存 6 小时过期
MEMBER_CHECK_SEC = 1800     # 每 30 分钟比一次成员数
CALL_GAP = 1.0              # 两次 OneBot 调用之间隔 1 秒（串行，抗频控）
LOOP_SEC = 300              # 后台循环的睡眠粒度
INJECT_MAX = 220            # 注入字数上限，别把上下文吃光

# ---------------- 缓存读写（模块级，别处也能直接 import role_of） ----------------

_fc = {"mtime": 0.0, "data": {}}


def load_cache() -> dict:
    try:
        m = os.path.getmtime(CACHE_PATH)
    except OSError:
        return {}
    if m != _fc["mtime"]:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _fc["data"] = json.load(f)
            _fc["mtime"] = m
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-roles] 读缓存失败：%s", e)
            return {}
    return _fc["data"]


def role_of(group_id, user_id) -> str:
    """owner / admin / member / unknown。缓存里没有这个群 = unknown。"""
    g = load_cache().get(str(group_id))
    if not isinstance(g, dict):
        return "unknown"
    uid = str(user_id)
    for p in g.get("owner") or []:
        if str(p.get("qq")) == uid:
            return "owner"
    for p in g.get("admins") or []:
        if str(p.get("qq")) == uid:
            return "admin"
    return "member"


def _save(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)


def _unwrap(resp):
    if isinstance(resp, dict) and "data" in resp:
        if any(k in resp for k in ("retcode", "status", "msg", "wording")):
            return resp.get("data")
    return resp


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _who(p: dict) -> str:
    return (p.get("card") or p.get("nick") or p.get("qq") or "?").strip()


class MingGroupRoles(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._client = None
        self._task = None
        self._lock = asyncio.Lock()
        self._dirty: dict[str, str] = {}

    # ---------- OneBot 连接 ----------

    @staticmethod
    def _usable(c):
        if c is None:
            return None
        if callable(getattr(c, "call_action", None)):
            return c
        api = getattr(c, "api", None)
        if api is not None and callable(getattr(api, "call_action", None)):
            return c
        return None

    async def _get_client(self, event: AstrMessageEvent | None = None):
        if event is not None:
            c = self._usable(getattr(event, "bot", None))
            if c is not None:
                self._client = c
                return c
        c = self._usable(self._client)
        if c is not None:
            self._client = c
            return c
        try:
            pm = self.context.platform_manager
            insts = pm.get_insts() if hasattr(pm, "get_insts") else getattr(pm, "platform_insts", [])
            for p in insts or []:
                # 2026-11-17 08:26：先取 platform.bot（aiocqhttp 就是 CQHttp 本体）；
                # 别再对 CQHttp 做 hasattr(c, "__await__") 判断——它身上有这个属性，
                # 一 await 就抛「object CQHttp can't be used in 'await'」，把整轮刷新带挂。
                cand = getattr(p, "bot", None)
                if cand is None:
                    getter = getattr(p, "get_client", None)
                    if callable(getter):
                        cand = getter()
                        if asyncio.iscoroutine(cand):
                            cand = await cand
                c = self._usable(cand)
                if c is not None:
                    self._client = c
                    return c
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-roles] 遍历平台实例失败：%s", e)
        return None

    async def _api(self, action: str, **kw):
        c = await self._get_client()
        if c is None:
            raise RuntimeError("拿不到 OneBot 客户端")
        fn = getattr(c, "call_action", None)
        if callable(fn):
            return _unwrap(await fn(action, **kw))
        return _unwrap(await c.api.call_action(action, **kw))

    # ---------- 刷新 ----------

    def mark_dirty(self, group_id, reason: str = "") -> None:
        """成员增减之类的信号：标脏，下一轮先重拉这一群。"""
        self._dirty[str(group_id)] = reason or "dirty"
        logger.info("[ming-roles] %s 标脏（%s）", group_id, reason or "dirty")

    async def _pull_group(self, gid: str) -> dict | None:
        members = await self._api("get_group_member_list", group_id=int(gid))
        if isinstance(members, dict):
            members = members.get("data") or []
        if not isinstance(members, list):
            raise RuntimeError(f"get_group_member_list 返回形状异常：{type(members)}")
        owner, admins = [], []
        for m in members:
            role = str(m.get("role") or "member")
            rec = {
                "qq": str(m.get("user_id") or ""),
                "card": str(m.get("card") or ""),
                "nick": str(m.get("nickname") or ""),
            }
            if role == "owner":
                owner.append(rec)
            elif role == "admin":
                admins.append(rec)
        return {
            "updated_at": _now(),
            "_ts": int(time.time()),
            "members_count": len(members),
            "owner": owner,
            "admins": admins,
        }

    async def refresh_all(self, force: bool = False) -> dict:
        """拉一遍所有群。串行 + 1 秒间隔；单群失败不打断整轮。"""
        async with self._lock:
            groups = await self._api("get_group_list")
            if isinstance(groups, dict):
                groups = groups.get("data") or []
            if not isinstance(groups, list):
                raise RuntimeError(f"get_group_list 返回形状异常：{type(groups)}")

            cache = dict(load_cache())
            now = time.time()
            pulled = failed = 0
            for g in groups:
                gid = str(g.get("group_id") or "").strip()
                if not gid:
                    continue
                entry = cache.get(gid) if isinstance(cache.get(gid), dict) else {}
                age = now - float(entry.get("_ts") or 0)
                need = force or gid in self._dirty or age > STALE_SEC

                # 没过期的群：拿成员数比一下，变了就当脏
                if not need and age > MEMBER_CHECK_SEC:
                    try:
                        info = await self._api("get_group_info", group_id=int(gid))
                        if isinstance(info, dict) and "data" in info:
                            info = info.get("data") or info
                        mc = int((info or {}).get("member_count") or 0)
                        old = int(entry.get("members_count") or 0)
                        if mc and old and mc != old:
                            logger.info("[ming-roles] %s 成员数 %s→%s，重拉", gid, old, mc)
                            need = True
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[ming-roles] %s 查成员数失败（不影响别的群）：%s", gid, e)
                    await asyncio.sleep(CALL_GAP)

                if not need:
                    continue
                try:
                    cache[gid] = await self._pull_group(gid)
                    pulled += 1
                    logger.info(
                        "[ming-roles] %s 拉到 %d 人（群主 %d / 管理员 %d）",
                        gid, cache[gid]["members_count"], len(cache[gid]["owner"]), len(cache[gid]["admins"]),
                    )
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    # 失败保留旧条目，只记日志
                    logger.warning("[ming-roles] %s 拉成员失败，保留旧缓存：%s", gid, e)
                self._dirty.pop(gid, None)
                await asyncio.sleep(CALL_GAP)

            cache["$meta"] = {"updated_at": _now(), "groups": len([k for k in cache if not k.startswith("$")])}
            try:
                _save(cache)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ming-roles] 写缓存失败：%s", e)
            logger.info("[ming-roles] 刷新完成：%d 群，重拉 %d，失败 %d", len(groups), pulled, failed)
            return {"groups": len(groups), "pulled": pulled, "failed": failed}

    async def _loop(self) -> None:
        first = True
        while True:
            if first:
                # 2026-11-18 15:3x：等 OneBot WebSocket 连上再拉群成员，
                # 否则启动竞态每次必报「后台刷新失败（30 秒后重试）」。
                await asyncio.sleep(12)
                first = False
            try:
                await self.refresh_all()
            except Exception as e:  # noqa: BLE001
                logger.warning("[ming-roles] 后台刷新失败（30 秒后重试）：%s", e)
                await asyncio.sleep(30)
                continue
            await asyncio.sleep(LOOP_SEC)

    @filter.on_astrbot_loaded()
    async def _on_loaded(self):
        if self._task and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._loop())
            logger.info("[ming-roles] 后台刷新已启动（启动即拉一遍，之后每群 6 小时过期）")
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-roles] 启动后台任务失败，改由首条消息触发：%s", e)

    # ---------- 注入 ----------

    @staticmethod
    def _gid_of(event) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""
        parts = umo.split(":")
        if len(parts) >= 3 and parts[1].lower() == "groupmessage":
            return parts[-1]
        try:
            gid = getattr(event.message_obj, "group_id", None)
            return str(gid) if gid else ""
        except Exception:  # noqa: BLE001
            return ""

    @filter.on_llm_request()
    async def inject_roles(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            gid = self._gid_of(event)
            if not gid:
                return
            # 兜底：后台任务没起来（老版本没触发 on_astrbot_loaded）就先拉一次，不阻塞本轮
            if self._task is None or self._task.done():
                try:
                    self._task = asyncio.create_task(self._loop())
                except Exception:  # noqa: BLE001
                    pass
            if gid in self._dirty:
                try:
                    asyncio.create_task(self.refresh_all())
                except Exception:  # noqa: BLE001
                    pass

            g = load_cache().get(gid)
            if not isinstance(g, dict):
                logger.info("[ming-roles] %s 还没有缓存，本轮不注入", gid)
                return

            sender = getattr(event.message_obj, "sender", None)
            uid = str(getattr(sender, "user_id", "") or "")
            name = str(getattr(sender, "name", "") or uid)
            role = role_of(gid, uid)
            zh = {"owner": "群主", "admin": "管理员", "member": "普通群友", "unknown": "身份不明"}[role]

            parts = [f"你正在和 {name}（QQ {uid}，{zh}）说话。"]
            if role == "owner":
                parts.append("这条是群主说的。")
            elif role == "admin":
                parts.append("这条是管理员说的——他能禁言、能改名片，别拿他当普通群友顶回去。")
            owner = "、".join(_who(p) for p in (g.get("owner") or [])[:2]) or "（未知）"
            admins = "、".join(_who(p) for p in (g.get("admins") or [])[:5])
            line = f"【本群身份 · 缓存 {str(g.get('updated_at', ''))[5:16]}】" + "".join(parts)
            line += f"群主：{owner}。"
            if admins:
                line += f"管理员：{admins}。"
            line += "（只用来判断说话的人是谁，不用在回复里点明身份。）"
            req.extra_user_content_parts.append(TextPart(text=line[:INJECT_MAX]).mark_as_temp())
            logger.info("[ming-roles] 注入 %s：%s=%s", gid, name, zh)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-roles] 注入失败（不影响本轮）：%s", e)
