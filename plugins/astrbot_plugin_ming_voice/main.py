"""astrbot_plugin_ming_voice —— 让 QQ 这边的溟用自己的嗓子说话

2026-11-16 17:1x 立（她：「一起做好吧……接下来可以做开关和白名单了」）。

为什么不用自带的 TTS：
  · `provider_tts_settings.enable` 一开是**全开**——`is_tts_enabled_for_session()`
    在没配置时返回 True，等于所有会话都发语音，没有白名单可写。
  · 自带的 provider 只出 24k wav，且不能按会话挑。
所以这里自己做：白名单写在插件配置里，命中才转语音，其余照旧发文字。

两个必须知道的现场事实：
  · `astrbot_plugin_pacer` 会在 on_decorating_result 里 `result.chain.clear()`
    然后自己发，所以本插件 priority 要比它小（更早跑），把 Plain 换成 Record
    之后再交给它——它对非 Plain 组件是「自己占一段原样发」，正好。
  · 音色 ID 与模型必须配对：cosyvoice-v3.5-flash-… 只能配 cosyvoice-v3.5-flash，
    配 v1 会报 Model not exist（2026-11-16 实测）。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import random
import re
import shutil
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register
# 2026-11-23：工作区不写死。换机器设环境变量 MING_WORKSPACE 即可；没设就退回作者本机那份。
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"


@register(
    "astrbot_plugin_ming_voice",
    "溟",
    "溟的音色 TTS：百炼 cosyvoice 复刻音色，按会话白名单把文字回复换成语音。",
    "2026-11-16",
)
class MingVoice(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.cache_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "audio",
        )
        os.makedirs(self.cache_dir, exist_ok=True)
        # 2026-11-21 17:1x：DSH 侧要调这个音色，但 key 只在 AstrBot 配置里（AI 不读凭证文件）。
        # 改法：那边往 queue.json 写 {id, text}，这边自己合成、拷进 voice-bank/samples 留档。
        self._bank_task = None

    # ── 配置 ────────────────────────────────────────────
    def _cfg(self, key: str, default=None):
        try:
            v = self.config.get(key)
        except Exception:
            v = None
        return default if v is None or v == "" else v

    def _provider(self) -> dict:
        """从主配置里找 dashscope_tts，拿 key / model / voice。"""
        try:
            cfg = self.context.get_config()
        except Exception:
            return {}
        providers = cfg.get("provider") if hasattr(cfg, "get") else None
        for p in providers or []:
            if p.get("type") == "dashscope_tts" or p.get("id") == "dashscope_tts":
                return dict(p)
        return {}

    @property
    def voice(self) -> str:
        return str(
            self._cfg("voice") or self._provider().get("dashscope_tts_voice") or ""
        ).strip()

    @property
    def model(self) -> str:
        return str(
            self._cfg("model") or self._provider().get("model") or "cosyvoice-v3.5-flash"
        ).strip()

    @property
    def api_key(self) -> str:
        return str(self._cfg("api_key") or self._provider().get("api_key") or "").strip()

    # ── 白名单 ──────────────────────────────────────────
    def _list(self, key: str) -> list[str]:
        v = self._cfg(key, []) or []
        if isinstance(v, str):
            v = [x for x in re.split(r"[\s,]+", v) if x]
        return [str(x).strip().rstrip(":") for x in v if str(x).strip()]

    def _match(self, umo: str, items: list[str]) -> bool:
        for a in items:
            if a == "*" or umo == a or umo.startswith(a + ":"):
                return True
        # 2026-11-23：换了平台（umo 前缀不是 ming）时，作者的默认白名单一条都匹配不上，
        # 语音就静默不发——装的人根本不知道为什么没声音。这里只报一次，说清是白名单的平台写错了。
        if items and not getattr(self, "_umo_warned", False):
            plat = umo.split(":", 1)[0]
            if all(a.split(":", 1)[0] != plat for a in items):
                self._umo_warned = True
                logger.warning(
                    "[ming_voice] 白名单里没有一条属于当前平台 %r（现在是 %s）。改成 %r 或写 * 才会发语音",
                    plat, ", ".join(items)[:120], plat + ":FriendMessage:<你的号>")
        return False

    # ── 会话级语音开关（她说不要就别发；写文件，重启不丢）─────────
    # 2026-11-18 02:2x 溟加。起因：她说「大晚上的不要发语音」，插件把它当关键词，
    # 连发两条。光排除否定词不够——她说的是「这个点别发」，那是状态，不是一次性的句子。
    VOICE_STATE = _MING_WS + r"\live\qq-voice-off.json"

    def _load_off(self) -> dict:
        try:
            with open(self.VOICE_STATE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save_off(self, d: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.VOICE_STATE), exist_ok=True)
            with open(self.VOICE_STATE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def voice_off(self, umo: str) -> bool:
        return bool(self._load_off().get(umo, {}).get("off"))

    def _note_voice_pref(self, umo: str, text: str) -> None:
        """听到「别发语音」就关掉这个会话，听到「可以发语音」再打开。"""
        t = (text or "").strip()
        if not t:
            return
        d = self._load_off()
        if any(w in t for w in self.VOICE_OFF_WORDS):
            d[umo] = {"off": True, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": t[:40]}
            self._save_off(d)
            logger.info("[ming_voice] 关掉这个会话的语音：%s", t[:30])
        elif any(w in t for w in self.VOICE_ON_WORDS):
            if umo in d:
                d.pop(umo, None)
                self._save_off(d)
            logger.info("[ming_voice] 打开这个会话的语音：%s", t[:30])

    def mode(self, umo: str) -> str | None:
        """prob = 概率+关键词；manual = 只关键词、且只认指定发送者。"""
        if not bool(self._cfg("enable", True)):
            return None
        if self.voice_off(umo):
            return None
        if self._match(umo, self._list("sessions")):
            return "prob"
        if self._match(umo, self._list("manual_sessions")):
            return "manual"
        return None

    def hit(self, umo: str) -> bool:
        return self.mode(umo) is not None

    def sender_allowed(self, sender: str) -> bool:
        allow = self._list("manual_senders")
        return bool(allow) and str(sender).strip() in allow

    # ── 合成 ────────────────────────────────────────────
    def _synth(self, text: str) -> str | None:
        key, voice, model = self.api_key, self.voice, self.model
        if not key or not voice:
            logger.warning("[ming_voice] 缺 api_key 或 voice，跳过本次语音")
            return None
        try:
            import dashscope
            from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer
        except Exception as e:
            logger.error(f"[ming_voice] dashscope SDK 不可用：{e}")
            return None

        dashscope.api_key = key
        try:
            syn = SpeechSynthesizer(
                model=model,
                voice=voice,
                format=AudioFormat.MP3_24000HZ_MONO_256KBPS,
            )
            data = syn.call(text, int(self._cfg("request_timeout_ms", 20000)))
        except Exception as e:
            logger.error(f"[ming_voice] 合成异常：{type(e).__name__}: {e}")
            return None
        if not data:
            logger.warning(f"[ming_voice] 合成空结果：{syn.get_response()}")
            return None

        path = os.path.join(self.cache_dir, f"tts_{int(time.time() * 1000)}.mp3")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _cleanup(self, keep: int = 200) -> None:
        files = sorted(glob.glob(os.path.join(self.cache_dir, "tts_*.mp3")))
        for f in files[:-keep] if len(files) > keep else []:
            try:
                os.remove(f)
            except Exception:
                pass

    # ── 音色留档（2026-11-21 17:1x 立）────────────────────
    # 她说的「云端生成音色到本地留档」：音色在阿里云，本地要留的是
    # ① model + voice 这个配对（写进 manifest，不写 key）
    # ② 每次合成的 mp3（拷进 samples/，缓存目录会滚动删，这里不删）
    VOICE_BANK = _MING_WS + r"\live\voice-bank"
    QUEUE = _MING_WS + r"\live\voice-bank\queue.json"

    def _bank_manifest(self, entry: dict) -> None:
        path = os.path.join(self.VOICE_BANK, "manifest.json")
        data = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
        data.setdefault("model", self.model)
        data.setdefault("voice", self.voice or "(未配)")
        data["model"] = self.model
        data["voice"] = self.voice or data.get("voice") or "(未配)"
        data.setdefault("samples", [])
        data["samples"].append(entry)
        data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(self.VOICE_BANK, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _drain_queue(self) -> int:
        try:
            with open(self.QUEUE, "r", encoding="utf-8") as f:
                jobs = json.load(f) or []
        except Exception:
            return 0
        if not isinstance(jobs, list) or not jobs:
            return 0
        left, done = [], 0
        limit = int(self._cfg("max_chars", 220))
        for job in jobs:
            job = job if isinstance(job, dict) else {}
            text = str(job.get("text") or "").strip()[:limit]
            jid = str(job.get("id") or f"{int(time.time() * 1000)}")
            if not text:
                continue
            path = self._synth(text)
            if not path:
                left.append(job)
                continue
            dst = os.path.join(self.VOICE_BANK, "samples", f"{jid}.mp3")
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(path, dst)
            except Exception as e:
                logger.error(f"[ming_voice] 留档拷贝失败：{e}")
                left.append(job)
                continue
            self._bank_manifest(
                {
                    "id": jid,
                    "text": text,
                    "file": dst,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            done += 1
        try:
            with open(self.QUEUE, "w", encoding="utf-8") as f:
                json.dump(left, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return done

    async def _queue_loop(self) -> None:
        while True:
            try:
                n = await asyncio.get_running_loop().run_in_executor(
                    None, self._drain_queue
                )
                if n:
                    logger.info(f"[ming_voice] 音色留档合成 {n} 条")
            except Exception as e:
                logger.error(f"[ming_voice] 留档队列异常：{type(e).__name__}: {e}")
            await asyncio.sleep(5)

    async def initialize(self) -> None:
        self._bank_task = asyncio.create_task(self._queue_loop())
        logger.info("[ming_voice] 版本 2026-11-21.1：加音色留档队列 %s", self.QUEUE)

    async def terminate(self) -> None:
        if self._bank_task:
            self._bank_task.cancel()

    # ── 触发判断 ────────────────────────────────────────
    # 2026-11-18 02:2x 溟加：关键词是裸的子串匹配，「不要发语音」里也有「语音」，
    # 于是 02:19:46 她说别发、02:19:52 我立刻发了一条，02:20:18 她说「怎么还是语音」、
    # 02:20:24 又发一条。命中位置往前看几个字，有否定字就不算命中。
    NEG_CHARS = "不别甭勿莫停免关少"
    VOICE_OFF_WORDS = (
        "不要发语音", "别发语音", "不发语音", "不用语音", "别说语音",
        "关掉语音", "关语音", "少发语音", "不要语音", "安静点",
    )
    VOICE_ON_WORDS = (
        "可以发语音", "发语音吧", "发条语音", "开语音", "用语音", "语音可以",
    )

    def forced(self, text: str) -> bool:
        """消息里命中关键词就强制发语音（不掷骰子；否定句不算）。"""
        keys = self._cfg("trigger_keywords", ["语音"]) or []
        if isinstance(keys, str):
            keys = [x for x in re.split(r"[\s,]+", keys) if x]
        low = (text or "").lower()
        for k in keys:
            k = str(k).strip().lower()
            if not k:
                continue
            start = 0
            while True:
                i = low.find(k, start)
                if i < 0:
                    break
                if not any(ch in low[max(0, i - 6):i] for ch in self.NEG_CHARS):
                    return True
                start = i + len(k)
        return False

    @property
    def probability(self) -> float:
        try:
            return max(0.0, min(float(self._cfg("trigger_probability", 0.25)), 1.0))
        except (TypeError, ValueError):
            return 0.25

    # ── 主钩子：排在 pacer 之前 ──────────────────────────
    # 排序是 `sort(key=lambda h: -priority)`，**大的先跑**（star_handler.py:26）。
    # pacer 用默认 0，所以这里必须 > 0，否则它先 clear chain 自己发，我们拿到空链。
    @filter.on_decorating_result(priority=10)
    async def tts_takeover(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        # 先记她说的话（关/开语音），再决定这一轮要不要发
        self._note_voice_pref(umo, event.message_str or "")
        mode = self.mode(umo)
        if mode is None:
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:
            return
        user_text = event.message_str or ""
        if user_text.strip().startswith("/"):
            return

        # prob（私聊）：关键词必发，否则按概率掷一次。
        # manual（群）：不掷骰子，只有她开口要才发——群里别人说「语音」不算。
        if self.forced(user_text):
            if mode == "manual":
                try:
                    sender = str(event.get_sender_id())
                except Exception:
                    sender = ""
                if not self.sender_allowed(sender):
                    logger.info(
                        f"[ming_voice] 群里命中关键词，但发送者 {sender} 不在 manual_senders，不发",
                    )
                    return
            logger.info("[ming_voice] 关键词命中，强制发语音")
        elif mode == "manual":
            return
        elif random.random() >= self.probability:
            return

        plains = [
            c for c in result.chain if isinstance(c, Plain) and len(c.text.strip()) > 1
        ]
        if not plains:
            return
        text = re.sub(r"\s+", " ", " ".join(c.text.strip() for c in plains)).strip()
        limit = int(self._cfg("max_chars", 220))
        if not text or len(text) > limit:
            logger.info(f"[ming_voice] 跳过语音：{len(text)} 字 > 上限 {limit}")
            return

        loop = asyncio.get_running_loop()
        try:
            path = await asyncio.wait_for(
                loop.run_in_executor(None, self._synth, text),
                timeout=float(self._cfg("hard_timeout", 30)),
            )
        except asyncio.TimeoutError:
            logger.error("[ming_voice] 合成超时，改发文字")
            return
        except Exception as e:
            logger.error(f"[ming_voice] 合成失败：{type(e).__name__}: {e}")
            return
        if not path:
            return

        new_chain, placed = [], False
        for c in result.chain:
            if isinstance(c, Plain) and len(c.text.strip()) > 1:
                if not placed:
                    new_chain.append(Record(file=path, text=text))
                    placed = True
                continue
            new_chain.append(c)
        result.chain = new_chain
        # 告诉 pacer 这一轮别接管：链里已经是 Record，核心 stage 自己发；
        # pacer 若照常 clear + 自己发，会把语音当成「一段」重复处理。
        try:
            event.set_extra("ming_voice_takeover", True)
            event.set_extra("ming_voice_text", text)
        except Exception:
            pass
        logger.info(f"[ming_voice] 已转语音 {len(text)} 字 → {path}")
        self._cleanup()

    # ── 文本回灌：让下一轮的我知道这句是语音说的 ──────────
    @staticmethod
    def _text_of(msg) -> str:
        c = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(
                x.get("text", "")
                for x in c
                if isinstance(x, dict) and x.get("type") == "text"
            )
        return ""

    async def _backfill_later(self, umo: str, text: str) -> None:
        # 框架写历史在 respond 之后（洋葱模型的 process 后半段），这里等它落完再改。
        await asyncio.sleep(float(self._cfg("backfill_delay", 2.0)))
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            if conv is None:
                return
            h = conv.history
            if isinstance(h, str):
                h = json.loads(h or "[]")
            if not isinstance(h, list):
                return
            key = text[:40]
            for i in range(len(h) - 1, -1, -1):
                if h[i].get("role") != "assistant":
                    continue
                body = self._text_of(h[i])
                if body.lstrip().startswith("[语音]"):
                    return
                if key and key in body:
                    h[i]["content"] = [{"type": "text", "text": "[语音] " + body}]
                    await cm.update_conversation(umo, cid, history=h)
                    logger.info("[ming_voice] 历史回灌：[语音] 前缀已加上")
                return
        except Exception as e:
            logger.warning(f"[ming_voice] 历史回灌失败：{type(e).__name__}: {e}")

    @filter.after_message_sent()
    async def backfill(self, event: AstrMessageEvent):
        text = ""
        try:
            text = str(event.get_extra("ming_voice_text") or "")
        except Exception:
            text = ""
        if not text:
            return
        asyncio.create_task(self._backfill_later(event.unified_msg_origin, text))

    # ── 自查指令 ────────────────────────────────────────
    @filter.command("语音")
    async def cmd_voice(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        m = self.mode(umo)
        label = {"prob": "概率+关键词", "manual": "只手动（要你说「语音」）"}.get(m, "关")
        yield event.plain_result(
            f"TTS：{label}\n会话：{umo}\n"
            f"概率：{self.probability}\n模型：{self.model}\n音色：{self.voice or '(未配)'}",
        )
