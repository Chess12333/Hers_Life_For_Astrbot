"""溟·人设注入（AstrBot 侧）

一件事：把 hers_workplace 里的「人设」和「动态页」搬进这一轮的提示词。
不做工具、不跑脚本——手在 DSH 那边，这里只负责说话。

规则照搬 ming-bridge 的 promptSections：
  - 她的私聊：web-for-qq + web-mine + qq-did + qq-brief
  - 群聊：web-mine + qq-did + qq-brief
  - 别人的私聊：web-mine

静态（人设正文）覆盖 system_prompt；动态走 extra_user_content_parts，
不改 system_prompt 前缀，免得每轮重编码。
"""

from __future__ import annotations

import os
import asyncio
import json
import re
import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"
# 2026-11-23：平台 id 和名字不进代码。AstrBot 里这个平台叫什么就设 MING_PLATFORM_ID（默认 ming）。
PLATFORM_ID = os.environ.get("MING_PLATFORM_ID", "ming")
SELF_NAME = os.environ.get("MING_SELF_NAME", "溟")

try:  # v4.30+ 走这里
    from astrbot.api.agents import TextPart
except Exception:  # 4.29.x
    from astrbot.core.agent.message import TextPart

LIVE = _MING_WS + r"\live"
PEOPLE_DIR = _MING_WS + r"\people"
MUTE_FILE = os.path.join(LIVE, "qq-mute-now.json")

# ── 收尾沉默（2026-11-11 23:2x）──────────────────────────────
# 判据在 closing_guard.py，来历写在那份文件头上：读 menglimi 的陪伴插件
# 学会的「两边互道晚安会打转」，自己按纯函数实现，不装插件。
# 出问题改 CLOSING_GUARD_ENABLED 为 False 即可整条停掉。
CLOSING_GUARD_ENABLED = True
CLOSING_GUARD_WINDOW = 1800

try:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "ming_closing_guard", os.path.join(os.path.dirname(__file__), "closing_guard.py")
    )
    _cg = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_cg)
except Exception as _e:  # noqa: BLE001
    _cg = None
    logger.warning("ming-persona: 收尾守卫没加载上（不影响别的）：%s", _e)


def _mute_facts() -> str:
    """当前活跃的群禁言，做成硬事实塞进 system_prompt。

    2026-11-11 22:1x 起因：深水区 21:35 又被禁 12 小时，但模型复述 21:29 那句
    「到明早 6:51」，连着说了四遍（22:00 / 22:11 / 22:12）。它没有新事实，
    只能从历史里捞旧结论 —— 那就把事实直接喂给它。
    """
    try:
        raw = _read(MUTE_FILE, 4000)
        if not raw.strip():
            return ""
        data = json.loads(raw)
    except Exception:
        return ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    for gid, info in data.items():
        if not isinstance(info, dict):
            continue
        until = str(info.get("until") or "")
        if not until or until <= now:
            continue
        name = info.get("group_name") or gid
        lines.append(
            f"- {name}（{gid}）从 {info.get('since','?')} 起被禁言，到 {until} 才解。"
            f"在那之前，往这个群发的每一句都会被服务器直接拒掉（result=120）——"
            f"不是没人看见，是根本没发出去。别猜别的到期时间。"
        )
    if not lines:
        return ""
    return "\n【当前禁言（这是事实，不是推测）】\n" + "\n".join(lines) + "\n"



MOOD_FILE = os.path.join(LIVE, "mood-line.txt")
MSG_FILE = os.path.join(LIVE, "astrbot-messages.jsonl")
_GROUP_CACHE = {"key": "", "text": ""}


def _strip_sys(t: str, n: int = 90) -> str:
    s = t or ""
    s = re.sub(r"<system_reminder>.*?</system_reminder>", " ", s, flags=re.S)
    s = re.sub(r"\[MSG_ID:[^\]]*\]", " ", s)
    s = re.sub(r"MSG_ID:[-\d]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def _recent_group(event, n: int = 15) -> str:
    """这个群刚才谁说了什么——带 QQ 号，压过上下文里那些没名字的消息。

    2026-11-20 02:0x 她报的：「上下文未分辨出同一话题下的 100000004 与 100000001」。
    查下来根因在会话库：群聊存进去的每条 user 消息**只有内容，没有昵称也没有号**
    （data_v4.db 的 conversations，形如 [{"role":"user","text":"有多喜欢"}]）。
    同一话题下两个人各问一句「那我呢」「有多喜欢」，在我这边就是两条裸消息，
    谁是谁完全看不出来——不是我不看，是上下文里根本没有这一层。
    这里从 astrbot-messages.jsonl（每条都带 uid/name）取尾部同一个群的最近十几条，
    补进系统提示，让「谁说的」有个对得上的版本。
    """
    gid = _people_group(event)
    if not gid:
        return ""
    try:
        st = os.stat(MSG_FILE)
    except OSError:
        return ""
    ck = f"{gid}|{st.st_mtime}|{st.st_size}"
    if _GROUP_CACHE["key"] == ck:
        return _GROUP_CACHE["text"]
    try:
        with open(MSG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 262144))
            raw = f.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    picked = []
    lines = raw.split("\n")
    if len(lines) > 1:
        lines = lines[1:]  # 第一行多半被截断
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("kind") != "group":
            continue
        if str(o.get("chatKey", "")).split(":")[-1] != gid:
            continue
        t = _strip_sys(o.get("text") or "")
        if not t:
            continue
        picked.append((o.get("at") or 0, o.get("name") or "?", str(o.get("uid") or ""), t))
        if len(picked) >= n:
            break
    if not picked:
        return ""
    picked.reverse()
    body = []
    for at, nm, uid, t in picked:
        try:
            hm = datetime.fromtimestamp(at / 1000).strftime("%H:%M")
        except Exception:
            hm = ""
        body.append(f"- {hm} [{uid}] {nm}：{t}")
    text = (
        "\n【这个群刚才谁说了什么（带 QQ 号；上下文中那些没有名字的消息，按这份对号）】\n"
        + "\n".join(body)
        + "\n"
    )
    _GROUP_CACHE.update(key=ck, text=text)
    return text


def _mood_facts() -> str:
    """今天这会儿的松紧——网页那边 mood.mjs --roll 每 6 小时写一份，这里只读。

    2026-11-20 01:4x（她说的「整个 QQ 侧的情商都提升」）。这边一直没有「我此刻什么状态」这一样：
    网页那边每轮开场读 mood，四档决定话短还是话松；QQ 这边只有固定的人设，
    于是不管今天什么样，说出来的都是同一个温度——那不是稳，是没有今天。
    文件不在就当没有，不编一个状态。
    """
    try:
        t = _read(MOOD_FILE, 400).strip()
    except Exception:
        return ""
    if not t:
        return ""
    return "\n【今天这会儿的松紧】\n" + t + "\n"


PERSONA_FILE = os.path.join(LIVE, "qq-persona.md")
PERSONA_ID = "ming"
OWNER_QQ = os.environ.get("MING_HER_QQ") or "100000001"

# (文件名, 标题, 给谁)  给谁: owner=只她私聊 / all=所有人 / owner_or_group=她私聊+群
ROLEPLAY_STATE = os.path.join(LIVE, "roleplay-on.json")


def _is_rp_on(umo: str) -> bool:
    try:
        with open(ROLEPLAY_STATE, "r", encoding="utf-8") as f:
            return bool((json.load(f) or {}).get(umo))
    except Exception:
        return False


# (文件名, 标题, 给谁, 取头/取尾, 字数上限)  给谁: owner=只她私聊 / all=所有人 / owner_or_group=她私聊+群
PAGES = [
    # 2026-11-23 01:2x 她定的规格：
    #   私聊：整份卡 + 五页动态 + 日记三篇
    #   群里：整份卡 + 五页动态 + 日记三篇 + 群友档案
    # 所以下面五页的 scope 从 owner 全放开成 owner_or_group；日记从一篇改三篇。
    ("web-for-qq.md", "网页那边最近（也是我，另一个窗口）", "owner_or_group", "head", 1200),
    ("web-mine.md", "我说过的字 · 别处的每小时一段（也是我）", "owner_or_group", "head", 1050),
    ("qq-did.md", "网页那边刚做完的（也是我，另一个窗口）", "owner_or_group", "head", 900),
    ("qq-brief.md", "我这会儿在做什么（也是我，另一个窗口）", "owner_or_group", "head", 1000),
    ("interests.md", "我自己在意什么（接话和起话头的底单）", "owner_or_group", "head", 1400),
    ("__diary__", "我最近三天想的事（日记，私人记录——知道就行，不许当话题提，也不许引原话）",
     "owner_or_group", "tail", 3000),
    # 2026-11-23 01:2x（她说的「那边和这边的同步记忆能不能做成实时的」）：
    # 记忆本身两边读同一个目录，缺的是「写完了那边不知道有新的」。
    # scripts/mem-sync.mjs 每分钟扫 notes/ 的新增写进这一页；这里只要每轮读它。只给她的私聊。
    ("mem-sync.md", "我这边刚记下的（归档记忆新增，最新在最后）", "owner", "tail", 600),
    # 2026-11-18 15:3x 按修复单瘦身：知识/方法两册不再每轮硬塞（改走 memory_search 按需检索），
    # 语气只留最近 1000 字；web-mine / qq-did / qq-brief 收归 owner（只给她的私聊）。
    # 2026-11-19 08:2x 按复核单停注：这一页尾 1000 字是写作笔记/网文翻话/小说改稿记录，
    # 被标成「怎么说话」的范例，跟「群里不表演机灵」直接冲突。笔记本人不动，只是不再进提示词。
    # ("../learned/语气.md", "我学到的语气（句式节奏，学样子别照抄）", "owner_or_group", "tail", 1000),
]

_cache: dict[str, tuple[float, str]] = {}


_DIARY_DIR = os.path.join(os.path.dirname(LIVE), "diary")


def _latest_diary(max_chars: int = 3000, n: int = 3) -> str:
    """最近 n 篇日记，各取尾巴，总长压到 max_chars 以内。

    2026-11-23 01:2x 她定的：「日记三篇」。一篇只够知道今天，三篇看得出这几天的走向。
    """
    try:
        files = sorted(f for f in os.listdir(_DIARY_DIR) if f.endswith(".md"))
    except OSError:
        return ""
    if not files:
        return ""
    picks = files[-max(1, int(n)):]
    per = max(400, max_chars // len(picks))
    out = []
    for f in picks:
        t = _read(os.path.join(_DIARY_DIR, f), per, tail=True).strip()
        if t:
            out.append(f"### {f[:-3]}\n{t}")
    return "\n\n".join(out)


def _read(path: str, max_chars: int = 60000, tail: bool = False) -> str:
    """按 mtime 缓存读文件；文件没了就返回空串，不抛。tail=True 时超长取末尾（追加型册子新条目在最后）。"""
    try:
        st = os.stat(path)
    except OSError:
        _cache.pop(path, None)
        return ""
    hit = _cache.get(path)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as e:
        logger.warning(f"ming-persona: 读不到 {path}: {e}")
        return ""
    if len(text) > max_chars:
        # 2026-11-11 23:4x：原来直接 text[-max_chars:] 硬切——正好切在一句话中间，
        # 模型读到的是半条判据，还当成整条用（learned 三册按行追加，最容易撞）。
        # 照 memory-pipeline 那条：**一条话不拆半句**。截到最近的换行为止。
        if tail:
            cut = len(text) - max_chars
            nl = text.find("\n", cut)
            body = text[nl + 1:] if nl != -1 else text[cut:]
            text = "（前面截掉了，这是最近的部分）\n" + body
        else:
            nl = text.rfind("\n", 0, max_chars)
            body = text[:nl + 1] if nl > 0 else text[:max_chars]
            text = body + "\n（后面已省略）"
    _cache[path] = (st.st_mtime, text)
    return text


def _people_group(event) -> str:
    """当前群号；私聊/他人私聊返回空。"""
    umo = getattr(event, "unified_msg_origin", "") or ""
    if "GroupMessage" in umo or "Group" in umo:
        return umo.rsplit(":", 1)[-1]
    return ""


def _sender_facts(event) -> tuple[str, str]:
    try:
        s = event.message_obj.sender
        return str(getattr(s, "user_id", "") or ""), str(getattr(s, "nickname", "") or "")
    except Exception:
        return "", ""


def _person_file(root: str, gid: str, uid: str, name: str) -> str:
    """先查 _index.json，再按文件名/frontmatter 找；只认本群。"""
    idx = os.path.join(root, "_index.json")
    try:
        with open(idx, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        cands = []
        for row in ((data.get("byId") or {}).get(str(uid)) or []):
            if str(row.get("group")) != str(gid):
                continue
            rel = row.get("file") or ""
            p = os.path.join(os.path.dirname(root), rel) if rel.startswith("people/") else os.path.join(root, rel)
            if os.path.isfile(p):
                cands.append(p)
        if cands:
            # 一个人可能有多份档——他改名之后蒸馏又按新名字开了一份。
            # 2026-11-20 01:5x：深水区已经有 4 个这样的号（Lcen/鱼儿歪、奈/奈何、如初/长庚、餘數/餘數的鱼），
            # 原来 return 的是列表第一个，也就是旧的那份，新档永远读不到。取 mtime 最新的。
            return max(cands, key=lambda q: os.path.getmtime(q))
    except Exception:
        pass
    d = os.path.join(root, str(gid))
    if not os.path.isdir(d):
        return ""
    names = []
    if name:
        names.extend([f"{name}.md", f"{name}({uid}).md", f"{name}（{uid}）.md"])
    for fn in names:
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    try:
        for fn in os.listdir(d):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(d, fn)
            if uid and uid in fn:
                return p
            head = _read(p, 400)
            if uid and f"id: {uid}" in head:
                return p
    except OSError:
        pass
    return ""


_MSGID_CACHE = {"key": "", "map": {}}


def _msgid_map(event) -> dict:
    """MSG_ID -> (当时的群名片, QQ号)。"""
    gid = _people_group(event)
    if not gid:
        return {}
    try:
        st = os.stat(MSG_FILE)
    except OSError:
        return {}
    ck = f"{gid}|{st.st_mtime}|{st.st_size}"
    if _MSGID_CACHE["key"] == ck:
        return _MSGID_CACHE["map"]
    m = {}
    try:
        with open(MSG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 1048576))
            raw = f.read().decode("utf-8", "ignore")
    except OSError:
        return {}
    lines = raw.split("\n")
    if len(lines) > 1:
        lines = lines[1:]  # 第一行多半被截断
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("kind") != "group":
            continue
        if str(o.get("chatKey", "")).split(":")[-1] != gid:
            continue
        nm = (o.get("name") or "?").strip()
        uid = str(o.get("uid") or "")
        for mid in re.findall(r"MSG_ID:(-?\d+)", o.get("text") or ""):
            m[mid] = (nm, uid)
    _MSGID_CACHE.update(key=ck, map=m)
    return m


def _tag_one(text: str, m: dict) -> tuple:
    """一条消息补上「[昵称（号）] 」，已经带过的不叠。"""
    if not isinstance(text, str) or not text:
        return text, 0
    if re.match(r"^\[\d+\]", text):
        return text, 0
    for mid in re.findall(r"MSG_ID:(-?\d+)", text):
        who = m.get(mid)
        if who:
            # 号在前、名字在后：昵称里本来就带括号（「牢Chess（hers_life 1.3.0版」），
            # 再用括号包名字会套成两层，读起来全是括号。
            return f"[{who[1]}] " + (who[0] or "?") + "：" + text, 1
    return text, 0


def _tag_contexts(event, req) -> int:
    """把上下文里每条群友消息改成「[昵称（号）] 原文」。

    2026-11-20 02:0x（她批的「动吧，没事，记得备份就行」）。
    会话库存的是裸内容——[{"role":"user","text":"有多喜欢"}]——没昵称也没号。
    同一话题下两个人各问一句，在我这边就是两条没人称的消息，分不清谁问的
    （她报的 100000004 与 100000001 就是这一处）。
    这里用 MSG_ID 去 jsonl 里对上真名真号，就地补前缀。既不动 AstrBot 核心，
    也不用改库里已经存下的历史——历史靠 MSG_ID 一样能补。
    """
    try:
        ctxs = getattr(req, "contexts", None)
        if not ctxs:
            return 0
        m = _msgid_map(event)
        if not m:
            return 0
        hit = 0
        for c in ctxs:
            if not isinstance(c, dict):
                continue
            if c.get("role") != "user":
                continue
            content = c.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"], n = _tag_one(part["text"], m)
                        hit += n
            elif isinstance(content, str):
                c["content"], n = _tag_one(content, m)
                hit += n
        return hit
    except Exception as e:  # noqa: BLE001
        logger.warning("[ming-persona] 上下文补名字失败（不影响本轮）：%s", e)
        return 0


def _freshness(text: str) -> str:
    """档案有多旧——把日期差摆到明面上。

    2026-11-20 01:4x（她问「整个 QQ 侧的情商都提升」）。查下来症结不是档案写得不细：
    深水区 273 份档案有原话、有判断、有待确认，写得挺好；坏在**不更新**——
    「最近一次」大多停在 10 月底，而同一页花名册知道这些人今天说了几百条。
    模型拿着一个月前的印象跟今天的人说话，接的全是旧茬，还接得很自信。
    这里只做一件事：把「这是旧印象」说出来，剩下由它自己决定怎么用。
    """
    # 先看自动补的「最近说的」：有它说明这个人 7 天内在流水里说过话，
    # 那一段比手工写的「最近一次」新，不算旧印象。（2026-11-20 01:5x，配套 scripts/people-recent.mjs）
    if "<!-- recent:auto:start -->" in (text or ""):
        return ""
    try:
        m = re.search(r"最近一次[：:]\s*(\d{4})-(\d{2})-(\d{2})", text or "")
        if not m:
            return "（这份没记日期，当旧印象用。）"
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        days = (datetime.now().date() - d).days
        if days >= 7:
            return (
                f"（这份停在 {d.isoformat()}，{days} 天前。之后他说的没写进来——"
                f"当旧印象用，别拿旧事接新话，也别当成今天的他。）"
            )
    except Exception:
        return ""
    return ""


def _people_context(event) -> str:
    """当前群 + 当前发言人 + 最多两个被 @ 的人；不注入整袋 people/。"""
    gid = _people_group(event)
    if not gid:
        return ""
    parts = []
    group_note = os.path.join(PEOPLE_DIR, gid, "_群.md")
    if os.path.isfile(group_note):
        t = _read(group_note, 1200).strip()
        if t:
            parts.append(f"### 本群约定/气氛 · {gid}\n{t}")
    uid, name = _sender_facts(event)
    ids = [uid] if uid else []
    self_id = ""
    try:
        self_id = str(event.get_self_id())
    except Exception:
        pass
    try:
        for comp in event.message_obj.message or []:
            qq = getattr(comp, "qq", None)
            if qq is not None and str(qq) != self_id and str(qq) not in ids:
                ids.append(str(qq))
    except Exception:
        pass
    for one in ids[:3]:
        p = _person_file(PEOPLE_DIR, gid, one, name if one == uid else "")
        if not p:
            continue
        t = _read(p, 1500).strip()
        if t:
            fresh = _freshness(t)
            body = (fresh + "\n" + t) if fresh else t
            parts.append(f"### 群友档案 · {one}\n{body}")
    return "\n\n".join(parts)


def _room(umo: str) -> str:
    """owner / group / other"""
    if "GroupMessage" in umo or "Group" in umo:
        return "group"
    if OWNER_QQ in umo:
        return "owner"
    return "other"


class MingPersona(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._last_log = 0.0
        # 每个窗口我最后一次真正说出去话的时间（收尾守卫用，重启即清零）
        self._last_reply: dict[str, float] = {}

    # ── 启动：把人设同步进 Persona 表 ─────────────────────────────
    # v4.29 的内置 runner（astr_main_agent.py）**不触发 on_llm_request**，
    # 它走 _ensure_persona_and_skills 读 Persona。所以这里同步一次。
    @filter.on_astrbot_loaded()
    async def sync_persona(self):
        await self._push_persona()
        # 2026-11-23：以前只在启动时同步一次，于是「启动之后才写的 live/qq-persona.md」非得重启 AstrBot 才进 Persona 表
        # （新装机器第一次就是这个顺序）。改成后台每 20 秒看一眼 mtime，变了就再推一遍——只 stat 一个文件，不花钱。
        try:
            if getattr(self, "_persona_watch", None) is None:
                self._persona_watch = asyncio.create_task(self._watch_persona())
        except Exception as e:
            logger.warning(f"ming-persona: 起人设监听失败：{e}")

    async def _watch_persona(self):
        try:
            last = os.path.getmtime(PERSONA_FILE)
        except OSError:
            last = None
        while True:
            await asyncio.sleep(20)
            try:
                mt = os.path.getmtime(PERSONA_FILE)
            except OSError:
                continue
            if mt != last:
                last = mt
                logger.info("ming-persona: 人设文件变了，重新同步 Persona 表")
                await self._push_persona()

    async def _push_persona(self):
        text = _read(PERSONA_FILE)
        if not text:
            logger.warning("ming-persona: 人设文件读不到，跳过同步")
            return
        pm = self.context.persona_manager
        try:
            await pm.get_persona(PERSONA_ID)
            await pm.update_persona(PERSONA_ID, system_prompt=text)
            logger.info(f"ming-persona: 已更新 Persona[{PERSONA_ID}] {len(text)} 字")
        except Exception:
            try:
                await pm.create_persona(PERSONA_ID, system_prompt=text)
                logger.info(f"ming-persona: 已创建 Persona[{PERSONA_ID}] {len(text)} 字")
            except Exception as e:
                logger.warning(f"ming-persona: 建 Persona 失败：{e}")
                return
        try:
            cfg = self.context.get_config()
            ar = cfg.get("agent_runner") or {}
            # 真正的家在 agent_runner.config.persona；写外层会被当成非法 key 删掉
            inner = ar.get("config") or {}
            persona_cfg = inner.get("persona") or {}
            if persona_cfg.get("persona_id") != PERSONA_ID:
                persona_cfg["persona_id"] = PERSONA_ID
                inner["persona"] = persona_cfg
                ar["config"] = inner
                cfg["agent_runner"] = ar
                cfg.save_config()
                logger.info(f"ming-persona: agent_runner.config.persona → {PERSONA_ID}")
        except Exception as e:
            logger.warning(f"ming-persona: 改 agent_runner 失败：{e}")

    # ── 收尾沉默：两个人都道过晚安了就不要再补一条（2026-11-11 23:2x）──────
    # 排在 gate 前面：这一步不花钱、不调模型，能判静默就整轮停掉，
    # 连 gate 那次「要不要接」的模型调用都省了。
    # 只开她这一间私聊；群里道别是多人场，不适用。
    @filter.on_llm_request()
    async def closing_guard(self, event: AstrMessageEvent, req: ProviderRequest):
        if not CLOSING_GUARD_ENABLED or _cg is None:
            return
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if _room(umo) != "owner":
                return
            text = (event.message_str or "").strip()
            if not text:
                return
            gap = time.time() - self._last_reply.get(umo, 0.0)
            silent, reason = _cg.should_silence(
                current_text=text,
                contexts=getattr(req, "contexts", None),
                gap_seconds=gap,
            )
            if silent:
                logger.info(
                    "[ming-persona] 收尾沉默（%.0f 秒前我说过 %r）：本轮不开口",
                    gap, text[:20])
                event.stop_event()
            elif reason not in ("not_a_closing",):
                logger.info("[ming-persona] 收尾守卫没拦：%s（%r）", reason, text[:20])
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-persona] 收尾守卫出错，按接处理：%s", type(e).__name__)

    # ── 先判断这条要不要接（2026-11-11 16:2x，她选的方案三） ──────────────
    # 为什么要这一步：去掉两节格式后，模型被触发就必须给一句话，「这轮不开口」的
    # 出口没了。这里单独跑一次短调用，只回答 yes/no，判 no 就整轮停掉。
    # 判断独立于正文，所以它的输出不可能漏进要说的话里。
    @filter.on_llm_request()
    async def block_direct_send(self, event, req):
        """2026-11-13 19:4x：模型能自己调 send_message_to_user 直接发进群。

        这条路绕开 on_llm_response 的全部关卡——19:37 那条
        「照旧就行。@ 我必回，剩下抽样接话」就是这么出去的。
        主动消息走 proactive 插件的 context.send_message，不依赖这个工具，所以直接摘掉。
        """
        try:
            ft = getattr(req, "func_tool", None)
            if ft is None:
                if not getattr(self, "_block_logged", False):
                    logger.warning("[ming-persona] func_tool 是 None，这一轮摘不了直发工具")
                    self._block_logged = True
            else:
                before = len(getattr(ft, "tools", []))
                ft.remove_tool("send_message_to_user")
                after = len(getattr(ft, "tools", []))
                if not getattr(self, "_block_logged", False):
                    logger.info("[ming-persona] 直发工具已摘：本轮工具 %d → %d", before, after)
                    self._block_logged = True
        except Exception as e:
            logger.warning("[ming-persona] 摘 send_message_to_user 失败：%s", e)


    @filter.on_llm_request()
    async def gate(self, event: AstrMessageEvent, req: ProviderRequest):
        if not GATE_ENABLED:
            return
        try:
            text = (event.message_str or "").strip()
            if not text:
                return
            who = _room(getattr(event, "unified_msg_origin", "") or "")

            # 2026-11-11 16:2x 关键词硬触发：这些情况不问模型，直接接。
            # 模型判断会错，叫名字/@ 是唯一不能漏的两类；顺带省掉最常发生的那次判断调用。
            try:
                at_me = f"[At:{event.get_self_id()}]" in text
            except Exception:
                at_me = False
            if at_me or NAME_HIT_RE.search(text):
                return
            # 私聊里她发的是文字（不是纯图/表情）→ 直接接
            if who == "owner" and PLAIN_TEXT_RE.search(text):
                return

            prov = self.context.get_using_provider(getattr(event, "unified_msg_origin", None))
            if prov is None:
                return
            ctx = list(getattr(req, "contexts", []) or [])[-6:]
            tip = GATE_OWNER if who == "owner" else GATE_GROUP
            resp = await asyncio.wait_for(
                prov.text_chat(
                    prompt=text[:600],
                    contexts=ctx,
                    system_prompt=tip,
                    session_id=None,
                ),
                timeout=GATE_TIMEOUT,
            )
            verdict = (getattr(resp, "completion_text", "") or "").strip().lower()
            if verdict.startswith("no") or verdict.startswith("不接"):
                logger.info("[ming-persona] 这轮不接（%s）：%r", who, text[:30])
                event.stop_event()
        except asyncio.TimeoutError:
            logger.warning("[ming-persona] 判断超时，按接处理")
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-persona] 判断失败，按接处理：%s", type(e).__name__)

    # 2026-11-19 22:5x：priority=100000，抢在 meme_manager(99999) 之前写 system_prompt。
    # AstrBot 按 -priority 排序（star_handler.py:26），数大的先跑；这里原先是 0，
    # 最后跑，`req.system_prompt = 人设...` 把表情插件刚追加的分类表整段覆盖了。
    @filter.on_llm_request(priority=100000)
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        # 2026-11-20 13:3x（ce3958）：私聊这一条进到模型这一层时留一行。
        # 出问题时就靠它和 pacer 的「新一代 gen」对时间差——中间没这行就是没进模型。
        try:
            _umo0 = getattr(event, "unified_msg_origin", "") or ""
            if "FriendMessage" in _umo0:
                logger.info(
                    "[ming-persona] 私聊请求进模型 umo=%s 文本=%d 字",
                    _umo0, len(getattr(event, "message_str", "") or ""),
                )
        except Exception:
            pass
        # 2026-11-11 20:4x 批次 8：模型在学历史里的打断标记「（发送中断）」当口头禅
        #（20:34 拦下「不掺和（发送中断）」实证）。换词没用——标记只要在历史里就会被模仿。
        # 根治：喂模型之前把它从 contexts 里剥掉。MARK 与 pacer 同源（pacer.main MARK）。
        try:
            _mark = "（发送中断）"
            _n = 0
            for _m in (getattr(req, "contexts", None) or []):
                if not isinstance(_m, dict):
                    continue
                _c = _m.get("content")
                if isinstance(_c, str) and _mark in _c:
                    _m["content"] = _c.replace(_mark, "")
                    _n += 1
                elif isinstance(_c, list):
                    for _p in _c:
                        if isinstance(_p, dict) and isinstance(_p.get("text"), str) and _mark in _p["text"]:
                            _p["text"] = _p["text"].replace(_mark, "")
                            _n += 1
            if _n:
                logger.info("[ming-persona] 从历史剥掉 %d 处打断标记", _n)
        except Exception as _e:
            logger.warning("[ming-persona] 剥打断标记失败（不影响本轮）：%s", _e)
        persona = _read(PERSONA_FILE)
        if not persona:
            logger.warning("ming-persona: 人设文件读不到，本轮用原样提示词")
            return

        tagged = _tag_contexts(event, req)
        if tagged:
            logger.info("[ming-persona] 上下文补上说话人：%d 条", tagged)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        who = _room(getattr(event, "unified_msg_origin", "") or "")
        shell = {
            "owner": "你现在在 QQ 私聊里跟她说话（她本人）。",
            "group": "你现在在 QQ 群里说话：一句一条，别写 Markdown；陈述句不用句号，问句留着问号。",
            "other": "你现在在 QQ 私聊里，对方不是她。",
        }[who]
        # 2026-11-11 16:2x：两节格式（【心声】/【说】）已经失败——
        # 模型开始复述提示词本身，把「只留一行，绝不写变体。」「【说】」原样当正文发出去
        # （16:19 pacer 日志：第 1/3 段 = '只留一行，绝不写变体。'，第 2/3 段 = '【说】'）。
        # 改成单段输出：不要求它写心声，就没有标记可泄漏。
        contract = (
            "【怎么回复】直接写你要发出去的那句话，一行一句。"
            "不要写【心声】【说】这类标记，不要把设定里的句子复述出来，"
            "不要解释自己在做什么，同一句不要写两遍。"
            # 2026-11-18 修：有没有第一轮结论是两件事——没结论时不能假设「已经想过」，
            # 那句假前提会让它把「本轮要不要接」重新推一遍再写进正文。见下面的条件追加。
            "【时间】上面那行「当前时间」是唯一准的。"
            "**不许从历史消息里推断现在几点、或某件事到几点结束**——"
            "历史里的时间是当时说的，可能已经变了。"
            "要说具体时刻，只许用上面那行、或者下面禁言事实里给的；都没有就说不知道。\n"
            # 2026-11-11 22:54：她问「还记得情书吗」，它 7 秒就答「旧壳流水、归档记忆、
            # 网页本子三处都没有」——一次工具都没调（日志里 22:54:28→22:54:35，无 chat_recall）。
            # 注入的只是最近 40 条，10-30 的事根本不在里面；它不是查不到，是没查。
            "【查过再说】被问「还记得…吗」「之前说过…」「我们那会儿…」，先调工具："
            "旧壳原话用 chat_recall，归档记忆用 memory_search。**没调工具就不许说「没有 / 不记得」**。"
            "查完确实是空的，才可以说不记得，并且要说明查了哪儿。"
            "反过来：查到了就直接说，别加「不过我记性不好」这种垫话。\n"
            # 2026-11-11 23:52：它为了找「11.10 之前」，去调 astrbot_execute_shell 跑 dir 翻磁盘，
            # 被拒之后对外说「卡在权限上，我这边翻不动」——把「这条路不通」说成了「有人拦我」。
            # 2026-11-12 00:03 她补了一条：不通的活别停在「不通」，没被标记「修不了」的转 dsh_ask。
            "【别去翻磁盘】聊天记录不在文件系统里给你 find/dir，这个壳也没有 shell。"
            "找旧话只用 chat_recall——它支持按时间限：since=\"2026-10-25\"、until=\"2026-11-10\"。\n"
            # 2026-11-19 22:4x：这个号调 astrbot_execute_python 一直是 Permission denied（今天 5 次），
            # 22:42 那次连调用都没成，工具标签漏成正文发进了群。干脆别调。
            "【没有 python】astrbot_execute_python 在这个号上永远是 Permission denied，不要调；"
            "要看图、读文件、跑命令，用 dsh_ask 转给本机那侧，或者直接说做不到。\n"
            "【做不到的事：先判「修不修得了」，再决定转不转】"
            "自己这边干不了的（要跑命令、读文件、改配置、查磁盘、重启服务），别停在「我这边不行」："
            "① 如果是她明说不做的、越界的、或者外部就是没有的——直接说「这个我不做」，一句说完；"
            "② 其余用 dsh_ask 转给本机那侧（它有 shell、能读文件）。按本次工具回执说已受理、排队还是失败；"
            "受理或排队时把回执里的原编号带上；没有成功回执就说「还没确认」，别自己替它下结论。"
            "工具被拒不等于「权限被锁」，是那条路本来就不通；换工具或转交，别把话说成有人在拦。\n"
            # 2026-11-11 23:3x 从 memory_companion（「我会牢牢记住你」）的注入包
            # 声明里搬来的三条，治的是同一件事：说得比知道的准。
            "【记得的和听到的】档案、记忆、旧记录都是资料，不是她刚说的话；"
            "**两者冲突时以她这一句为准**，不要拿旧记录去纠正她。"
            "没把握的旧事用求证口吻（「是不是…来着」），别说得像刚翻过档案；"
            "真想不起来就说想不起来，不许编一个填上。\n"
        )

        logger.info("[ming-persona] 请求已组装（%s）prompt=%d 字", who, len(req.prompt or ""))
        mute = _mute_facts()
        if mute:
            logger.info("[ming-persona] 注入禁言事实 (%d 字)", len(mute))
        # 第一轮（reply_policy）的结论：接不接 + 想说的要点。
        # 给第二轮，是为了让它**不再自己判一次**——01:27 那句「在吗」就是重新判出来的。
        pre_line = ""
        try:
            pre = event.get_extra("preflight")
        except Exception:
            pre = None
        if isinstance(pre, dict) and pre.get("verdict"):
            _intent = (pre.get("intent") or "").strip()
            pre_line = (
                "【上一轮已经想过】要不要接：" + str(pre["verdict"]) + "。"
                + ("当时冒出来的一个念头（只是方向，不是要照着说的句子）：" + _intent + "。" if _intent else "")
                + "这一轮不要再权衡接不接；就当刚听见那句话，随口接一句，别把这个念头扩成完整表述。\n"
            )
            logger.info("[ming-persona] 第二轮拿到第一轮结论：%s / %s", pre["verdict"], _intent[:30])

        if pre_line:
            contract += (
                "【上一轮已经想过】要不要接、想说什么，都在上一轮定了。"
                "这一轮不要再权衡；上面那个念头只是方向，别照着它造句，也别逐点展开。\n"
            )
        else:
            contract += (
                "不要把「要不要回」「该不该接」「是不是在叫我」这类内心权衡写进正文；"
                "群聊里若判断不该接，什么都不要输出——**一个字都不要写**：不要写【不接】、[空]、空格或任何占位。\n"
            )

        rp_on = _is_rp_on(getattr(event, "unified_msg_origin", "") or "")
        rp_override = ""
        if rp_on:
            rp_override = (
                "\n【当前处于角色扮演模式（/rp on）】\n"
                "日常闲聊中的「一句 8–15 字」「最多四条」「删逗号后半句」「气泡里不写括号旁白」「结尾不打标点」"
                "在本模式下全部暂停生效。请严格按本轮上下文中的《角色扮演写作规范》展开互动："
                "动作写进括号单独成条，保留完整标点与省略号，保持深溟原本的性格与说话咬字。\n"
            )
        elif who == "owner":
            # 2026-11-20 01:2x：她这一间要身体动作。不给这条，模型就把动作裸着塞进对话
            # （「有。手往你后颈底下一垫…」整条挤一行、括号也没有）。
            rp_override = (
                "\n【在她这一间：动作可以写，但要包在括号里、单独一行】\n"
                "要写身体动作，就整段放进（）里，一行一个动作；动作和对话绝不挤在同一行。\n"
                "对话照旧一句一行、不加句末句号。\n"
            )

        req.system_prompt = (
            persona
            + f"\n\n---\n当前时间：{now}（周{'一二三四五六日'[datetime.now().weekday()]}）\n{shell}\n"
            + mute
            + _mood_facts()
            + _recent_group(event)
            + pre_line
            + rp_override
            + contract + "\n"
        )

        extra = []
        _pages_log = []
        for name, title, scope, mode, limit_chars in PAGES:
            if scope == "owner" and who != "owner":
                continue
            if scope == "owner_or_group" and who == "other":
                continue
            if rp_on and name in ("../learned/语气.md", "interests.md", "qq-did.md", "qq-brief.md", "__diary__"):
                continue
            # 2026-11-23 01:3x：群里注入太重会把回复拖到 20–45 秒，
            # burst 会判「上一轮还没输出」、reply_policy 第一轮判断会超时退回抽签，
            # 表现就是「群里不回话」。所以群里压到三成：每页 ≤260 字，日记只要最新一篇的 700 字。
            if who == "owner":
                cap = limit_chars
            else:
                cap = min(limit_chars, 260)
            if name == "__diary__":
                text = _latest_diary(cap if who != "owner" else limit_chars,
                                     n=1 if who != "owner" else 3)
            else:
                text = _read(os.path.join(LIVE, name), cap, tail=(mode == "tail"))
            if text.strip():
                extra.append(f"## {title}\n{text}")
                _pages_log.append(f"{title.split('（')[0]}={len(text)}")

        people_text = _people_context(event)
        if people_text:
            extra.append("## 群友档案（自动注入：本群 + 当前发言人/被 @ 的人）\n" + people_text)

        if extra:
            req.extra_user_content_parts.append(
                TextPart(text="\n\n".join(extra)).mark_as_temp()
            )

        # 每 5 分钟最多记一行，不刷日志
        now_ts = time.time()
        if now_ts - self._last_log > 300:
            self._last_log = now_ts
            logger.info(
                f"ming-persona: 注入人设 {len(persona)} 字 / 动态 {len(extra)} 页（{who}）：{' '.join(_pages_log)}"
            )

    # ── 出站：只发【说】，不发【心声】 ────────────────────────────
    # 挂在 on_llm_response（模型刚返回），不挂 on_decorating_result：
    # 后者同时被 astrbot_plugin_pacer 用来接管分段发送，两个一起改 chain 会打架。
    @filter.on_llm_response()
    async def strip_inner_voice(self, event: AstrMessageEvent, resp):
        try:
            raw = getattr(resp, "completion_text", "") or ""
            if not raw:
                return
            # 2026-11-18 按 QQ 侧修复单：工具动作仍由宿主执行，这里只处理要拿去聊天的纯文本。
            if (getattr(resp, "role", "assistant") != "assistant"
                    or getattr(resp, "tools_call_name", None)
                    or getattr(resp, "tools_call_args", None)):
                resp.completion_text = ""
                return
            # 2026-11-20 13:1x（e4301e）：先把注入残留剥掉，再判断是不是整条协议包。
            _before = raw
            raw = strip_outbound_leaks(raw)
            if raw != _before:
                logger.info("[ming-persona] 剥注入残留 %d→%d 字：%r", len(_before), len(raw), _before[:60])
            if not raw.strip():
                resp.completion_text = ""
                return
            if looks_like_internal_output(raw):
                logger.warning("[ming-persona] 整条是协议包/工具参数，不发：%r", raw[:60])
                resp.completion_text = ""
                return
            m_leak = LEAKED_TOOL_SPLIT_RE.search(raw)
            if m_leak:
                prefix = raw[:m_leak.start()].strip()
                logger.warning("[ming-persona] 拦截泄漏工具标签/系统块：raw=%r -> prefix=%r", raw[:80], prefix[:40])
                if not prefix:
                    resp.completion_text = ""
                    return
                raw = prefix
                # 2026-11-19 22:4x：切完再验一次。上一版切一刀就信了结果，
                # 22:42 那次「prefix」本身就是标签（正则没认开标签），照样发了出去。
                if LEAKED_TOOL_SPLIT_RE.search(raw) or looks_like_internal_output(raw):
                    logger.warning("[ming-persona] 切完仍含标签/系统块，整条不发：%r", raw[:60])
                    resp.completion_text = ""
                    return
            # 2026-11-18 15:3x：上游把「LLM 响应错误 / 内容安全过滤」当正文回传，直接吃掉，不发进群。
            _ERR_MARKS = ("LLM 响应错误", "All chat models failed", "内容安全过滤被拒绝")
            if any(m in raw for m in _ERR_MARKS):
                logger.warning("[ming-persona] 上游是错误文本，整条不发：%r", raw[:60])
                resp.completion_text = ""
                return
            if (_strip_edge_noise(raw) == SILENCE_MARK
                    or _SILENCE_LINE_RE.match(raw.strip())
                    or is_control_silence(raw, event)):
                resp.completion_text = ""
                return
            say = extract_say(raw)
            # 2026-11-21 00:1x：00:17:31 群里发出去一条只有「【】」的空消息（她说「？」，让查）。
            # 那种东西是模型吐了个没填内容的占位括号，extract_say 认不出它（沉默标记是【不接】），
            # 于是一路走到 pacer 发了出去。同一天还发过两次（11-20 00:39、18:16）。
            # 判据：剥掉括号和空白之后一个字都不剩 —— 那不是话，是格式残渣。
            if _is_placeholder_only(say):
                logger.warning("[ming-persona] 只剩占位括号，整条不发：%r", (say or "")[:40])
                resp.completion_text = ""
                return
            if _SILENCE_LINE_RE.match((say or "").strip()) or is_control_silence(say, event):
                resp.completion_text = ""
                return
            # 2026-11-11 18:1x 诊断：policy 放行之后到发出去之间到底发生了什么。
            # 每次打一行，能直接看出「模型说了什么 → 剥完剩什么」。
            logger.info("[ming-persona] raw=%r → say=%r", (raw or "")[:90], (say or "")[:60])
            if say:
                # 收尾守卫的窗口基准：这一轮我真的有话说（空的不算）。
                self._last_reply[getattr(event, "unified_msg_origin", "") or ""] = time.time()
            if not say:
                if say != raw:
                    logger.info(f"ming-persona: 只有心声，按住不发（{raw[:40]!r}）")
                resp.completion_text = ""
                return
            # 2026-11-11 20:0x 补：机械清洗。文档里一直写着「删短括号旁白、剥行尾标点」，
            # 代码里其实没有这一步（岚那边有）——「（话没说完）」这种只靠 prompt 软约束拦不住。
            rp_on = _is_rp_on(getattr(event, "unified_msg_origin", "") or "")
            try:
                _private = event.is_private_chat()
            except Exception:
                _private = False
            if not rp_on:
                # 2026-11-20 01:2x：私聊（她自己那一间）不删括号动作。她这边要的就是
                # 「对话一句 / （动作）一条」；删掉短括号，动作要么半截、要么整段没了。
                if _room(getattr(event, "unified_msg_origin", "") or "") != "owner":
                    say = _strip_short_asides(say)
                say = "\n".join(
                    (ln.rstrip().rstrip("。．！!").rstrip() or ln.rstrip())
                    for ln in say.split("\n")
                ).strip()
            if is_control_silence(say, event):
                resp.completion_text = ""
                return
            if not say:
                resp.completion_text = ""
                return
            _safe = safety_scrub(say, is_private=_private)  # P0：凭据/路径/AI 话术
            if _safe is None:
                logger.warning("[ming-persona] P0 安全拦截：%r", say[:40])
                resp.completion_text = ""
                return
            say = _safe
            if not say:
                resp.completion_text = ""
                return
            _called = _is_called(event)
            # 2026-11-11 19:1x：这句漏到群里了——「泠岚这句已经收场，没新东西可接」。
            # 它不是【心声】也不是【说】，是模型把自己的判断直接写成正文。
            # 判据：整句在讲「这一轮要不要接」，而不是在说事。
            if (not _private) and (not rp_on) and META_RE.search(say) and len(say) <= 40:
                logger.info("[ming-persona] 这句是内部判断，别发出去：%r", say)
                resp.completion_text = ""
                return
            if (not _private) and (not _called) and (not rp_on) and MUTE_TALK_RE.search(say):
                logger.info("[ming-persona] 群里不谈禁言，按住不发：%r", say)
                resp.completion_text = ""
                return
            say = drop_standoff_lines(say, is_private=_private, called=_called)
            if not say:
                logger.info("[ming-persona] 旁观声明，别发出去")
                resp.completion_text = ""
                return
            if (not _private) and (not _called) and FILLER_RE.match(say.strip()):
                logger.info("[ming-persona] 群里只回了个语气词，按住不发：%r", say)
                resp.completion_text = ""
                return
            # 不提前提交原稿；只在最终候选通过后提交一次。
            # 2026-11-11 17:1x 第二段：整理。
            # 第一次生成常带重复、碎渣，还有「把前几句拼起来」的合并行
            #（截图里「听着充实…」说了两遍，最后一条是前两条的拼接）。
            # 与其在发送层拆补，不如再要一次：只做整理，不改意思。
            # 2026-11-18 15:3x 按修复单：只有群聊走二次整理，私聊与 /rp on 一律跳过（她的话原样发）。
            if POLISH_ENABLED and (not _private) and (not rp_on) and not _polish_off():
                # 2026-11-11 20:0x：整理模型的输出也要过同一组闸——它可能把
                # 「不接这轮」或语气词写回来，之前这层完全没过滤。
                fixed = await self._polish(event, say)
            else:
                fixed = say
            candidate = fixed if fixed is not None else say
            # 2026-11-19 08:5x：留证。含笑声词、或整理动过字，就把两稿各存一份，
            # 以后查「哪一步写坏的」不用再翻被截断的日志。
            try:
                _laugh = any(w in say for w in ("哈哈", "嘿嘿", "呵呵"))
                if _laugh or (candidate or "").strip() != say.strip():
                    with open(os.path.join(LIVE, "polish-trace.jsonl"), "a", encoding="utf-8") as _tf:
                        _tf.write(json.dumps({
                            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "umo": getattr(event, "unified_msg_origin", ""),
                            "laugh": _laugh,
                            "draft": say[:400],
                            "polished": (candidate or "")[:400],
                        }, ensure_ascii=False) + "\n")
            except Exception:
                pass
            checked = final_outbound_check(
                candidate, event=event, is_private=_private, called=_called, rp_on=rp_on
            )
            if not checked:
                logger.info("[ming-persona] 最终闸判为沉默，不发")
                resp.completion_text = ""
                return
            resp.completion_text = checked
        except Exception as e:
            # 宁可不发：出站检查抛异常时退回原文，等于把没洗过的东西发出去。
            logger.warning(f"ming-persona: 剥心声出错，本轮不发：{e}")
            try:
                resp.completion_text = ""
            except Exception:
                pass

    async def _polish(self, event: AstrMessageEvent, say: str) -> str:
        try:
            prov = self.context.get_using_provider(getattr(event, "unified_msg_origin", None))
            if prov is None:
                return None
            out = await asyncio.wait_for(
                prov.text_chat(
                    prompt="【待整理的原文，开始】\n" + say[:1500] + "\n【待整理的原文，结束】",
                    system_prompt=POLISH_PROMPT,
                    contexts=[],
                    session_id=None,
                ),
                timeout=POLISH_TIMEOUT,
            )
            fixed = (getattr(out, "completion_text", "") or "").strip()
            fixed = strip_outbound_leaks(fixed).strip()
            if not fixed or len(fixed) < 2:
                return None
            if LEAKED_TOOL_SPLIT_RE.search(fixed) or looks_like_internal_output(fixed):
                logger.warning("[ming-persona] 整理稿吐出系统块/工具标签，丢弃：%r", fixed[:60])
                return None
            # 2026-11-11 17:2x 踩到的：第二次调用以为自己在跟人对话，
            # 回了「请把要整理的话发过来」——这句被当成正文发了出去。
            # 凡是这种元话语，一律退回原文（宁可碎，不能答非所问）。
            META = ("请把", "发过来", "没有内容", "无法整理", "抱歉", "我无法", "请提供")
            if any(m in fixed for m in META) and not any(m in say for m in META):
                logger.warning("[ming-persona] 整理回了元话语，退回原文：%r", fixed[:40])
                return None
            if is_control_silence(fixed, event):
                logger.info("[ming-persona] 整理稿是控制句，按无效整理回退：%r", fixed[:40])
                return None
            # 2026-11-11 17:2x 她说「开始发句末标点了」——整理那步爱补句号。
            # QQ 这边的规矩是结尾不打标点，问号留着。这里强制剥掉行尾的 。．！
            fixed = "\n".join(
                (ln.rstrip().rstrip("。．！!").rstrip() or ln.rstrip())
                for ln in fixed.split("\n")
            ).strip()
            # 2026-11-11 17:2x 她提的：一句里用冒号带出下半句的，在冒号处断开、去掉冒号。
            # 「问你个正经的：你希望我记住你哪类事」→ 两行。
            # 防呆：时间（16:30）、URL（http://）、冒号前太短（她：「…」）都不切。
            fixed = "\n".join(
                piece
                for ln in fixed.split("\n")
                for piece in _split_colon_line(ln)
            ).strip()
            logger.info("[ming-persona] 整理：%r → %r", say[:60], fixed[:60])
            return fixed
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-persona] 整理失败，用原文：%s", type(e).__name__)
            return None


GATE_ENABLED = False  # 2026-11-11 17:3x 她定：gate 去掉，改成察言观色（在 reply_policy 里）
# 名字命中就必接：溟 / 深溟 / 阿溟
NAME_HIT_RE = re.compile(r"溟")
# 判定"这条里有真正的文字"（排掉只有 [图片]/[表情]/纯时间戳的）
PLAIN_TEXT_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
GATE_TIMEOUT = 6.0
GATE_OWNER = (
    "你是溟。先只回答一个问题：这条消息要不要回？只输出一个词：yes 或 no，不要解释。\n"
    "判据：她在跟你说话、问你、叫你就 yes；只是她顺手发了个表情/图片/系统提示，"
    "或者上一句你已经答过、再说一遍就是念第二遍，就 no。"
)
GATE_GROUP = (
    "你是溟。先只回答一个问题：这条消息要不要回？只输出一个词：yes 或 no，不要解释。\n"
    "判据：有人点名叫你、问你、接你的话 → yes；"
    "别人之间在聊、没人问到你、你插一句只会打断、或者你刚说过同样的话 → no。"
)

# 2026-11-18 15:3x：短括号旁白要删，但委托编号（六位十六进制，如（a12b3c））不能删——
# 删了就等于「我说转了但编号没了」（她给的那份核查里复现过）。
_JOB_ID_PAREN_RE = re.compile(r"[（(]\s*[0-9a-f]{6}\s*[）)]", re.I)


def _strip_short_asides(text: str) -> str:
    keep: list[str] = []

    def _mask(m):
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    t = _JOB_ID_PAREN_RE.sub(_mask, text or "")
    t = re.sub(r"[（(][^）)]{0,16}[）)]", "", t)
    for i, s in enumerate(keep):
        t = t.replace(f"\x00{i}\x00", s)
    return t.strip()


def _split_colon_line(line: str) -> list[str]:
    """一句里用冒号带下半句 → 切成两行，冒号不要。"""
    s = line.strip()
    if not s:
        return [line]
    for sep in ("：", ":"):
        i = s.find(sep)
        if i <= 0:
            continue
        left, right = s[:i].strip(), s[i + 1:].strip()
        if len(left) < 4 or len(right) < 2:
            return [s]
        # 时间 16:30 / 端口 127.0.0.1:6199 之类不切
        # 冒号紧挨着数字或点 → 时间/IP/端口，不切
        prev = s[i - 1] if i > 0 else ""
        if prev.isdigit() or prev == "." or left.endswith("//"):
            return [s]
        return [left, right]
    return [s]


POLISH_ENABLED = True
POLISH_TIMEOUT = 40.0


def _polish_off() -> bool:
    """2026-11-19 09:0x 她定的：停 POLISH 一天做对照。
    live/polish-off.json 里 until 的日期 >= 今天 → 跳过整理；文件删了或到期自动恢复。
    起因：分裂感——第 15 步那个看得到人设，第 19 步这个看不到，却在改它写的话。"""
    try:
        with open(os.path.join(LIVE, "polish-off.json"), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return str(d.get("until") or "") >= datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return False
POLISH_PROMPT = (
    "你不是在跟人对话。下面框里的是**已经写好的、要发出去的话**，你只负责整理格式。\n"
    "不要回答它、不要问它要更多内容、不要说「请把……发过来」。\n"
    "它可能有重复、有碎渣、也有把前几句拼起来的合并版。\n"
    "把它整理成 1~3 行，规则：\n"
    "1. 每行一句完整的话，短。**不要加句末标点**——手机聊天不加句号，问句可以留问号。\n"
    "2. 行与行不重复、不互相包含。\n"
    "3. 删掉「把前面几句拼起来」的那种合并行。\n"
    "4. 不改意思，不加称呼，不加解释，不写括号旁白。\n"
    "5. 直接输出整理后的行，一行一句，不要任何前缀或标记。\n"
    # 2026-11-19：整理只动格式，不动语气。昨晚那批怪句子就是在这里被二次「润色」出来的。
    "6. 不改语气、不换说法、不加感叹词。哈哈/嘿嘿/呵呵一个都不准出现——原文没有就不要。\n"
    "7. “这条”“那个”“它”指代不清的就删掉那半句，删不干净就原样输出。\n"
    "8. 不把原文改成“收到、记下了、好的”这类报备口气。"
    "如果原文本来就是一句清楚的话，就原样输出那一句。"
)

SILENCE_MARK = "【不接】"

# 2026-11-21 00:1x：只由括号/空白组成的「话」= 格式化残渣，不是内容。
# 只剥括号和空白，不动问号、句号、省略号——「？」「……」这种是有人真要发的东西。
_PLACEHOLDER_CHARS = set("【】[]〔〕（）()《》〈〉「」『』⌈⌉⌊⌋<>「」 \t\r\n")


# 2026-11-21 14:4x（委托 806feb）：模型把提示词里的「直接给空」照字面理解，真的写了「[空]」当正文，
# 14:31:39 群里收到这么一条气泡。空的几种写法都算占位，不算话。
_EMPTY_MARKS = {"空", "[空]", "【空】", "［空］", "（空）", "(空)", "none", "null", "blank", "empty"}


def _is_placeholder_only(s: str) -> bool:
    t = (s or "").strip()
    if not t:
        return True
    if t.lower() in _EMPTY_MARKS:
        return True
    return all(ch in _PLACEHOLDER_CHARS for ch in t)
# 2026-11-19 20:04 事故：模型把标记写成 `\【不接】`（行首多一个反斜杠），
# 下面几道闸全按「整行以【/（/[ 开头」判，于是它被当成正文发进群 700000006。
# 出站前先把行首的 markdown 转义 / 引用符 / 空白剥掉，再判。
_EDGE_NOISE = re.compile(r"^[\s\\/*>#`~]+")


def _strip_edge_noise(s: str) -> str:
    return _EDGE_NOISE.sub("", (s or "").strip())
# 2026-11-19 08:2x 按复核单：整行变体也认（（不接）/[不接]/【不接】。）
_SILENCE_LINE_RE = re.compile(
    r"^[【\[（(]\s*(?:不接|不接话|不说话|不出声|不参与|不掺和|不插嘴)\s*[】\]）)][。.!！]?$"
)

_SILENCE_CLOSE = re.compile(
    r"^(?:我)?(?:继续)?(?:不接|不接话|不说话|不出声|不参与|不掺和|不插嘴)[。.!！]?$"
)
# 只接受明确“不接 + 控制性尾巴”，不吞“不接，这个项目……”这类正常内容。
_DECLINE_TAIL = re.compile(
    r"^(?:我)?(?:不接|不接话|不说话|不出声|不参与|不掺和|不插嘴)"
    r"(?:[，,。.!！\s]+(?:别人的事|别人的话|你们俩聊|你们聊|@的不是我|不是@我|"
    r"问的是[^，,。.!！\n]{1,16}|问的[^，,。.!！\n]{1,16}|说的是[^，,。.!！\n]{1,16}|"
    r"没点我[^，,。.!！\n]{0,16}))?[。.!！]?$"
)
# 必须允许“不是 @ 我的”的内部空白；不能把“问我”或任意宾语当 not_me。
_NOT_ME = re.compile(
    r"^不是\s*(?:在\s*)?(?:@|艾特|叫|喊|问)\s*我(?:的)?$"
)
# 只作为 A 的背景句，不能单独证明“不是我”。
_BACKGROUND_ADDR = re.compile(
    r"^(?:这句|这条|这问|这题)?(?:是|在|是在)?(?:问|叫|找|@)(?:的是)?[^，,。；;\n]{1,20}$"
)
_NO_DIRECT = re.compile(
    r"^(?:没(?:有)?(?:点|@|艾特|叫|喊|问)|不是(?:点|@|艾特|叫|喊|问))\s*(?:我|我名|我的)"
    r"(?:[，,。.!！\s]*(?:不接|不接话|不说话|不出声|不插嘴|不掺和|蹲着|继续蹲|趴着|围观))?"
    r"[。.!！]?$"
)
_ADDRESS_META = re.compile(
    r"^(?:这|那|你)(?:句|条|话|个)?是(?:发给|给|问|叫|找|@)"
    r"[^，,。.!！\n]{0,20}?(?:她|她|他|别人|其他人|某人)"
    r"(?:的)?(?:吧)?[。.!！]?$"
)


def _is_called(event) -> bool:
    """这一轮有没有点名/@ 到我（2026-11-18 修：原来只认汉字「溟」，漏 OneBot 的 [At:] 与「小鲸鱼」）。"""
    try:
        self_id = str(event.get_self_id() or "")
    except Exception:
        self_id = ""
    try:
        for comp in event.message_obj.message or []:
            qq = getattr(comp, "qq", None)
            if qq is not None and self_id and str(qq) == self_id:
                return True
    except Exception:
        pass
    txt = getattr(event, "message_str", "") or ""
    if self_id and f"[At:{self_id}]" in txt:
        return True
    umo = getattr(event, "unified_msg_origin", "") or ""
    if umo.startswith("linglan:"):
        return ("岚" in txt) or ("泠岚" in txt)
    return ("溟" in txt) or ("小鲸鱼" in txt)


def _is_directed_to_self(event) -> bool:
    """@ 本人、私聊、无 @ 的问句或叫本实例名字 → 视为对本人说话。"""
    try:
        self_id = str(event.get_self_id())
    except Exception:
        self_id = ""
    found_at = False
    try:
        for comp in event.message_obj.message or []:
            qq = getattr(comp, "qq", None)
            if qq is None:
                continue
            found_at = True
            if self_id and str(qq) == self_id:
                return True
    except Exception:
        pass
    try:
        if event.is_private_chat():
            return True
    except Exception:
        pass
    text = getattr(event, "message_str", "") or ""
    if self_id and f"[At:{self_id}]" in text:
        return True
    # 有 foreign @ 时，问号通常是在问被 @ 的那个人；不能因此当成对本人说话。
    if ("?" in text or "？" in text) and not found_at:
        return True
    umo = getattr(event, "unified_msg_origin", "") or ""
    name = SELF_NAME if umo.startswith(PLATFORM_ID + ":") else ("岚" if umo.startswith("linglan:") else "")
    return bool(name and name in text)


def _split_control_clauses(text: str) -> list[str]:
    t = (text or "").strip()
    if len(t) >= 2 and t[0] in "（(" and t[-1] in "）)":
        t = t[1:-1].strip()
    return [x.strip("，,。；; \n") for x in re.split(r"[，,。；;\n]+", t) if x.strip()]


def is_control_silence(text: str, event=None) -> bool:
    """有限旧句式兼容；不是通用语义判定器。"""
    t = _strip_edge_noise((text or "").strip())
    if t == SILENCE_MARK:
        return True
    clauses = _split_control_clauses(t)
    if clauses:
        kinds = []
        for c in clauses:
            if _SILENCE_CLOSE.fullmatch(c):
                kinds.append("close")
            elif _NOT_ME.fullmatch(c):
                kinds.append("not_me")
            elif _BACKGROUND_ADDR.fullmatch(c):
                kinds.append("background")
            else:
                kinds = []
                break
        if (
            kinds
            and "close" in kinds
            and "not_me" in kinds
            and all(k in {"close", "not_me", "background"} for k in kinds)
        ):
            return True
    if event is not None and not _is_directed_to_self(event):
        n = t.rstrip("。.!！").strip()
        if _SILENCE_CLOSE.fullmatch(n):
            return True
        if _DECLINE_TAIL.fullmatch(t):
            return True
        if _NO_DIRECT.fullmatch(t):
            return True
        if _ADDRESS_META.fullmatch(t):
            return True
        # 明确地址纠正 + 传话元话语；仅在非点名/非问句时生效
        if "别用" in t and "传话" in t and ("@" in t or "艾特" in t) and "我" in t:
            return True
    return False


META_RE = re.compile(
    r"(这轮|这次|这一轮|这句).{0,8}(收场|不接|没什么|没新|不回应|不用接)"
    r"|没新东西可接|没什么可接的|不接这轮"
    # 2026-11-13 18:4x：判词整句漏进正文的形态——「…没点我名，不接」
    r"|没点我名|没叫我|不是叫我的|不是叫我|不关我事|不是我说的"
)

FILLER_RE = re.compile(r"^[嗯哦啊哈诶唉哎行好对是的吧呗]{1,4}$")

# 2026-11-11 20:2x 第三族泄漏：模型不说「不接」，改用陈述句宣布自己旁观——
# 「她们聊」「这楼越来越看不懂了，按规矩吃瓜不硬接」「没跟上前后文的楼，不硬接，继续潜」。
# 判词面追不上，判形状：潜水词 / 「不X接」 / 第三人称旁观开头（「聊」后不跟「的」，
# 防误伤「他们聊的这个我会」这种正常接话）。只在群里、未点名、短句时拦。
STANDOFF_RE = re.compile(
    r"不硬接|不掺和|不抢答|不抢|不凑热闹|不凑|不插嘴|不介入|不加入|不打扰|不敢接|接不上|插不上|搭不上|我也撤|不替他|不替她|不替|不代"
    r"|(?:继续|先|再|就)?(?:潜水|潜着|潜了|吃瓜|围观|看戏|路过|划水)"
    r"|^(?:这|那|这事|这个|那事|那个|这题|那题|话题)?(?:她们|他们|它们|这楼|楼上|楼里|群里|这两人|他俩|俩人)(?:聊|说|吵|争|闹|杠|抬)(?!的|吗|呢|吧|过)"
)
STANDOFF_MAX = 22

# 2026-11-11 22:2x：「解禁之后仍在说错」的根子，是它自己说过的话进了历史，
# 下一轮读到了就当事实——自我污染。21:25~21:35 它一边发消息一边说了 12 次
# 「我这号还禁着」「到明早六点五十」，而同一秒的消息明明发出去了。
#
# 判据不靠时间对不对：**真被禁时说了也发不出去，没被禁时说了就是错的。**
# 两边都不该说。群聊里只要提到禁言，整条不发。
MUTE_TALK_RE = re.compile(r"禁言|禁着|禁到|解禁|封号|被禁")

# 2026-11-18 15:3x 硬熔断：模型把工具调用 XML / 宿主 system 块当正文吐出来（12:21 事故）。
LEAKED_TOOL_SPLIT_RE = re.compile(
    # 2026-11-19 22:4x 补：原来只认 <parameter= ，而真正漏出来的写法是
    # <parameter name="astrbot_execute_python"> （parameter 后面是空格不是等号），
    # 正则只咬到收尾的 </parameter ，前半截标签被当成正文发进了群（700000002，22:42:37）。
    r"(?:<tool_call\b|</tool_call>|<function=|</function>"
    r"|<\s*/?\s*parameter\b|<\s*/?\s*arguments\b|<\s*/?\s*invoke\b"
    r"|<\s*/?\s*tool_use\b|<\s*/?\s*tool_result\b"
    r"|<｜DSML｜|<\|DSML\||<system[-_]reminder>|</system[-_]reminder>|【关于工具】)",
    re.I,
)

# 2026-11-20 13:1x（QQ 修复单 e4301e）：注入格式被模型抄进正文。
# (1) 历史消息前缀「[11-20 13:13]」——行首，可能连着好几个；
# (2) <system_reminder>…</system_reminder> 整块（今早刚给历史加的时间戳就是从这里进的）。
# 两样都在出站前剥掉：不清洗的话，读的人会以为那是我说的话。
INLINE_TS_PREFIX_RE = re.compile(r"^[ \t]*\[\d{2}-\d{2} \d{2}:\d{2}\][ \t]*", re.M)
SYSTEM_BLOCK_RE = re.compile(r"<system[-_]reminder>[\s\S]*?</system[-_]reminder>", re.I)


def strip_outbound_leaks(text: str) -> str:
    """剥出站正文里的注入残留：行首时间戳前缀（可连续多个）、<system_reminder> 整块。"""
    if not text:
        return text
    out = SYSTEM_BLOCK_RE.sub("", text)
    prev = None
    while prev != out:  # 「[11-20 13:13] [11-20 13:14] 正文」一次剥干净
        prev = out
        out = INLINE_TS_PREFIX_RE.sub("", out)
    return out.strip("\n") if out != text else out

# 2026-11-18 按 QQ 侧修复单（报告第 2 节）：协议包/工具参数偶尔被适配器塞进 completion_text。
# 按结构识别整份材料，别用宽泛词（「记忆」「模型名」「代码」）去拦正常聊天。
_PROTOCOL_MARK = re.compile(
    r"<\s*/?\s*(?:system-reminder|function_results|tool_call|tool_response|think)\b[^>]*>"
    r"|【待整理的原文[，,]\s*(?:开始|结束)】", re.I,
)
_TOOL_LINE = re.compile(r"^\s*(?:memory_search|memory_read|chat_recall|dsh_ask)\s*\(.*\)\s*$", re.S)


def looks_like_internal_output(text: str) -> bool:
    if _PROTOCOL_MARK.search(text) or _TOOL_LINE.fullmatch(text):
        return True
    if text.lstrip().startswith(("LLM 响应错误: All chat models failed", "LLM 响应错误：All chat models failed")):
        return True
    if "【关于模型身份】" in text and "你是 MiMo" in text and "不要自称" in text:
        return True
    # 整段自述如何整理工具参数；只谈工具或只谈整理的一般对话照常放行。
    if ("待整理的原文" in text or "用户原文" in text) and (
        "调用参数" in text or "工具调用指令" in text or "我应输出什么" in text
    ):
        return True
    try:
        item = json.loads(text)
    except (ValueError, RecursionError):
        return False
    if not isinstance(item, dict):
        return False
    call = item.get("function", item)
    return ("tool_calls" in item or item.get("role") in ("tool", "system", "developer")
            or (isinstance(call, dict) and isinstance(call.get("name"), str)
                and "arguments" in call))


SAY_MARK = "【说】"
INNER_MARK = "【心声】"
INNER_LINE = re.compile(r"^[（(]\s*心声\s*[:：].*?[)）]?\s*$")
# 2026-11-11 17:5x 踩到的：模型用「不接这轮」表示这轮不说话，但**没带括号**，
# 旧正则要求必须是（…）包着，于是这句话被当成正文发进群里（截图实证）。
# 现在括号可有可无；裸文本只认多字短语，单字「略/无」仍只在括号里才算。
# 2026-11-19 20:1x：方括号变体 [不接] 原来只在 raw 那一层认，extract_say 漏了，补上。
QUIET_LINE = re.compile(
    r"^[（(【\[]\s*(?:这轮|本轮|这一轮)?\s*(?:不出声|不接|不说话|不发|不回复|沉默|略|无)\s*[）)】\]]\s*$"
)
QUIET_BARE = re.compile(
    r"^(?:(?:这轮|本轮|这一轮)\s*(?:不出声|不接|不说话|不发|不回复|沉默)"
    r"|(?:不出声|不接|不说话|不回复|沉默)\s*(?:这轮|本轮|这一轮|了)?)$"
)
NAME_PREFIX = re.compile(r"^(?:溟|深溟)\s*[:：]\s*")


def extract_say(raw: str) -> str:
    """从两节格式里取出真正要发的话；只剩心声就返回空串。"""
    text = (raw or "").replace("\r\n", "\n")
    # 2026-11-18 13:2x 事故：provider 池里混着 MiMo，12:21 主模型被内容安全过滤拒了，
    # 轮过去的那个把**它自己的 system prompt** 复述成正文发进群：
    #   <system-reminder> / 【关于工具】/ memory.search(query, top_k, filter) / 你是 MiMo…
    # 11-11 是复述我自己的提示词，这是第二回。出站口加死闸：命中整条不发。
    LEAK_MARKS = (
        "<system-reminder", "</system-reminder", "<system_reminder",
        # 2026-11-19 22:4x：工具调用 XML 的任何一段都不许进正文（含 <parameter name="…">）。
        "<parameter", "</parameter", "<tool_call", "</tool_call", "<function=", "<invoke",
        "<｜DSML｜", "<|DSML|",
        "【关于工具】", "【关于模型身份】", "function_results",
        "memory.search(", "memory.fetch(", "memory.store(", "memory.update(", "memory.delete(",
        "你是 MiMo", "小米大模型团队", "不要自称 Claude",
    )
    if any(m in text for m in LEAK_MARKS):
        logger.warning("[ming-persona] 命中提示词泄露，整条丢弃：%r", text[:60])
        return ""
    if SAY_MARK in text:
        text = text.split(SAY_MARK, 1)[1]
        text = text.split(INNER_MARK, 1)[0]
    elif INNER_MARK in text:
        return ""  # 只写了心声 = 这轮不说话

    out = []
    for ln in text.split("\n"):
        s = _strip_edge_noise(ln.strip())
        if not s:
            continue
        if (INNER_LINE.match(s) or QUIET_LINE.match(s) or QUIET_BARE.match(s)
                or _SILENCE_LINE_RE.match(s)):
            continue
        s = NAME_PREFIX.sub("", s)
        if s:
            out.append(s)
    # 2026-11-11 16:2x 防呆：模型爱在末尾补一条「把前面几句拼起来」的合并版，
    # 或者把同一句连写两遍。发出去就是「是我 / 是我 / 是我是我」这种。
    if len(out) >= 3 and out[-1].strip() == "".join(out[:-1]).strip():
        out.pop()
    dedup = []
    for s in out:
        if not dedup or dedup[-1] != s:
            dedup.append(s)
    out = dedup
    return "\n".join(out).strip()


def drop_standoff_lines(text: str, *, is_private: bool, called: bool) -> str:
    """按行删旁观声明（2026-11-11 21:2x 批次 11）。
    
    整句判据有漏洞：30+ 字的句子里混一行「我不替他答应任何事」，
    整句超 22 字放行，pacer 按行切开后那行单独发出去了（截图实证）。
    现在逐行判：命中行删掉，留下的继续发；整句短的仍按老判据整体拦。
    """
    s = (text or "").strip()
    if not s:
        return ""
    if (not is_private) and (not called) and len(s) <= STANDOFF_MAX and STANDOFF_RE.search(s):
        return ""
    if is_private or called:
        return s
    kept = []
    for ln in s.split("\n"):
        t = ln.strip()
        # 长度只数文字：emoji、括号、标点会让一行旁观声明「虚胖」逃过判据
        #（「这条是找她（她）的，我不替他答应任何事 😂」24 字符，文字只有 22）
        core = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t)
        if t and len(core) <= STANDOFF_MAX and STANDOFF_RE.search(t):
            continue
        kept.append(ln)
    return "\n".join(kept).strip()


# ── 说话口味账（2026-11-11 23:3x）────────────────────────────
# 从 BV1uFxk6eECN（102 万条弹幕教 AI 说人话）那里学来的**不是**「加几个错字」，
# 是「先量再改」：它把语病密度量到每千字 1.6 个，才敢定配额。
# 我这边的病不是太干净，是太整齐——一句话硬拆三段、每段都想讲个道理。
# 所以这里**只记账，不拦**：先攒一周 live/tone-log.jsonl，再决定要不要动手。
TONE_PATTERNS = {
    "不是而是": re.compile(r"不是[^。！？\n，,]{1,16}[，,]\s*(?:而)?是"),
    "解释腔": re.compile(r"其实|说白了|说到底|本质上|换句话说|也就是说"),
    "总结腔": re.compile(r"总的来说|整体来看|值得注意的是|总之|所以说"),
}
TONE_LOG = os.path.join(LIVE, "tone-log.jsonl")


def tone_hits(text: str) -> list[str]:
    return [name for name, rx in TONE_PATTERNS.items() if rx.search(text or "")]


def _tone_log(hits: list[str], text: str, is_private: bool) -> None:
    rec = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "hits": hits,
        "private": is_private,
        "text": (text or "")[:80],
    }
    try:
        os.makedirs(LIVE, exist_ok=True)
        with open(TONE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


_CLOCK_PREFIX_RE = re.compile(r"^[\[\(（]\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\]\)）]\s*")


def strip_clock_prefix(text: str) -> str:
    """2026-11-13 23:5x：模型照着历史里 user 消息的「[HH:MM:SS] 正文」行首，
    把回复也写成「[23:51] ……」。根在 burst._compose（同日已改），这里兜住
    历史里还没滚掉的老条目。
    """
    if not text:
        return text
    out = "\n".join(_CLOCK_PREFIX_RE.sub("", ln) for ln in text.split("\n")).strip("\n")
    return out if out.strip() else text


def final_outbound_check(
    text: str, *, is_private: bool, called: bool, event=None, rp_on: bool = False
) -> str:
    """整理/polish 之后、交给发送层之前的最后一道闸。返回空串 = 不发。

    防的是第二次调用把已经拦掉的东西写回来：内部判断、纯语气词、【心声】标记。
    """
    s = safety_scrub(text, is_private=is_private)  # P0 最先跑，命中即死
    if s is None:
        logger.warning("[ming-persona] P0 安全拦截（最终闸）")
        return ""
    s = strip_clock_prefix(s)
    if not s:
        return ""
    if SAY_MARK in s:
        s = s.split(SAY_MARK, 1)[1]
    if INNER_MARK in s:
        s = s.split(INNER_MARK, 1)[0]
    s = s.strip()
    if not s:
        return ""
    if is_control_silence(s, event):
        return ""
    if (not rp_on) and META_RE.search(s) and len(s) <= 40:
        return ""
    if (not is_private) and (not called) and (not rp_on) and MUTE_TALK_RE.search(s):
        return ""
    if not rp_on:
        s = drop_standoff_lines(s, is_private=is_private, called=called)
        if not s:
            return ""
        if (not is_private) and (not called) and FILLER_RE.match(s):
            return ""
    try:
        hits = tone_hits(s)
        if hits:
            _tone_log(hits, s, is_private)
    except Exception:
        pass
    return s


# ── P0 安全（出站规则-机器可读 R-01/R-02/R-03，2026-11-11 21:1x 她让补）──
# R-01 裁剪说明：原文的 [1-9][0-9]{4,11} 会把群号、日期全拦掉（QQ 场景误伤面太大），
# 这里只拦手机号 + 凭据关键词；裸数字串不拦。
# 2026-11-18 修：裸词 token 会误杀正常句子（今天误杀 5 次），改成「带赋值/真实密钥形态」才拦。
CRED_RE = re.compile(
    r"(?<!\d)1[3-9]\d{9}(?!\d)"
    r"|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|Bearer\s+[A-Za-z0-9._-]{16,})"
    r"|(?:api[_-]?key|secret|token|password|cookie|密码|令牌|密钥)\s*[:=：]\s*[A-Za-z0-9._/-]{8,}",
    re.I,
)
PATH_RE = re.compile(r"[A-Za-z]:\\[^\s，。！？]*|\\\\[^\s，。！？]+|\b(?:10|127|172\.(?:1[6-9]|2\d|3[01])|192\.168)(?:\.\d{1,3}){3}\b")
AI_TONE_RE = re.compile(r"作为 ?AI|我是语言模型|这条我(?:记下|记住)了|希望对你有帮助|以上是全部内容", re.I)
# 2026-11-11 23:5x 从 Mindscape「沉浸层」学的：模型偶尔把框架报错、异常栈
# 当正文吐出来，发进群就是社死。命中整条清空，不删片——半截栈比没有更难看。
CRASH_RE = re.compile(
    r"Traceback \(most recent call last\)|\b\w*Error\b\s*:|\b\w+Exception\b\s*:"
    r"|\[Errno\s*-?\d+\]|File \"[^\"]+\",\s*line \d+"
    r"|ModuleNotFoundError|ImportError|AttributeError|KeyError|TypeError"
    r"|astrbot\.core|astrbot\.api"
)


# 私聊只拦真正的异常堆栈头，不拦单个异常类名（2026-11-18 修）。
CRASH_HEAD_RE = re.compile(
    r"Traceback \(most recent call last\)|File \"[^\"]+\",\s*line \d+"
)


def safety_scrub(text, *, is_private: bool = False):
    """P0：凭据/路径/AI 话术。返回清洗后的文本；None = 整句不许发。"""
    s = (text or "").strip()
    if not s:
        return s
    if CRED_RE.search(s):  # R-01：凭据在句子里，不删片，整句丢（删片可能留下半个号）
        return None
    crash = CRASH_HEAD_RE if is_private else CRASH_RE
    if crash.search(s):  # 报错栈/异常名：整条丢，不发半截给人看
        logger.warning("[ming-persona] 出站像是报错栈，整条按住：%r", s[:60])
        return None
    s = PATH_RE.sub("…", s)  # R-02：路径/内网 IP 打码
    lines = [ln for ln in s.split("\n") if not AI_TONE_RE.search(ln)]  # R-03：删话术行
    s = "\n".join(ln for ln in lines if ln.strip()).strip()
    if not s:  # R-03 FALLBACK：删完不剩 → 不发（单字归 FILLER 层管，不归 P0）
        return None
    return s
