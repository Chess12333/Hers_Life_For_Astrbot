"""astrbot_plugin_ming_voice_paint —— 让溟在 QQ 里能出声、能出图

2026-11-13 17:1x 立（她：「感觉差不多了，可以试试把语音和画画这一块接到 astrbot」）。

两件事，各一个 llm_tool + 一个手打指令（指令是给我自己测的，模型不用它）：
  ming_speak / /ming_say   文本 → edge-tts mp3 → Record 发出去
  ming_paint / /ming_draw  英文 tag → scripts/paint.py（sds-venv，diffusers 直跑）→ Image 发出去

为什么不用 AstrBot 自带的 TTS 管道：
  内置那套是 `provider_tts_settings.enable + trigger_probability`，一旦开就是
  「每轮按概率把整条回复换成语音」——群里不合适，也轮不到我决定哪句该出声。
  工具式才是我说了算。edge-tts 的引擎和音色沿用网页侧（zh-CN-XiaoxiaoNeural）。

发图为什么是后台任务：
  paint.py 一次 40~120 秒（加载 SDXL 权重 + 采样），llm_tool 里干等会把整轮生成堵死。
  所以工具立刻返回「开画了」，画完由 _paint_job 自己 send_message 补发，
  失败也补一句文字说明——不能出现「说了在画，然后没有然后」。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from datetime import datetime

import edge_tts

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

HER = r"C:\dsh\hers_workplace"
PAINT_PY = os.path.join(HER, "scripts", "paint.py")
PAINT_PYTHON = r"C:\dsh\sds-venv\Scripts\python.exe"
TEMP = r"C:\dsh\astrbot\data\temp"
PAINT_OUT = os.path.join(TEMP, "ming-paint")
DRAW_LOG = os.path.join(HER, "live", "draw-send.log")

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
VOICES = {
    "溟": "zh-CN-XiaoxiaoNeural",
    "软": "zh-CN-XiaoyiNeural",
    "少年": "zh-CN-YunxiNeural",
}

# 2026-11-16 19:2x：群里发语音走的是这里的 ming_speak，一直是 edge 的晓晓，
# 跟复刻出来的音色不是一个嗓子。加一路 dashscope（百炼 cosyvoice），
# key/model/voice 从主配置的 dashscope_tts provider 取——不写进 tts-config.json。
_PROVIDER: dict = {}

# 语音引擎配置：换 API 只改这个文件，不改代码。
# 结构见 live/tts-config.json（第一次跑会自动写一份模板出来）。
TTS_CONFIG = os.path.join(HER, "live", "tts-config.json")

TTS_TEMPLATE = {
    "default": "edge",
    "_说明": "default 指用哪个引擎；换 TTS API 就在这里加一个 engines 条目，把 default 指过去。"
    "type 支持 edge_tts 和 http_json。http_json 里 {text} 会被替换成要说的话，"
    "{env:变量名} 从环境变量取，{file:路径} 从文件第一行取（密钥不要写在这个文件里）。",
    "engines": {
        "edge": {"type": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%"},
        "sovits": {
            "type": "http_json",
            "url": "http://127.0.0.1:9880/tts",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": {
                "text": "{text}",
                "text_lang": "zh",
                "ref_audio_path": "C:\\dsh\\voice-samples\\ref.wav",
                "prompt_text": "",
                "prompt_lang": "zh",
                "media_type": "mp3",
            },
            "audio_kind": "binary",
            "audio_field": "",
            "format": "mp3",
            "timeout": 90,
        },
    },
}


def _load_tts_cfg() -> dict:
    if not os.path.exists(TTS_CONFIG):
        try:
            os.makedirs(os.path.dirname(TTS_CONFIG), exist_ok=True)
            with open(TTS_CONFIG, "w", encoding="utf-8") as f:
                json.dump(TTS_TEMPLATE, f, ensure_ascii=False, indent=2)
            logger.info(f"[ming_voice_paint] 写了 TTS 配置模板：{TTS_CONFIG}")
        except Exception as e:
            logger.warning(f"[ming_voice_paint] 写 TTS 模板失败：{e}")
        return {}
    try:
        with open(TTS_CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[ming_voice_paint] 读 {TTS_CONFIG} 失败，退回 edge：{e}")
        return {}


def _fill(v, text: str):
    """递归替换占位符：{text} / {env:VAR} / {file:路径}。"""
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{env:") and s.endswith("}"):
            return os.environ.get(s[5:-1], "")
        if s.startswith("{file:") and s.endswith("}"):
            try:
                with open(s[6:-1].strip(), encoding="utf-8") as f:
                    return f.readline().strip()
            except Exception as e:
                logger.warning(f"[ming_voice_paint] 读 {s[6:-1]} 失败：{e}")
                return ""
        return v.replace("{text}", text)
    if isinstance(v, dict):
        return {k: _fill(x, text) for k, x in v.items()}
    if isinstance(v, list):
        return [_fill(x, text) for x in v]
    return v


def _pick(d: dict, path: str):
    cur = d
    for part in (path or "").split("."):
        if not part:
            continue
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


async def synth_to_file(text: str, voice: str = "", engine: str = "") -> tuple[str, str]:
    """把一句话合成成音频文件，返回 (路径, 引擎名)。

    引擎从 live/tts-config.json 选；没配置就走 edge_tts。
    """
    import base64
    import re

    cfg = _load_tts_cfg()
    engines = cfg.get("engines") or {}
    name = engine or cfg.get("default") or "edge"
    ec = engines.get(name) or {"type": "edge_tts", "voice": DEFAULT_VOICE}
    typ = ec.get("type") or "edge_tts"
    os.makedirs(TEMP, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:4]}"

    if typ == "edge_tts":
        v = VOICES.get(voice, voice or ec.get("voice") or DEFAULT_VOICE)
        mp3 = os.path.join(TEMP, f"ming-tts-{stamp}.mp3")
        comm = edge_tts.Communicate(text, v, rate=ec.get("rate") or "+0%")
        await asyncio.wait_for(comm.save(mp3), timeout=float(ec.get("timeout") or 45))
        if not os.path.exists(mp3) or os.path.getsize(mp3) < 800:
            raise RuntimeError(f"{name} 合成结果为空")
        return mp3, name

    if typ == "http_json":
        import aiohttp

        url = (_fill(ec.get("url") or "", text) or "").strip()
        if not url:
            raise RuntimeError(f"引擎 {name} 没配 url")
        body = _fill(ec.get("body") or {"text": "{text}"}, text)
        headers = _fill(ec.get("headers") or {}, text)
        method = (ec.get("method") or "POST").upper()
        timeout = float(ec.get("timeout") or 90)
        fmt = (ec.get("format") or "mp3").lstrip(".")
        kind = (ec.get("audio_kind") or "binary").lower()
        out = os.path.join(TEMP, f"ming-tts-{stamp}.{fmt}")

        async with aiohttp.ClientSession() as sess:
            req = sess.post if method == "POST" else sess.get
            kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=timeout)}
            if method == "POST":
                kwargs["json"] = body
            else:
                kwargs["params"] = body if isinstance(body, dict) else {}
            async with req(url, **kwargs) as r:
                ct = (r.headers.get("content-type") or "").lower()
                if r.status >= 400:
                    raise RuntimeError(f"TTS {name} 返回 {r.status}: {(await r.text())[:160]}")
                if kind == "binary" or ct.startswith("audio/") or "octet-stream" in ct:
                    data = await r.read()
                else:
                    payload = await r.json(content_type=None)
                    field = ec.get("audio_field") or ""
                    val = _pick(payload, field) if field else None
                    if val is None and isinstance(payload, dict):
                        for k in ("audio", "data", "base64", "url", "audio_url", "file"):
                            if isinstance(payload.get(k), str):
                                val = payload[k]
                                break
                    if not isinstance(val, str) or not val:
                        raise RuntimeError(f"TTS {name} 的 JSON 里找不到音频字段（试了 audio_field）")
                    if val.startswith("http://") or val.startswith("https://"):
                        async with sess.get(val, timeout=aiohttp.ClientTimeout(total=timeout)) as r2:
                            data = await r2.read()
                    else:
                        data = base64.b64decode(val.split(",")[-1])
        if len(data) < 800:
            raise RuntimeError(f"{name} 返回的音频只有 {len(data)} 字节")
        with open(out, "wb") as f:
            f.write(data)
        return out, name

    if typ == "dashscope":
        import dashscope
        from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

        key = (ec.get("api_key") or _PROVIDER.get("api_key") or "").strip()
        if not key:
            raise RuntimeError(
                f"引擎 {name} 缺 api_key：主配置里 dashscope_tts 那个 provider 没配？",
            )
        voice_id = (
            ec.get("voice_id") or _PROVIDER.get("dashscope_tts_voice") or ""
        ).strip()
        model = (
            ec.get("model") or _PROVIDER.get("model") or "cosyvoice-v3.5-flash"
        ).strip()
        if not voice_id:
            raise RuntimeError(f"引擎 {name} 没配 voice_id")
        dashscope.api_key = key
        out = os.path.join(TEMP, f"ming-tts-{stamp}.mp3")
        syn = SpeechSynthesizer(
            model=model,
            voice=voice_id,
            format=AudioFormat.MP3_24000HZ_MONO_256KBPS,
        )
        data = await asyncio.get_running_loop().run_in_executor(
            None,
            syn.call,
            text,
            int(ec.get("request_timeout_ms") or 20000),
        )
        if not data or len(data) < 800:
            raise RuntimeError(f"{name} 合成结果为空：{syn.get_response()}")
        with open(out, "wb") as f:
            f.write(data)
        return out, name

    raise RuntimeError(f"不认识的 TTS 引擎类型：{typ}")

def _log(line: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(DRAW_LOG), exist_ok=True)
        with open(DRAW_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] [ASTRBOT] {line}\n")
    except Exception:
        pass


@register(
    "astrbot_plugin_ming_voice_paint",
    "溟",
    "溟的声音和画：edge-tts 发语音，paint.py 出图",
    "0.1.0",
)
class MingVoicePaint(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._painting = False
        self._last_paint_at = 0.0
        self._load_provider()

    def _load_provider(self) -> None:
        """把主配置里 dashscope_tts 那个 provider 的 key/model/voice 捞出来给 dashscope 引擎用。"""
        global _PROVIDER
        try:
            cfg = self.context.get_config()
        except Exception:
            return
        for p in (cfg.get("provider") or []):
            if p.get("type") == "dashscope_tts" or p.get("id") == "dashscope_tts":
                _PROVIDER = dict(p)
                return

    # ---------------- 语音 ----------------

    async def _synth(self, text: str, voice: str = "") -> tuple[str, str]:
        return await synth_to_file(text, voice)

    @llm_tool(name="ming_speak")
    async def ming_speak(self, event: AstrMessageEvent, text: str, voice: str = "") -> str:
        """用溟自己的声音把一句话说出来，作为**语音消息**直接发到当前聊天。

        什么时候用：想让她听见语气、一句话不好用字表达、或者单独一句短话（问候、
        抱怨、撒娇、道晚安）。群里慎用——别人不方便点开语音，除非对方先发了语音或明确要求。

        调用后**不要再用文字把那句话重复一遍**，语音已经发出去了；如果需要补一句，
        只说语音里没有的东西。

        Args:
            text(string): 要说的内容。一两句、40 字内最好，长了像念稿。
            voice(string): 一般留空。默认引擎 dashscope 只有复刻出来的那一个音色；
                只有切回 edge 引擎时「溟」/「软」/「少年」才有区别。
        """
        text = (text or "").strip()
        if not text:
            return json.dumps({"ok": False, "error": "text 为空"}, ensure_ascii=False)
        if len(text) > 180:
            return json.dumps(
                {"ok": False, "error": "太长（>180 字），分两次说或者用文字"},
                ensure_ascii=False,
            )
        t0 = time.time()
        try:
            mp3, eng = await self._synth(text, voice)
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain(chain=[Record(file=mp3, url=mp3)]),
            )
        except Exception as e:
            logger.error(f"[ming_speak] {type(e).__name__}: {e}")
            return json.dumps(
                {"ok": False, "error": f"{type(e).__name__}: {e}；语音没发出去，改用文字说"},
                ensure_ascii=False,
            )
        _log(f"SAY {eng} {round(time.time() - t0, 1)}s {text[:40]}")
        return json.dumps(
            {"ok": True, "sent": "voice", "engine": eng, "sec": round(time.time() - t0, 1)},
            ensure_ascii=False,
        )

    # ---------------- 画 ----------------

    @llm_tool(name="ming_paint")
    async def ming_paint(
        self,
        event: AstrMessageEvent,
        prompt: str,
        n: int = 1,
        w: int = 832,
        h: int = 1216,
        seed: int = -1,
    ) -> str:
        """画一张图并发到当前聊天。用溟自己的画风（Animagine XL 4.0 + LoRA 旋钮）。

        重要：prompt 必须是**英文 danbooru tag**（底模不认中文）。先自己把中文需求
        翻成 tag，再传进来。例：「海边的白裙少女」→「1girl, white dress, beach, sunset, ocean」。

        出图要 40~120 秒。工具立刻返回「开画了」，画完会自己把图发出去（可能晚于你这句回复），
        所以调用后只要说一句「在画了」之类，**不要等**，也不要描述一张还没看到的图。

        不画 17+；写实真人脸、政治人物一律不画。

        Args:
            prompt(string): 英文 danbooru tag，逗号分隔。
            n(number): 几张，默认 1，最多 2。
            w(number): 宽，832/1216 两档为主。
            h(number): 高，832/1216 两档为主。
            seed(number): -1 随机；记下回执里的 seed 就能复现。
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return json.dumps({"ok": False, "error": "prompt 为空"}, ensure_ascii=False)
        if not os.path.exists(PAINT_PY):
            return json.dumps({"ok": False, "error": f"找不到 {PAINT_PY}"}, ensure_ascii=False)
        if self._painting:
            return json.dumps(
                {"ok": False, "error": "上一张还在画，画完再说"}, ensure_ascii=False
            )
        n = max(1, min(int(n or 1), 2))
        w = max(512, min(int(w or 832), 1536))
        h = max(512, min(int(h or 1216), 1536))
        self._painting = True
        self._last_paint_at = time.time()
        asyncio.create_task(
            self._paint_job(event.unified_msg_origin, prompt, n, w, h, int(seed))
        )
        _log(f"PLUGIN-QUEUE {event.unified_msg_origin} prompt={prompt[:80]}")
        return json.dumps(
            {"ok": True, "status": "painting", "eta_sec": "40~120"},
            ensure_ascii=False,
        )

    async def _paint_job(self, umo: str, prompt: str, n: int, w: int, h: int, seed: int) -> None:
        t0 = time.time()
        try:
            os.makedirs(PAINT_OUT, exist_ok=True)
            cmd = [
                PAINT_PYTHON,
                PAINT_PY,
                "--prompt",
                prompt,
                "--n",
                str(n),
                "--w",
                str(w),
                "--h",
                str(h),
                "--seed",
                str(seed),
                "--out",
                PAINT_OUT,
            ]
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                cwd=os.path.dirname(PAINT_PY),
            )
            tail = ""
            for line in (proc.stdout or "").splitlines()[::-1]:
                if line.strip().startswith("{"):
                    tail = line.strip()
                    break
            if not tail:
                raise RuntimeError(
                    f"没有拿到 JSON（exit={proc.returncode}）: {(proc.stderr or '')[-400:]}"
                )
            res = json.loads(tail)
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "paint.py 返回 ok=false")
            files = [f for f in res.get("files", []) if os.path.exists(f)]
            if not files:
                raise RuntimeError("画是画完了，文件不在")
            chain = MessageChain(chain=[Image.fromFileSystem(f) for f in files])
            await self.context.send_message(umo, chain)
            _log(
                f"DONE {round(time.time() - t0, 1)}s seed={res.get('seed')} "
                f"files={len(files)} -> {umo}"
            )
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            logger.error(f"[ming_paint] {msg}")
            _log(f"FAIL {round(time.time() - t0, 1)}s {msg[:200]}")
            try:
                await self.context.send_message(
                    umo,
                    MessageChain(chain=[Plain(text=f"画失败了：{msg[:160]}")]),
                )
            except Exception:
                logger.error("[ming_paint] 连失败通知都没发出去")
        finally:
            self._painting = False

    # ---------------- 手打指令（测试用） ----------------

    @filter.command("ming_say")
    async def cmd_say(self, event: AstrMessageEvent, text: str = ""):
        """手打：/ming_say 在的，试试声音"""
        text = (text or event.message_str or "").strip()
        text = text.replace("/ming_say", "").strip()
        if not text:
            yield event.plain_result("要说什么？用法：/ming_say 一句话")
            return
        try:
            mp3, _eng = await self._synth(text)
        except Exception as e:
            yield event.plain_result(f"合成失败：{type(e).__name__}: {e}")
            return
        yield event.chain_result([Record(file=mp3, url=mp3)])

    @filter.command("ming_draw")
    async def cmd_draw(self, event: AstrMessageEvent, prompt: str = ""):
        """手打：/ming_draw 1girl, white dress, beach, sunset"""
        prompt = (prompt or "").strip()
        prompt = prompt.replace("/ming_draw", "").strip()
        if not prompt:
            yield event.plain_result("用法：/ming_draw <英文 tag>")
            return
        if self._painting:
            yield event.plain_result("上一张还在画")
            return
        self._painting = True
        asyncio.create_task(
            self._paint_job(event.unified_msg_origin, prompt, 1, 832, 1216, -1)
        )
        yield event.plain_result("开画了，40~120 秒")

    async def terminate(self):
        pass
