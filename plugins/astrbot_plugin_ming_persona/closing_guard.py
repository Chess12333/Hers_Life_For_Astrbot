"""私聊「收尾沉默」判据（纯函数，不碰模型、不发送、不写库）。

来历：2026-11-11 夜读 menglimi/astrbot_plugin_private_companion 的
`companion_v2/conversation/private_closing.py`，把它的判据搬过来自己写，
不是装插件。原版结构更复杂（带配置校验、对话历史时间校验），这里只留
真正决定「这一轮要不要静默」的那一部分。

为什么需要它：AstrBot 的私聊回复没有「不说话」这个动作——消息一进来
就是一次模型请求、一条回复。两边互相道晚安时，它会一直补下去。

判据（故意保守，宁可不静默）：
  1. 本轮消息必须【整条】都是收尾词汇（晚安/拜拜/谢谢/嗯嗯/早安/亲亲…），
     中间只允许夹语气词。有任何问号 → 不算；有任何认不出的字 → 不算。
  2. 上一轮我自己的回复必须是【强收尾】结尾（晚安/道别/感谢/早安/亲昵），
     而且不含问句、请求、提醒一类的词。只说「好」「嗯」不算我收尾过。
  3. 距离我上一条发出不超过窗口（默认 30 分钟）。
  4. 带图/语音/文件的一律不算。
任何一条不满足就正常回复。
"""

from __future__ import annotations

import re
import unicodedata

WINDOW_SECONDS = 1800
MAX_CLOSING_CHARS = 48
MAX_BOT_CLOSING_CHARS = 120

GOODNIGHT = (
    "晚安", "安安", "好梦", "做个好梦", "梦里见", "梦里相见", "睡个好觉",
    "好好睡", "好好休息", "早点休息", "早点睡", "早睡", "快去睡", "去睡觉",
    "去睡了", "去睡啦", "睡觉去", "睡觉了", "睡觉啦", "该睡了", "要睡了",
    "我睡了", "我睡啦", "我先睡了", "我困了", "困了", "躺了", "上床了", "关灯了",
    "这就睡", "马上睡", "先睡了", "先睡啦", "睡啦", "睡喽", "睡了", "睡吧",
    "睡觉", "要早起", "得早起", "安",
    "goodnight", "night", "gn", "sweetdreams", "sleepwell",
)
FAREWELL = (
    "拜拜", "拜", "白白", "再见", "再会", "回见", "回聊", "明天见", "明儿见",
    "早上见", "晚上见", "下次见", "改天见", "改天聊", "下次聊", "回头聊",
    "先这样", "先不聊了", "不聊了", "先撤了", "先撤", "撤了", "走了", "我走了",
    "下线了", "下了", "先下了", "我去忙了", "忙去了", "去忙了", "先忙了",
    "去吃饭了", "吃饭去了", "出门了", "去洗澡了", "洗澡去了", "路上小心", "慢走",
    "goodbye", "bye", "seeyou",
)
THANKS = ("谢谢", "多谢", "感谢", "辛苦", "麻烦你了", "谢啦", "谢了", "thanks", "thx")
ACK = (
    "好的", "好", "好哒", "好嘞", "好呀", "好啊", "好吧", "好啦", "行", "行吧",
    "可以", "收到", "知道了", "知道啦", "明白", "明白了", "了解", "没事", "没关系",
    "那就好", "嗯", "嗯嗯", "嗯呢", "哦", "噢", "喔", "ok", "okay",
)
MORNING = ("早安", "早上好", "早呀", "早啊", "早", "morning")
AFFECTION = ("爱你", "么么哒", "么么", "亲亲", "抱抱", "啵啵", "mua")

TOKEN_KINDS = {
    "goodnight": GOODNIGHT,
    "farewell": FAREWELL,
    "thanks": THANKS,
    "ack": ACK,
    "morning": MORNING,
    "affection": AFFECTION,
}
# 只有这些算「我真的收尾了」；一句「好」「嗯」不能替我道过晚安。
STRONG_KINDS = frozenset(("goodnight", "farewell", "thanks", "morning", "affection"))

_TOKEN_KIND: dict[str, str] = {}
for _kind, _tokens in TOKEN_KINDS.items():
    for _t in _tokens:
        _TOKEN_KIND.setdefault(_t, _kind)
_TOKENS = tuple(sorted(_TOKEN_KIND, key=len, reverse=True))

# 语气词和称呼：让「晚安啦」「那我也睡了」「宝宝晚安」还算同一句收尾。
_FILLERS = tuple(sorted({
    "啦", "呀", "啊", "呢", "吧", "哈", "嘛", "咯", "喽", "哒", "滴", "嘞", "哟", "呦",
    "诶", "欸", "了", "的", "我", "你", "您", "也", "都", "就", "先", "那", "那么",
    "再", "快", "乖", "是", "宝贝", "亲爱的", "宝宝", "小可爱", "大家",
    "今天", "今晚", "明天", "早上", "晚上", "现在", "已经", "一会儿", "一下",
} - set(_TOKEN_KIND), key=len, reverse=True))

_STRONG_TOKENS = tuple(sorted(
    (t for t, k in _TOKEN_KIND.items() if k in STRONG_KINDS), key=len, reverse=True))

_MEDIA = re.compile(
    r"(?:\[(?:图片|表情|语音|音频|视频|文件|合并转发|引用消息|分享卡片)[^\]]*\])"
    r"|(?:【(?:图片|表情|语音|音频|视频|文件|合并转发|引用消息|分享卡片)[^】]*】)")
# 我上一轮里出现这些，就说明那轮不只是道别（有问题、有请求、有安排），不能算收尾。
_QUESTION_OR_REQUEST = re.compile(
    r"[?？]|吗|几点|要不要|好不好|行不行|可以吗|好吗|你说|你觉得|想不想|去不去|"
    r"有没有|是不是|会不会|能不能|需要我|记得|提醒|叫我|告诉我|发给我|帮我|陪我|听我说")


def normalize(text: str) -> str:
    """去掉媒体标记、空白、标点符号和大小写差异；数字保留。"""
    compact = _MEDIA.sub("", str(text or ""))
    kept = []
    for ch in compact:
        if ch.isspace() or unicodedata.category(ch)[0] in "PSCZ":
            continue
        kept.append(ch)
    return "".join(kept).casefold()


def _scan(compact: str):
    """把整条文本当成「收尾词 + 语气词」来消费；有一个字不认识就返回 ()。"""
    i, kinds = 0, []
    while i < len(compact):
        for token in _TOKENS:
            if compact.startswith(token, i):
                i += len(token)
                kind = _TOKEN_KIND[token]
                if kind not in kinds:
                    kinds.append(kind)
                break
        else:
            for filler in _FILLERS:
                if compact.startswith(filler, i):
                    i += len(filler)
                    break
            else:
                return ()
    return tuple(kinds) if kinds else ()


def closing_categories(text: str):
    """整条消息只是收尾时，返回它属于哪几类；否则返回 ()。"""
    raw = str(text or "")
    if "?" in raw or "？" in raw:
        return ()
    compact = normalize(raw)
    if not compact or len(compact) > MAX_CLOSING_CHARS:
        return ()
    return _scan(compact)


def is_closing(text: str) -> bool:
    return bool(closing_categories(text))


def has_media(text: str) -> bool:
    return bool(_MEDIA.search(str(text or "")))


def _ends_with_strong(compact: str) -> bool:
    if not compact:
        return False
    for _ in range(3):
        if any(compact.endswith(t) for t in _STRONG_TOKENS):
            return True
        for filler in _FILLERS:
            if compact.endswith(filler) and len(compact) > len(filler):
                compact = compact[:-len(filler)]
                break
        else:
            return False
    return False


def role_closed(text: str) -> bool:
    """我上一条回复是不是「强收尾」：末尾是收尾词，且没有问句/请求。"""
    raw = str(text or "")
    if not raw.strip():
        return False
    if _QUESTION_OR_REQUEST.search(raw):
        return False
    compact = normalize(raw)
    if not compact or len(compact) > MAX_BOT_CLOSING_CHARS:
        return False
    kinds = _scan(compact)
    if kinds and any(k in STRONG_KINDS for k in kinds):
        return True
    return _ends_with_strong(compact)


def last_assistant_text(contexts) -> str:
    """从 req.contexts（[{'role','content'}]）里取我最后一条说的话。"""
    for msg in reversed(list(contexts or [])):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def should_silence(*, current_text: str, contexts, gap_seconds: float) -> tuple[bool, str]:
    """这一轮要不要不吭声。返回 (静默?, 原因)。"""
    if has_media(current_text):
        return False, "media_input"
    categories = closing_categories(current_text)
    if not categories:
        return False, "not_a_closing"
    # 「早」「亲亲」不算该吞的收尾：早安是开新的一天，亲昵是撒娇——
    # 拿一句晚安去挡这两样，就不是省话，是冷落。
    if any(kind in ("morning", "affection") for kind in categories):
        return False, "opening_not_closing"
    if gap_seconds > WINDOW_SECONDS:
        return False, "outside_window"
    last = last_assistant_text(contexts)
    if not last:
        return False, "no_role_reply"
    if not role_closed(last):
        return False, "role_did_not_close"
    return True, "role_already_closed"
