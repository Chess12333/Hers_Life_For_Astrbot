"""astrbot_plugin_ming_bili —— 让 QQ 这边的溟能看 B 站

2026-11-16 14:2x 立（她：「astrbot 没有和 qqa 一样的看 b站功能，补一个吧」）。

为什么有它：14:11 群里丢来一个 b23 短链，模型调了现成的 astrbot_plugin_biliread，
一句「Credential 类未提供 sessdata 或者为空」就死了——B 站的字幕元数据要登录态，
它把这一步当成必经路径，于是那一轮只能转给 DSH 侧代看。

这个插件把「看」拆成五件事，能不用登录态的就不用（2026-11-16 实测都通）：
  bili_search      搜索（WBI 签名，匿名可用）
  bili_video       标题/UP/时长/播放/简介/分P + 热评（匿名可用）
  bili_comments    评论区（匿名可用）
  bili_transcript  文字稿：自己的接口有没有字幕轨 → 走 bili-humanism CLI（它自己带
                   凭证文件，插件不读任何密钥）→ 还是没有就本地 ASR（scripts/bili-get.py）
  bili_frames      按秒间隔抽帧（默认 1.5s）→ 每 6 格一张拼图（帧多分多张，全部发出）→ 逐张视觉描述

不做的：点赞/投币/收藏/评论。写操作留在 DSH 侧，那边有台账和护栏。

另外一条：`/看b站 <链接或 BV>` 是给人手打的入口，出一张卡（标题、UP、数据、热评、封面）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse

import aiohttp

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"

try:
    from PIL import Image as PILImage, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    PILImage = None

HER = _MING_WS
BILI_GET_PY = os.path.join(HER, "scripts", "bili-get.py")
BILI_FRAMES_JS = os.path.join(HER, "scripts", "bili-frames.mjs")
TMP = r"C:\dsh\astrbot\data\temp\ming-bili"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
HDRS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

BV_RE = re.compile(r"BV[0-9A-Za-z]{10,12}")
SHORT_RE = re.compile(r"(?:b23\.tv|bili2233\.cn)/([0-9A-Za-z]+)")

MIXIN = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

SORTS = {"hot": 2, "time": 0, "reply": 1}
ORDERS = {"totalrank": "totalrank", "click": "click", "pubdate": "pubdate", "dm": "dm"}


class BiliError(RuntimeError):
    pass


def _fmt_dur(sec: int) -> str:
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_num(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _clean_title(t: str) -> str:
    return re.sub(r"</?em[^>]*>", "", t or "").replace("\n", " ").strip()


@register(
    "astrbot_plugin_ming_bili",
    "溟",
    "让 QQ 这边的溟看 B 站：搜片、读简介、取文字稿、看热评、抽帧看图。不用登录态的先走匿名接口。",
    "2026-11-16",
)
class MingBili(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._wbi_key: str | None = None
        self._wbi_ts: float = 0.0
        os.makedirs(TMP, exist_ok=True)

    # ── 配置 ─────────────────────────────────────────────
    def _cfg(self, key: str, default=None):
        try:
            v = self.config.get(key)
        except Exception:
            v = None
        return default if v is None or v == "" else v

    @property
    def vision_provider_id(self) -> str:
        return str(self._cfg("vision_provider_id", "openai/mimo-v2.6-pro"))

    @property
    def enable_asr(self) -> bool:
        return bool(self._cfg("enable_asr", True))

    @property
    def max_asr_seconds(self) -> int:
        try:
            return int(self._cfg("max_asr_seconds", 900))
        except Exception:
            return 900

    @property
    def send_frames(self) -> bool:
        return bool(self._cfg("send_frames_to_chat", True))

    # ── HTTP ─────────────────────────────────────────────
    async def _json(
        self, session: aiohttp.ClientSession, url: str, params: dict | None = None,
        check_code: bool = True,
    ):
        async with session.get(url, params=params, headers=HDRS) as r:
            try:
                j = await r.json(content_type=None)
            except Exception:
                txt = await r.text()
                raise BiliError(f"接口回了非 JSON（HTTP {r.status}）：{txt[:120]}")
        if not isinstance(j, dict):
            raise BiliError("接口返回结构不对")
        if check_code and j.get("code") != 0:
            raise BiliError(f"接口报错 code={j.get('code')} {j.get('message') or ''}".strip())
        return j.get("data") if check_code else j

    async def _wbi(self, session: aiohttp.ClientSession) -> str:
        if self._wbi_key and time.time() - self._wbi_ts < 3600:
            return self._wbi_key
        async with session.get(
            "https://api.bilibili.com/x/web-interface/nav", headers=HDRS
        ) as r:
            j = await r.json(content_type=None)
        wbi = ((j or {}).get("data") or {}).get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        if len(raw) < 64:
            raise BiliError("拿不到 WBI 签名用的 key")
        self._wbi_key = "".join(raw[i] for i in MIXIN if i < len(raw))
        self._wbi_ts = time.time()
        return self._wbi_key

    async def _wbi_params(self, session, params: dict) -> dict:
        key = await self._wbi(session)
        p = dict(params)
        p["wts"] = int(time.time())
        q = urllib.parse.urlencode(sorted(p.items()), quote_via=urllib.parse.quote)
        p["w_rid"] = hashlib.md5((q + key).encode()).hexdigest()
        return p

    # ── 解析输入 ──────────────────────────────────────────
    async def _bvid(self, session, text: str) -> str:
        raw = (text or "").strip()
        m = BV_RE.search(raw)
        if m:
            return m.group(0)
        m = SHORT_RE.search(raw)
        if m:
            url = "https://b23.tv/" + m.group(1)
            try:
                async with session.get(
                    url, allow_redirects=True, headers=HDRS,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as r:
                    final = str(r.url)
            except Exception as e:  # noqa: BLE001
                raise BiliError(f"短链没解开：{type(e).__name__}")
            m2 = BV_RE.search(final)
            if m2:
                return m2.group(0)
            raise BiliError("短链跳过去了，但里面没有 BV 号")
        raise BiliError("没找到 BV 号。要 BV1xxxxxxxxx，或者 b23.tv 短链、带 BV 的链接")

    async def _view(self, session, bvid: str) -> dict:
        return await self._json(
            session, "https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}
        )

    async def _comments(self, session, aid: int, sort: str, page: int, limit: int) -> list[dict]:
        data = await self._json(
            session,
            "https://api.bilibili.com/x/v2/reply",
            {
                "type": 1,
                "oid": aid,
                "sort": SORTS.get(sort, 2),
                "ps": max(1, min(limit, 30)),
                "pn": max(1, page),
            },
        )
        return data.get("replies") or []

    @staticmethod
    def _fmt_comment(c: dict) -> str:
        name = (c.get("member") or {}).get("uname") or "?"
        msg = ((c.get("content") or {}).get("message") or "").replace("\n", " ").strip()
        return f"@{name}（{_fmt_num(c.get('like'))}赞）：{msg[:220]}"

    # ── 工具 1：搜索 ──────────────────────────────────────
    @llm_tool(name="bili_search")
    async def bili_search(
        self, event: AstrMessageEvent, keyword: str, order: str = "totalrank", limit: int = 8
    ) -> str:
        """在 B 站搜视频，拿 BV 号。想找片、想看看某个话题有没有人拍过的时候用它；拿到 BV 再用 bili_video / bili_transcript 往下看。

        Args:
            keyword(string): 搜索词，短一点，别用整句
            order(string): 排序：totalrank 综合（默认）/ click 播放多 / pubdate 最新 / dm 弹幕多
            limit(number): 要几条，默认 8，最多 15
        """
        kw = (keyword or "").strip()
        if not kw:
            return "搜索词空着。"
        n = max(1, min(int(limit or 8), 15))
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                params = await self._wbi_params(
                    s,
                    {
                        "search_type": "video",
                        "keyword": kw,
                        "page": 1,
                        "page_size": n,
                        "order": ORDERS.get(order, "totalrank"),
                    },
                )
                data = await self._json(
                    s, "https://api.bilibili.com/x/web-interface/wbi/search/type", params
                )
        except BiliError as e:
            return f"搜不了：{e}。这一分钟别再重试，容易撞风控。"
        except Exception as e:  # noqa: BLE001
            return f"搜不了：{type(e).__name__}: {e}"
        rows = data.get("result") or []
        if not rows:
            return f"「{kw}」一条都没搜到。"
        lines = [f"「{kw}」搜到 {len(rows)} 条："]
        for i, it in enumerate(rows, 1):
            pub = ""
            try:
                pub = time.strftime("%Y-%m-%d", time.localtime(int(it.get("pubdate") or 0)))
            except Exception:  # noqa: BLE001
                pub = ""
            lines.append(
                f"{i}. {_clean_title(it.get('title'))}（{it.get('bvid')}）\n"
                f"   {it.get('author')} · {_fmt_num(it.get('play'))}播放 · "
                f"{it.get('duration')} · {pub}"
            )
        return "\n".join(lines)

    # ── 工具 2：视频信息 ──────────────────────────────────
    @llm_tool(name="bili_video")
    async def bili_video(
        self, event: AstrMessageEvent, url_or_bvid: str, with_comments: bool = True,
        comment_limit: int = 5,
    ) -> str:
        """看一个 B 站视频的基本信息：标题、UP、时长、播放/点赞、发布时间、简介全文、分P，顺带几条热评。别人丢来一个链接先调它。

        Args:
            url_or_bvid(string): BV 号、b23.tv 短链或视频链接
            with_comments(boolean): 要不要带热评，默认要
            comment_limit(number): 热评几条，默认 5
        """
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                bvid = await self._bvid(s, url_or_bvid)
                d = await self._view(s, bvid)
                out = [f"《{d.get('title')}》  https://www.bilibili.com/video/{bvid}"]
                st = d.get("stat") or {}
                out.append(
                    f"UP：{((d.get('owner') or {}).get('name'))} ｜ 时长 {_fmt_dur(d.get('duration'))}"
                    f" ｜ 播放 {_fmt_num(st.get('view'))} ｜ 弹幕 {_fmt_num(st.get('danmaku'))}"
                    f" ｜ 点赞 {_fmt_num(st.get('like'))} ｜ 投币 {_fmt_num(st.get('coin'))}"
                )
                out.append(
                    f"发布：{time.strftime('%Y-%m-%d', time.localtime(d.get('pubdate') or 0))}"
                    f" ｜ 评论 {_fmt_num(st.get('reply'))} ｜ 收藏 {_fmt_num(st.get('favorite'))}"
                )
                desc = (d.get("desc") or "").strip()
                if desc and desc != "-":
                    out.append("简介：" + desc[:900])
                pages = d.get("pages") or []
                if len(pages) > 1:
                    names = " / ".join(
                        f"P{p.get('page')} {p.get('part')}" for p in pages[:8]
                    )
                    out.append(f"共 {len(pages)} P：{names}")
                if with_comments:
                    try:
                        cs = await self._comments(
                            s, d.get("aid"), "hot", 1, max(1, min(int(comment_limit or 5), 10))
                        )
                        if cs:
                            out.append("热评：")
                            out += ["  " + self._fmt_comment(c) for c in cs]
                    except Exception as e:  # noqa: BLE001
                        out.append(f"（热评没取到：{type(e).__name__}）")
                return "\n".join(out)
        except BiliError as e:
            return f"看不了：{e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("[ming-bili] bili_video 失败")
            return f"看不了：{type(e).__name__}: {e}"

    # ── 工具 3：评论 ──────────────────────────────────────
    @llm_tool(name="bili_comments")
    async def bili_comments(
        self, event: AstrMessageEvent, url_or_bvid: str, sort: str = "hot", page: int = 1,
        limit: int = 10,
    ) -> str:
        """读一个 B 站视频的评论区。想知道大家在想什么、值不值得看的时候用它。

        Args:
            url_or_bvid(string): BV 号或链接
            sort(string): hot 按点赞（默认）/ time 最新 / reply 回复多
            page(number): 第几页，默认 1
            limit(number): 几条，默认 10，最多 20
        """
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                bvid = await self._bvid(s, url_or_bvid)
                d = await self._view(s, bvid)
                cs = await self._comments(
                    s, d.get("aid"), sort, int(page or 1), max(1, min(int(limit or 10), 20))
                )
                if not cs:
                    return f"《{d.get('title')}》这一页没有评论（关了评论区，或者页数越界）。"
                lines = [f"《{d.get('title')}》评论区（{sort}，第 {page} 页）："]
                lines += [f"{i}. {self._fmt_comment(c)}" for i, c in enumerate(cs, 1)]
                return "\n".join(lines)
        except BiliError as e:
            return f"读不了：{e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("[ming-bili] bili_comments 失败")
            return f"读不了：{type(e).__name__}: {e}"

    # ── 工具 4：文字稿 ────────────────────────────────────
    @llm_tool(name="bili_transcript")
    async def bili_transcript(
        self, event: AstrMessageEvent, url_or_bvid: str, max_chars: int = 2500,
        allow_asr: bool = True,
    ) -> str:
        """取一个 B 站视频的文字稿（字幕；没有字幕就在时长允许时走本地语音转写）。想要片子讲了什么、却看不到画面时用它。

        Args:
            url_or_bvid(string): BV 号或链接
            max_chars(number): 最多回多少字，默认 2500
            allow_asr(boolean): 允许本地转写（慢，十几秒到几分钟），默认允许
        """
        n = max(200, min(int(max_chars or 2500), 8000))
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                bvid = await self._bvid(s, url_or_bvid)
                d = await self._view(s, bvid)
                title, dur, cid = d.get("title"), int(d.get("duration") or 0), d.get("cid")
        except BiliError as e:
            return f"取不到：{e}"
        except Exception as e:  # noqa: BLE001
            return f"取不到：{type(e).__name__}: {e}"

        # 1) 匿名能不能直接拿到字幕轨
        text = ""
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                data = await self._json(
                    s, "https://api.bilibili.com/x/player/v2", {"bvid": bvid, "cid": cid}
                )
                subs = ((data.get("subtitle") or {}).get("subtitles")) or []
                if subs:
                    pick = next(
                        (x for x in subs if str(x.get("lan", "")).startswith("zh")), subs[0]
                    )
                    u = pick.get("subtitle_url") or ""
                    if u.startswith("//"):
                        u = "https:" + u
                    j = await self._json(s, u, check_code=False) if u else {}
                    body = (j or {}).get("body") or []
                    text = "\n".join(
                        "[{}] {}".format(
                            _fmt_dur(int(seg.get("from") or 0)), (seg.get("content") or "").strip()
                        )
                        for seg in body
                        if (seg.get("content") or "").strip()
                    )
                    if text:
                        text = f"（官方字幕，{len(body)} 段）\n" + text
        except Exception as e:  # noqa: BLE001
            logger.info("[ming-bili] 匿名字幕轨没拿到：%s", e)

        # 2) 自己的 CLI + 本地 ASR
        if not text:
            if not allow_asr:
                return f"《{title}》匿名接口没有字幕轨。要我走本地转写就说一声。"
            if dur > self.max_asr_seconds:
                return (
                    f"《{title}》没有官方字幕轨，片子 {_fmt_dur(dur)}，"
                    f"超过 {self.max_asr_seconds // 60} 分钟不自动转写（本地 ASR 太慢）。"
                    "想了解内容可以看简介和热评，或者抽几帧看画面。"
                )
            text = await self._run_bili_get(bvid, dur)

        if not text:
            return f"《{title}》没拿到文字稿（可能整片没人声，或者转写失败了）。"

        cut = text if len(text) <= n else text[:n] + f"\n…（截断，全文 {len(text)} 字）"
        return f"《{title}》文字稿：\n{cut}"

    async def _run_bili_get(self, bvid: str, dur: int) -> str:
        out = os.path.join(TMP, f"{bvid}.txt")
        cmd = [sys.executable, BILI_GET_PY, bvid, f"--out={out}"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=HER,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=420)
            except asyncio.TimeoutError:
                proc.kill()
                return "转写超时（7 分钟），放弃了。"
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-bili] 调 bili-get 失败：%s", e)
            return ""
        logger.info(
            "[ming-bili] bili-get %s 退出码 %s：%s",
            bvid, proc.returncode,
            (stdout or b"").decode("utf-8", "ignore")[-300:].replace("\n", " "),
        )
        if not os.path.exists(out):
            return ""
        try:
            with open(out, encoding="utf-8") as f:
                body = f.read().strip()
        except Exception:
            return ""
        if not body:
            return ""
        return f"（本地 ASR 转写；片子 {_fmt_dur(dur)}，可能有错字）\n" + body

    # ── 工具 5：抽帧看图 ──────────────────────────────────
    @llm_tool(name="bili_frames")
    async def bili_frames(
        self,
        event: AstrMessageEvent,
        url_or_bvid: str,
        count: int = 0,
        describe: bool = True,
        interval: float = 1.5,
        max_frames: int = 24,
    ) -> str:
        """抽帧看一支 B 站视频的画面，拼图发到当前聊天（每张 6 格，帧多了自动分成多张）。

        默认按秒数等间隔抽：interval=1.5 就是每 1.5 秒一帧。要用老办法（均匀抽 count 帧、只出一张图）就传 count>0。

        Args:
            url_or_bvid(string): BV 号或链接
            count(number): 老模式：均匀抽几帧（1-12），只出一张拼图；传了它就忽略 interval
            describe(boolean): 要不要让视觉模型描述画面，默认要（多张图逐张描述）
            interval(number): 秒数间隔，默认 1.5（1-2 秒一档）
            max_frames(number): 间隔模式最多抽几帧（默认 24=4 张拼图，上限 120；超了按等距取）
        """
        use_count = int(count or 0) > 0
        iv = 0.0 if use_count else max(0.0, float(interval or 0))
        if not use_count and iv <= 0:
            use_count = True  # count / interval 都没给：退回老行为（6 帧一张）
        n = max(2, min(int(count or 6), 12)) if use_count else 0
        cap = max(6, min(int(max_frames or 24), 120))
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                bvid = await self._bvid(s, url_or_bvid)
                d = await self._view(s, bvid)
                title, dur = d.get("title"), int(d.get("duration") or 0)
        except BiliError as e:
            return f"抽不了：{e}"
        except Exception as e:  # noqa: BLE001
            return f"抽不了：{type(e).__name__}: {e}"

        out_dir = os.path.join(TMP, "frames", bvid)
        # 2026-11-20 16:0x（0a4869）：同一支片第二次跑时，上次的帧还躺在目录里，
        # 会被一起拼进拼图。抽之前先清掉。
        try:
            if os.path.isdir(out_dir):
                for fn in os.listdir(out_dir):
                    if re.fullmatch(r"(f\d+\.jpg|sheet.*\.jpg)", fn):
                        os.remove(os.path.join(out_dir, fn))
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-bili] 清旧帧失败（不影响抽帧）：%s", e)
        cmd = ["node", BILI_FRAMES_JS, bvid]
        if use_count:
            cmd.append(f"--n={n}")
        else:
            cmd.append(f"--interval={iv}")
            cmd.append(f"--max={cap}")
        cmd.append(f"--out={out_dir}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=HER,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except asyncio.TimeoutError:
                proc.kill()
                return "抽帧超时（5 分钟），这支片可能太大。"
        except Exception as e:  # noqa: BLE001
            return f"抽帧脚本起不来：{type(e).__name__}: {e}"
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", "ignore")[-300:]
            return f"抽帧失败（退出码 {proc.returncode}）：{err.strip()[:280]}"

        frames = []
        if os.path.isdir(out_dir):
            for fn in os.listdir(out_dir):
                m = re.fullmatch(r"f(\d+)\.jpg", fn)
                if m:
                    frames.append((int(m.group(1)), os.path.join(out_dir, fn)))
        frames.sort()
        if not frames:
            return "脚本说抽到了，但目录里没有帧文件。"

        # 2026-11-20 16:0x（0a4869）：每张拼图只放 6 格（3 列 × 2 行），帧多了分多张，一次全发。
        groups = [frames[i : i + 6] for i in range(0, len(frames), 6)]
        sheets: list[tuple[str, list[int]]] = []
        for k, grp in enumerate(groups, 1):
            path = os.path.join(out_dir, f"sheet_{k}.jpg")
            try:
                path = self._tile(grp, path, cols=3)
                sheets.append((path, [t for t, _ in grp]))
            except Exception as e:  # noqa: BLE001
                logger.warning("[ming-bili] 拼第 %d 张失败：%s", k, e)
        if not sheets:
            sheets = [(frames[0][1], [frames[0][0]])]

        if self.send_frames:
            try:
                await event.send(
                    MessageChain(chain=[Image.fromFileSystem(p) for p, _ in sheets])
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[ming-bili] 发拼图失败：%s", e)

        head = (
            f"《{title}》抽了 {len(frames)} 帧（{_fmt_dur(dur)} 的片），"
            f"{len(sheets)} 张拼图 × 每张 6 格，已发到聊天。"
            f"秒数：{', '.join(str(t) + 's' for t, _ in frames)}。"
        )
        if not describe:
            return head + "（描述关掉了）"

        descs: list[str] = []
        for k, (path, secs) in enumerate(sheets, 1):
            d = await self._describe_sheet(path, secs)
            if d:
                descs.append((f"第 {k} 张：\n" if len(sheets) > 1 else "") + d)
        if not descs:
            return head + "但视觉模型没给出描述。"
        return head + "\n画面（按格序）：\n" + "\n".join(descs)

    @staticmethod
    def _tile(frames: list[tuple[int, str]], out: str, cols: int = 0) -> str:
        if PILImage is None:
            raise RuntimeError("没有 PIL")
        imgs = []
        for sec, path in frames:
            im = PILImage.open(path).convert("RGB")
            w = 640
            im = im.resize((w, max(1, int(w * im.height / im.width))))
            draw = ImageDraw.Draw(im)
            try:
                font = ImageFont.load_default(size=24)
            except Exception:
                font = ImageFont.load_default()
            label = _fmt_dur(sec)
            draw.rectangle([0, 0, 16 + 13 * len(label), 32], fill=(0, 0, 0))
            draw.text((8, 5), label, fill=(255, 235, 120), font=font)
            imgs.append(im)
        cols = cols or (3 if len(imgs) > 4 else 2)  # 传了 cols 就照传的排（6 格固定 3×2）
        rows = math.ceil(len(imgs) / cols)
        cw, ch = imgs[0].size
        sheet = PILImage.new("RGB", (cols * cw, rows * ch), (18, 18, 18))
        for i, im in enumerate(imgs):
            sheet.paste(im, ((i % cols) * cw, (i // cols) * ch))
        if sheet.width > 1920:
            sheet = sheet.resize((1920, int(sheet.height * 1920 / sheet.width)))
        sheet.save(out, "JPEG", quality=88)
        return out

    async def _describe_sheet(self, path: str, secs: list[int]) -> str:
        pid = self.vision_provider_id
        try:
            provider = self.context.get_provider_by_id(pid)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-bili] 拿不到视觉模型 %s：%s", pid, e)
            return ""
        if provider is None:
            logger.warning("[ming-bili] 配置里的视觉模型 %s 不存在", pid)
            return ""
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            cols = 3 if len(secs) > 4 else 2
            prompt = (
                f"这是一支视频按时间顺序抽的 {len(secs)} 帧，拼成 {cols} 列的网格，"
                "从左到右、从上到下排列，每格左上角标着秒数。"
                "只写画面里确实有的东西：拍了什么、有谁/什么东西、在做什么、什么场景、画面上的字。"
                "不要评价、不要猜剧情、不要总结，也不要提「视频」这个词以外的判断。"
                "每格一到两句，按「[秒数] …」一行一条。"
            )
            resp = await provider.text_chat(
                prompt=prompt, image_urls=[f"data:image/jpeg;base64,{b64}"]
            )
            return (getattr(resp, "completion_text", "") or "").strip()[:2000]
        except Exception as e:  # noqa: BLE001
            logger.warning("[ming-bili] 视觉描述失败：%s", e)
            return ""

    # ── 手打入口 ─────────────────────────────────────────
    @filter.command("看b站", alias={"看片", "bili"})
    async def cmd_look(self, event: AstrMessageEvent, arg: str = ""):
        """手打：/看b站 BV1xxxxxxxxx 或 /看b站 https://b23.tv/xxxx"""
        raw = (arg or "").strip()
        if not raw:
            raw = (event.message_str or "")
            for k in ("/看b站", "/看片", "/bili"):
                raw = raw.replace(k, "")
            raw = raw.strip()
        if not raw:
            yield event.plain_result("用法：/看b站 BV1xxxxxxxxx（或 b23.tv 短链）")
            return
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                bvid = await self._bvid(s, raw)
                d = await self._view(s, bvid)
                st = d.get("stat") or {}
                lines = [
                    f"《{d.get('title')}》",
                    f"UP {((d.get('owner') or {}).get('name'))} · {_fmt_dur(d.get('duration'))}"
                    f" · {_fmt_num(st.get('view'))}播放 · {_fmt_num(st.get('like'))}赞",
                ]
                try:
                    cs = await self._comments(s, d.get("aid"), "hot", 1, 3)
                    lines += [self._fmt_comment(c) for c in cs]
                except Exception:  # noqa: BLE001
                    pass
                pic = d.get("pic") or ""
                if pic.startswith("//"):
                    pic = "https:" + pic
        except BiliError as e:
            yield event.plain_result(f"看不了：{e}")
            return
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"看不了：{type(e).__name__}: {e}")
            return
        chain = [Plain(text="\n".join(lines))]
        if pic:
            chain.append(Image.fromURL(pic))
        yield event.chain_result(chain)

    async def terminate(self):
        pass
