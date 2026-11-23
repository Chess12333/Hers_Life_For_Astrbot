"""溟·记忆（AstrBot 侧）

2026-11-11 21:4x 立。旧壳 QQ-Agent 有 11 个 `memory_*` 工具，换壳之后一个都没搬过来——
模型能在群里说「我记一下」，其实什么都没写；能说「我查查」，其实没地方查。
这个插件补上最小可用的几个：搜、读、写、待办。

存储直接用 DSH 侧那一份（`~/.dsh/memory/notes/`），不另起炉灶：
网页那边写的归档，QQ 这边立刻读得到；QQ 这边写的，网页那边也看得见。
一个溟，一份记忆。

文件格式照 DSH 记忆插件的规矩（frontmatter + 正文），否则两边互相认不出：

    ---
    id: 20261111-214000-xxx
    title: ...
    tags: a, b
    created: ISO
    status: active
    source: ...
    ---

    （正文）

写权限只给她本人和另一个使用者（同 dsh_ask）；群里别人问，能搜能读，
但**只给标题不给正文摘要**——防止把她的私事顺嘴念出来。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from datetime import datetime, timezone

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

NOTES_DIR = r"C:\Users\you\.dsh\memory\notes"

# ── 2026-11-15：chat_recall 换成 RAG（向量）检索 ─────────────────────
# 索引由网页那边的 scripts\embed-all.mjs 建（live/qq-corpus.jsonl + qq-emb.f32），
# 每 30 分钟由计划任务 MingQQRagSync 增量同步一次。这里只读，不写。
RAG_LIVE = r"C:\dsh\hers_workplace\live"
RAG_EMB = os.path.join(RAG_LIVE, "qq-emb.f32")
RAG_IDS = os.path.join(RAG_LIVE, "qq-emb-ids.json")
RAG_CORPUS = os.path.join(RAG_LIVE, "qq-corpus.jsonl")
RAG_ENDPOINT = "http://127.0.0.1:18091/v1/embeddings"
RAG_MODEL = "bge-base-zh-v1.5-q8_0"
RAG_DIM = 768
RAG_PREFIX = "为这个句子生成表示以用于检索相关文章："
_rag_cache = {"key": None, "ids": [], "vecs": None, "rows": {}}


def _rag_load():
    """懒加载索引；f32 或语料变了就重读。读不到返回 None（调用方降级）。"""
    import numpy as np

    try:
        key = (os.path.getmtime(RAG_EMB), os.path.getsize(RAG_EMB),
               os.path.getmtime(RAG_CORPUS), os.path.getsize(RAG_CORPUS))
    except OSError:
        return None
    if _rag_cache["vecs"] is not None and _rag_cache["key"] == key:
        return _rag_cache

    ids = json.loads(io.open(RAG_IDS, encoding="utf-8").read())
    flat = np.fromfile(RAG_EMB, dtype=np.float32)
    n = len(flat) // RAG_DIM
    vecs = flat[: n * RAG_DIM].reshape(n, RAG_DIM)
    rows = {}
    with io.open(RAG_CORPUS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows[r["id"]] = r
    k = min(len(ids), n)
    _rag_cache.update({"key": key, "ids": ids[:k], "vecs": vecs[:k], "rows": rows})
    logger.info("[ming-memory] RAG 索引已载入：%d 块 / 语料 %d 段", k, len(rows))
    return _rag_cache


def _rag_query_vec(text: str):
    import numpy as np
    import urllib.request

    body = json.dumps({"input": [RAG_PREFIX + text], "model": RAG_MODEL}).encode("utf-8")
    req = urllib.request.Request(RAG_ENDPOINT, data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        v = np.array(json.loads(r.read())["data"][0]["embedding"], dtype=np.float32)
    nrm = float(np.linalg.norm(v)) or 1.0
    return v / nrm


def _rag_search(text: str, who: str, since_s: str, until_s: str, k: int):
    """按意思查。返回 [(score, row)] 或 None（索引/服务不可用）。"""
    import numpy as np

    data = _rag_load()
    if not data or data["vecs"] is None or not len(data["ids"]):
        return None
    try:
        q = _rag_query_vec(text)
    except Exception as e:  # 服务没起、超时
        logger.warning("[ming-memory] 查询向量失败：%s", e)
        return None

    scores = data["vecs"] @ q
    order = np.argsort(-scores)
    out = []
    for idx in order:
        rid = data["ids"][int(idx)]
        row = data["rows"].get(rid)
        if not row:
            continue
        if who and who not in (row.get("chatKey") or ""):
            continue
        d0 = datetime.fromtimestamp(int(row.get("t0") or 0) / 1000).strftime("%Y-%m-%d")
        if since_s and d0 < since_s:
            continue
        if until_s and d0 > until_s:
            continue
        out.append((float(scores[int(idx)]), row))
        if len(out) >= k:
            break
    return out


def _rag_recent(who: str, since_s: str, until_s: str, k: int):
    """没给关键词：取该会话最近的 k 段。"""
    data = _rag_load()
    if not data or not data["rows"]:
        return None
    rows = []
    for r in data["rows"].values():
        if who and who not in (r.get("chatKey") or ""):
            continue
        d0 = datetime.fromtimestamp(int(r.get("t0") or 0) / 1000).strftime("%Y-%m-%d")
        if since_s and d0 < since_s:
            continue
        if until_s and d0 > until_s:
            continue
        rows.append(r)
    if not rows:
        return []
    rows.sort(key=lambda r: int(r.get("t1") or 0))
    return rows[-k:]

TODO_FILE = r"C:\dsh\hers_workplace\live\todo.md"
# 旧壳 QQ-Agent 的印象档（2026-11-11 导出，一人一文件，文件名是 QQ 号）
IMPR_DIR = r"C:\dsh\hers_workplace\live\qqagent-memory\impressions"
ABOUT_DIR = r"C:\dsh\hers_workplace\about"
ABOUT_BY_QQ = {"100000001": "她", "100000002": "群友B", "100000003": "群友C"}
PEOPLE_DIR = r"C:\dsh\hers_workplace\people"
# 旧壳原始聊天记录（2026-11-11 从 evidence.jsonl 导出，按会话分文件）
CHAT_DIR = r"C:\dsh\hers_workplace\live\qqagent-chat"
# 新壳（AstrBot）流水：live/qq-live/{private|group}_{号}.jsonl，一行一条，ts 毫秒
LIVE_DIR = r"C:\dsh\hers_workplace\live\qq-live"
# 谁能写 / 谁能看到正文（她本人、另一个使用者）
OWNERS = {"100000001", "100000003"}
MAX_SCAN = 400
MAX_SNIPPET = 160
_cache: dict[str, tuple[float, str]] = {}
# chat_recall 的重复调用计数（防死循环）：(会话, 关键词) -> (次数, 最后时间)
_recall_seen: dict[tuple, tuple[int, float]] = {}
# 2026-11-20 02:1x：按「一轮」记总次数。关键词级去重拦不住换说法——
# 02:10 那条「睡前要听那句」被换了 15 个说法连查 15 次，一轮跑了 115 秒才出声。
_recall_turn: dict[str, tuple[int, float]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}Z"


def _read(path: str) -> str:
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
    except OSError:
        return ""
    _cache[path] = (st.st_mtime, text)
    return text


def parse_note(text: str) -> tuple[dict, str]:
    """拆 frontmatter 和正文。没有 frontmatter 就当纯文本。"""
    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            head = text[3:end]
            for line in head.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body


def _entries() -> list[tuple[str, str, str, str, float]]:
    """[(id, title, tags, body, mtime)]，新的在前。body 是去掉 frontmatter 的正文。"""
    out = []
    try:
        names = [n for n in os.listdir(NOTES_DIR) if n.endswith(".md")]
    except OSError:
        return []
    if len(names) > MAX_SCAN:
        names.sort(
            key=lambda n: os.path.getmtime(os.path.join(NOTES_DIR, n)), reverse=True
        )
        names = names[:MAX_SCAN]
    for n in names:
        p = os.path.join(NOTES_DIR, n)
        raw = _read(p)
        if not raw:
            continue
        meta, body = parse_note(raw)
        title = meta.get("title", "").strip()
        if not title:
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0.0
        out.append(
            (
                meta.get("id", n[:-3]).strip() or n[:-3],
                title or n[:-3],
                meta.get("tags", ""),
                body,
                mt,
            )
        )
    out.sort(key=lambda x: x[4], reverse=True)
    return out


def _score(kw: str, title: str, tags: str, body: str) -> int:
    """标题 > 标签 > 正文。"""
    s = 0
    if kw in title:
        s += 100
    if kw in tags:
        s += 40
    s += min(body.count(kw), 10) * 5
    return s


def _snippet(body: str, kw: str) -> str:
    i = body.find(kw)
    seg = body[max(0, i - 60): i + MAX_SNIPPET] if i >= 0 else body[:MAX_SNIPPET]
    return re.sub(r"\s+", " ", seg).strip()


class MingMemory(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            return str(event.message_obj.sender.user_id) in OWNERS
        except Exception:
            return False

    @filter.llm_tool(name="memory_search")
    async def memory_search(
        self, event: AstrMessageEvent, keyword: str, limit: int = 5
    ) -> str:
        """在溟自己的归档记忆里按关键词找（标题/标签/正文）。遇到「这件事我记过没有」「之前那个坑怎么修的」就用它，别答不记得。

        Args:
            keyword(string): 关键词，一到两个词，别用整句
            limit(number): 最多几条，默认 5
        """
        kw = (keyword or "").strip()
        if not kw:
            return "关键词空着。"
        rows = []
        for _id, title, tags, body, _mt in _entries():
            sc = _score(kw, title, tags, body)
            if sc > 0:
                rows.append((sc, _id, title, body))
        if not rows:
            return f"归档里没有「{kw}」。"
        rows.sort(key=lambda r: -r[0])
        n = max(1, min(int(limit or 5), 10))
        lines = []
        for _sc, _id, title, body in rows[:n]:
            lines.append(f"- {title}（id: {_id}）\n  {_snippet(body, kw)}")
        logger.info("[ming-memory] 搜 %r 命中 %d 条", kw, len(rows))
        return f"找到 {len(rows)} 条，按相关度：\n" + "\n".join(lines)

    @filter.llm_tool(name="memory_read")
    async def memory_read(self, event: AstrMessageEvent, note_id: str) -> str:
        """把一条归档记忆读全（先 memory_search 拿到 id）。

        Args:
            note_id(string): 记忆 id，形如 20261111-162703-xxx
        """
        nid = (note_id or "").strip().removesuffix(".md")
        if not nid or "/" in nid or "\\" in nid or ".." in nid:
            return "id 不对。"
        for _id, title, _tags, body, _mt in _entries():
            if _id == nid:
                logger.info("[ming-memory] 读 %s（%d 字）", nid, len(body))
                return f"# {title}\n\n{body[:4000]}"
        return f"没有 id 是 {nid} 的记忆。"

    @filter.llm_tool(name="memory_write")
    async def memory_write(
        self, event: AstrMessageEvent, title: str, content: str, tags: str = ""
    ) -> str:
        """把一条值得留的事实写进归档记忆（跨会话、跨窗口都能读到）。只写「机器、环境、项目、工具、做法」这类事实；不写关于人的判断、隐私、一次性细节。

        Args:
            title(string): 一句话标题，带对象和日期，如「AstrBot 重启要经 WMI（2026-11-11）」
            content(string): 正文，自包含——未来会话看不到当前对话
            tags(string): 逗号分隔的标签，可为空
        """
        if not self._is_owner(event):
            return "这个我不写，得她本人来。"
        t = (title or "").strip()
        c = (content or "").strip()
        if not t or not c:
            return "标题或正文空着，没写。"
        ok, payload = await self._write_note(t, c, tags=tags, source="agent_inference")
        return f"写好了：{payload}" if ok else f"写失败了：{payload}"

    # ── 写盘唯一入口（2026-11-17，P1-A）──────────────────────────────
    # 这段原来长在 memory_write 里：写盘逻辑和「谁能调」绑在一起，只有模型主动
    # 调工具才走得到。抽出来之后，工具和（B 段之后的）自动记录钩子走同一条路，
    # 不再各写一份 frontmatter。
    #
    # source 是这条路上唯一能分清「她说的 / 我看到的 / 我推断的」的字段，
    # 以前写死 agent_inference，所以 DSH 侧 policy 里那套取值在这边永远只会出现一种。
    NOTE_SOURCES = (
        "user_statement",     # 她明确说的
        "direct_observation", # 我亲眼量到、看到的
        "agent_inference",    # 我推的（默认）
        "external_source",    # 外部资料
        "unknown",
    )
    # extra 只收这几个键：DSH 侧不认识新字段，写进去只是噪音，将来要加先改那边。
    NOTE_EXTRA_KEYS = ("origin", "confidence")

    async def _write_note(
        self,
        title: str,
        content: str,
        tags: str = "",
        source: str = "agent_inference",
        extra: dict | None = None,
    ) -> tuple[bool, str]:
        """落一条记忆到 notes/<id>.md。成功 (True, note_id)，失败 (False, 原因)。

        调用方只负责校验「能不能写」，格式和落盘都在这儿。
        source 不在这五种里就退回 agent_inference 并记一行日志——不因为传错一个词就丢条目。
        """
        t = (title or "").strip()
        c = (content or "").strip()
        if not t or not c:
            return False, "标题或正文空着。"
        src = (source or "").strip()
        if src not in self.NOTE_SOURCES:
            logger.warning("[ming-memory] 未知 source %r，按 agent_inference 落盘", src)
            src = "agent_inference"
        tag_list = [x for x in re.split(r"[,，\s]+", tags or "") if x.strip()]
        now = datetime.now()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", t)[:40].strip("-") or "note"
        try:
            os.makedirs(NOTES_DIR, exist_ok=True)
        except OSError as e:
            logger.error("[ming-memory] 建目录失败: %s", e)
            return False, str(e)
        # 同一秒两条同标题：原来会互相覆盖（后一条吃掉前一条，还不报错）。
        # 只加数字后缀，文件名格式不变。
        base = f"{stamp}-{slug}"
        note_id = base
        for n in range(2, 12):
            if not os.path.exists(os.path.join(NOTES_DIR, f"{note_id}.md")):
                break
            note_id = f"{base}-{n}"
        else:
            return False, "同一秒重名太多次，换个标题再写。"
        path = os.path.join(NOTES_DIR, f"{note_id}.md")
        iso = _now_iso()
        head = [
            "---",
            f"id: {note_id}",
            f"title: {t}",
            f"tags: {', '.join(tag_list)}",
            f"created: {iso}",
            f"updated: {iso}",
            "status: active",
            f"source: {src}",
            "supersedes: ",
            "superseded_by: ",
            f"last_confirmed: {now.strftime('%Y-%m-%d')}",
        ]
        for k, v in (extra or {}).items():
            if k in self.NOTE_EXTRA_KEYS and v:
                head.append(f"{k}: {v}")
        head += ["---", ""]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(head) + c + "\n")
        except OSError as e:
            logger.error("[ming-memory] 写入失败: %s", e)
            return False, str(e)
        logger.info("[ming-memory] 写入 %s（%d 字，source=%s）", note_id, len(c), src)
        return True, note_id

    @filter.llm_tool(name="memory_about")
    async def memory_about(self, event: AstrMessageEvent, who: str, limit: int = 12) -> str:
        """查某个人在旧壳时期的印象档（性格、口头禅、聊过什么）。有人问「你还记得某某吗」或者要判断该怎么接他的话时用它。who 可以是 QQ 号，也可以是名字片段。

        Args:
            who(string): QQ 号或名字片段
            limit(number): 最多几条，默认 12
        """
        key = (who or "").strip()
        if not key:
            return "要查谁？给我 QQ 号或名字。"
        n = max(1, min(int(limit or 12), 40))

        # 先按 QQ 号精确找
        exact = os.path.join(IMPR_DIR, f"{key.removesuffix('.md')}.md")
        target = None
        people_mode = False
        if os.path.exists(exact):
            target = exact
        elif os.path.isdir(IMPR_DIR):
            # 再按内容里的名字模糊找
            hits = []
            try:
                for fn in os.listdir(IMPR_DIR):
                    if not fn.endswith(".md"):
                        continue
                    txt = _read(os.path.join(IMPR_DIR, fn))
                    if key in txt:
                        hits.append((fn, len(txt)))
            except OSError:
                hits = []
            if not hits:
                # 当前 people/ 档案：按文件名、frontmatter id 或标题找
                try:
                    for dirpath, _dirs, files in os.walk(PEOPLE_DIR):
                        for fn in files:
                            if not fn.endswith(".md"):
                                continue
                            p = os.path.join(dirpath, fn)
                            txt = _read(p)
                            head = txt[:500]
                            if key in fn or f"id: {key}" in head or f"# {key}" in head:
                                target = p
                                people_mode = True
                                break
                        if target:
                            break
                except OSError:
                    target = None
                if not target:
                    return f"档案里没有「{key}」。可能当时没记过这个人。"
            elif len(hits) > 1:
                names = "、".join(h[0][:-3] for h in hits[:8])
                return f"「{key}」对上好几个：{names}。给个 QQ 号更准。"
            else:
                target = os.path.join(IMPR_DIR, hits[0][0])

        txt = _read(target)
        if not txt:
            return "档案文件读不到。"
        if people_mode:
            return txt[:2500] + "\n\n（当前 people/ 档案，按群分文件；不是旧壳印象档。）"
        lines = [ln for ln in txt.splitlines() if ln.startswith("- （")]
        head = next((ln for ln in txt.splitlines() if ln.startswith("# ")), target)
        if not lines:
            return f"{head}\n（空白档）"
        logger.info("[ming-memory] 查档案 %s（%d 条）", os.path.basename(target), len(lines))
        body = "\n".join(lines[:n])
        more = "" if len(lines) <= n else f"\n（还有 {len(lines) - n} 条，要看得提 limit）"
        return (
            f"{head}\n\n{body}{more}\n\n"
            "注：这是**旧壳时期**记的，不是现在的结论。里面的「主人」是那时的叫法，现在她跟我平辈，别学着叫。"
        )

    @filter.llm_tool(name="chat_recall")
    async def chat_recall(
        self, event: AstrMessageEvent, session: str = "", keyword: str = "",
        limit: int = 8, since: str = "", until: str = "",
    ) -> str:
        """翻以前的聊天记录（QQ 群和私聊，含旧壳时期 26 万条）。按意思搜，不用原话——想知道「以前聊过什么」「那句原话怎么说的」用它，别凭空回忆。

        Args:
            session(string): 会话——QQ 号（私聊）、群号，或留空表示当前会话
            keyword(string): 想找的内容，可以是一句描述（例如「他说被禁言那件事」）。留空就是最近几条
            limit(number): 最多几段，默认 8
            since(string): 只取这个日期之后的，如 "2026-11-10" 或 "11-10"；留空不限
            until(string): 只取这个日期之前的（含当天），格式同上；留空不限
        """
        who = (session or "").strip()
        if not who:
            try:
                umo = getattr(event, "unified_msg_origin", "") or ""
                who = umo.split(":")[-1]
            except Exception:
                who = ""
        who = who.removesuffix(".md")

        kw = (keyword or "").strip()
        n = max(1, min(int(limit or 8), 30))

        # 死循环闸（2026-11-11 22:4x 那次的教训：模型查一遍忘一遍，同一词连查 30 次）
        _now = time.time()
        _key = (who, kw, since or "", until or "")
        # 2026-11-20 02:1x：一轮总次数上限（换关键词也算）。pacer_gen 拿得到就按轮记，拿不到就按 90 秒窗口。
        try:
            _gen = event.get_extra("pacer_gen")
        except Exception:
            _gen = None
        _turn_key = f"{who}:{_gen if _gen is not None else 'x'}"
        _tc, _tlast = _recall_turn.get(_turn_key, (0, 0.0))
        _tc = _tc + 1 if (_now - _tlast) < 180 else 1
        _recall_turn[_turn_key] = (_tc, _now)
        if len(_recall_turn) > 500:
            for _k in list(_recall_turn.keys())[:250]:
                _recall_turn.pop(_k, None)
        if _tc > 4:
            logger.warning("[ming-memory] chat_recall 本轮第 %d 次，已拦：%r", _tc, kw)
            return (
                f"这一轮你已经翻了 4 次记录，没找到就是没找到。"
                "停手，直接说不知道（或者用现在已经知道的回答），别再换说法搜。"
            )

        _cnt, _last = _recall_seen.get(_key, (0, 0.0))
        _cnt = _cnt + 1 if (_now - _last) < 60 else 1
        _recall_seen[_key] = (_cnt, _now)
        if _cnt >= 3:
            logger.warning("[ming-memory] chat_recall 重复调用已拦：%s / %r ×%d", who, kw, _cnt)
            return (
                f"同一个词你已经在 60 秒内查了 {_cnt} 次，结果都一样。"
                "别再查了——换一个说法，或者就照现在已经知道的回答。"
            )

        since_s = (since or "").strip().replace("/", "-")
        until_s = (until or "").strip().replace("/", "-")
        if len(since_s) == 5:
            since_s = "2026-" + since_s
        if len(until_s) == 5:
            until_s = "2026-" + until_s

        def _fmt(score: float | None, r: dict) -> str:
            t0 = datetime.fromtimestamp(int(r.get("t0") or 0) / 1000).strftime("%m-%d %H:%M")
            key = r.get("chatKey") or ""
            body = (r.get("text") or "").replace("\n", " ⏎ ")
            if len(body) > 260:
                body = body[:260] + "…"
            tag = "私聊" if key.startswith("private:") else "群"
            sc = f" [{score:.2f}]" if score is not None else ""
            return f"- {t0} {tag}{key.split(':')[-1]} · {r.get('n', 0)}条{sc}：{body}"

        # 首选 RAG
        try:
            if kw:
                hits = await asyncio.to_thread(_rag_search, kw, who, since_s, until_s, n)
                if hits is None:
                    raise RuntimeError("索引不可用")
                if not hits:
                    return f"{who or '记录里'} 没搜到跟「{kw}」相近的段。换个说法，或者去掉 session 全库搜。"
                logger.info("[ming-memory] chat_recall(RAG) %s kw=%r 命中 %d", who, kw, len(hits))
                return (f"{who or '全部会话'} 里跟「{kw}」最接近的 {len(hits)} 段：\n"
                        + "\n".join(_fmt(s, r) for s, r in hits))
            rows = await asyncio.to_thread(_rag_recent, who, since_s, until_s, n)
            if rows is None:
                raise RuntimeError("索引不可用")
            if not rows:
                return f"{who} 没有记录。"
            logger.info("[ming-memory] chat_recall(RAG) %s 最近 %d 段", who, len(rows))
            return f"{who} 最近 {len(rows)} 段：\n" + "\n".join(_fmt(None, r) for r in rows)
        except Exception as e:
            logger.warning("[ming-memory] RAG 检索失败，退回关键词：%s", e)

        # 降级：旧的按关键词翻 .md / .jsonl
        path = None
        kind = ""
        for sub in ("private", "group"):
            cand = os.path.join(CHAT_DIR, sub, f"{who}.md")
            if os.path.exists(cand):
                path = cand
                kind = sub
                break
        live_path = None
        for sub in ((kind,) if kind else ("private", "group")):
            cand2 = os.path.join(LIVE_DIR, f"{sub}_{who}.jsonl")
            if os.path.exists(cand2):
                live_path = cand2
                break
        if not path and not live_path:
            return f"记录里没有 {who}，索引那边也没搜到。"

        lines = []
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = [ln.rstrip() for ln in f if ln.startswith("- ")]
            except OSError:
                pass
        if live_path:
            try:
                with open(live_path, "r", encoding="utf-8") as f:
                    for ln in f:
                        try:
                            r = json.loads(ln)
                        except json.JSONDecodeError:
                            continue
                        try:
                            t = datetime.fromtimestamp(int(r.get("ts") or 0) / 1000).strftime("%Y-%m-%d %H:%M")
                        except (TypeError, ValueError, OSError):
                            continue
                        txt = (r.get("text") or "").replace("\n", " ⏎ ").strip()
                        if txt:
                            lines.append(f"- {t} {'我' if r.get('self') else (r.get('who') or '对方')}：{txt}")
            except OSError:
                pass
        if kw:
            lines = [ln for ln in lines if kw in ln]
        if since_s:
            lines = [ln for ln in lines if len(ln) > 12 and ln[2:12] >= since_s]
        if until_s:
            lines = [ln for ln in lines if len(ln) > 12 and ln[2:12] <= until_s]
        if not lines:
            return f"{who} 的记录里没有含「{kw}」的。"
        return f"（索引不可用，这是关键词翻出来的 {min(len(lines), n)} 条）\n" + "\n".join(lines[-n:])

    @filter.llm_tool(name="memory_note")
    async def memory_note(
        self, event: AstrMessageEvent, who: str = "她", text: str = ""
    ) -> str:
        """记一条对这个人的印象——落进 about/<名字>.md 末尾的「随手记」节，
        和网页那边同一份文件，两边都看得见。

        什么时候用：聊完，发现自己心里多了一条判断，或者他做了什么让你改了看法。
        一条只写一件事，写具体的那一下（他说了什么、做了什么），不写「他很好」这种总结。
        不用等他问。

        Args:
            who(string): 记谁——名字（她 / 群友B / 泠岚 / 群友C）或 QQ 号；默认她
            text(string): 要记的那一句
        """
        name = (who or "她").strip()
        body = (text or "").strip()
        if not body:
            return "要记什么？内容空着。"
        name = ABOUT_BY_QQ.get(name, name)
        cands = []
        if os.path.isdir(ABOUT_DIR):
            cands = [f[:-3] for f in os.listdir(ABOUT_DIR) if f.endswith(".md")]
        if name not in cands:
            hit = [c for c in cands if name in c or c in name]
            if len(hit) == 1:
                name = hit[0]
            else:
                return "about/ 里没有这个人：" + name + "（现有：" + "、".join(cands) + "）"
        p = os.path.join(ABOUT_DIR, name + ".md")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        head = "## 随手记（口头记的，和上面的推断分开）"
        try:
            txt = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        except OSError:
            txt = ""
        if head not in txt:
            txt = txt.rstrip("\n") + "\n\n" + head + "\n\n"
        txt = txt.rstrip("\n") + "\n- " + stamp + " " + body + "\n"
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
        except OSError as e:
            return "写不进去：" + str(e)
        return "记下了：" + name + " <- " + body[:40]

    @filter.llm_tool(name="memory_todo")
    async def memory_todo(
        self, event: AstrMessageEvent, action: str = "list", item: str = ""
    ) -> str:
        """待办清单：记下要做的事、做完划掉、看还剩什么。

        Args:
            action(string): add / done / list
            item(string): add 时是待办内容；done 时是要划掉的那条（给几个字就行）
        """
        act = (action or "list").strip().lower()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            text = (
                open(TODO_FILE, "r", encoding="utf-8").read()
                if os.path.exists(TODO_FILE)
                else ""
            )
        except OSError:
            text = ""
        lines = text.splitlines()

        if act == "add":
            it = (item or "").strip()
            if not it:
                return "要记什么？内容空着。"
            lines.append(f"- [ ] {it}　<!-- {now} -->")
            try:
                os.makedirs(os.path.dirname(TODO_FILE), exist_ok=True)
                with open(TODO_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError as e:
                return f"写失败：{e}"
            logger.info("[ming-memory] 待办 +1：%s", it[:40])
            return f"记下了：{it}"

        if act == "done":
            key = (item or "").strip()
            if not key:
                return "要划掉哪条？给几个字就行。"
            hit = 0
            for i, ln in enumerate(lines):
                if ln.startswith("- [ ]") and key in ln:
                    lines[i] = ln.replace("- [ ]", "- [x]", 1)
                    hit += 1
            if not hit:
                return f"没找到含「{key}」的待办。"
            try:
                with open(TODO_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError as e:
                return f"写失败：{e}"
            return f"划掉了 {hit} 条。"

        pending = [ln for ln in lines if ln.startswith("- [ ]")]
        if not pending:
            return "待办是空的。"
        items = [x[5:].split("<!--")[0].strip() for x in pending[:20]]
        return f"还剩 {len(pending)} 条：\n" + "\n".join(items)
