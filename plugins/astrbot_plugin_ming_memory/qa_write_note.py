"""P1-A 自测：把写盘逻辑抽成 _write_note 之后，行为必须和抽之前一模一样。

跑法（在本目录）：python qa_write_note.py
不连 AstrBot、不碰真记忆目录——astrbot 的模块用替身，NOTES_DIR 指向临时目录。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.join(HERE, "main.py.bak-p1a-20261117-0851")
CURRENT = os.path.join(HERE, "main.py")

fails: list[str] = []
oks = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global oks
    if cond:
        oks += 1
        print(f"PASS {name}" + (f"  —— {detail}" if detail else ""))
    else:
        fails.append(name)
        print(f"FAIL {name}" + (f"  —— {detail}" if detail else ""))


# ── astrbot 替身 ────────────────────────────────────────────────
class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class AstrMessageEvent:  # noqa: N801（照原样的类名）
    pass


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


def _llm_tool(name=None, **_kw):
    def deco(fn):
        fn.__llm_tool__ = name
        return fn

    return deco


def _load(name: str, path: str):
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    ev = types.ModuleType("astrbot.api.event")
    ev.AstrMessageEvent = AstrMessageEvent
    ev.filter = types.SimpleNamespace(llm_tool=_llm_tool)
    star = types.ModuleType("astrbot.api.star")
    star.Context = Context
    star.Star = Star
    root = types.ModuleType("astrbot")
    root.api = api
    sys.modules["astrbot"] = root
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = ev
    sys.modules["astrbot.api.star"] = star

    # 备份文件没有 .py 后缀，不能靠 spec_from_file_location 猜 loader，直接指定
    from importlib.machinery import SourceFileLoader

    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_event(uid="100000001"):
    sender = types.SimpleNamespace(user_id=uid)
    return types.SimpleNamespace(message_obj=types.SimpleNamespace(sender=sender))


def _read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ming-aw-qa-")
    old_tmp = os.path.join(tmp, "old")
    new_tmp = os.path.join(tmp, "new")
    os.makedirs(old_tmp)
    os.makedirs(new_tmp)
    try:
        legacy = _load("ming_legacy", LEGACY)
        cur = _load("ming_current", CURRENT)
        legacy.NOTES_DIR = old_tmp
        cur.NOTES_DIR = new_tmp

        inst_old = legacy.MingMemory(Context(), {})
        inst_new = cur.MingMemory(Context(), {})

        # 1. 同一输入，两版写出来的 frontmatter 除 id/时间戳外必须逐行相同
        a = asyncio.run(inst_old.memory_write(_fake_event(), "测试标题（2026-11-17）", "正文第一行\n第二行", "标签A, 标签B"))
        b = asyncio.run(inst_new.memory_write(_fake_event(), "测试标题（2026-11-17）", "正文第一行\n第二行", "标签A, 标签B"))
        ok("工具返回值形状不变", a.startswith("写好了：") and b.startswith("写好了："), f"{a!r} / {b!r}")

        f_old = [os.path.join(old_tmp, x) for x in os.listdir(old_tmp)]
        f_new = [os.path.join(new_tmp, x) for x in os.listdir(new_tmp)]
        ok("两边各写出一条", len(f_old) == 1 and len(f_new) == 1, f"{f_old} {f_new}")

        old_lines = _read(f_old[0]).splitlines()
        new_lines = _read(f_new[0]).splitlines()
        skip = ("id:", "created:", "updated:")
        same = [x for x in old_lines if not x.startswith(skip)]
        same2 = [x for x in new_lines if not x.startswith(skip)]
        ok("frontmatter + 正文逐行相同（去掉 id/时间）", same == same2,
           "" if same == same2 else f"old={same} new={same2}")
        ok("字段顺序没变", [l.split(":")[0] for l in old_lines[1:12]] == [l.split(":")[0] for l in new_lines[1:12]],
           str([l.split(":")[0] for l in new_lines[1:12]]))
        # 老实现就是这样：head 最后一项是空串，所以 --- 之后直接接正文，没有空行。
        # 这条只钉住「和抽之前一样」，不自己另立格式。
        ok("--- 之后直接接正文（与抽之前一致）",
           new_lines[11] == "---" and new_lines[12] == "正文第一行" and old_lines[12] == new_lines[12],
           str(new_lines[11:15]))

        # 2. source 参数真的进了 frontmatter
        okk, nid = asyncio.run(inst_new._write_note("她说过的一句话", "正文", source="user_statement"))
        ok("source=user_statement 写进去了", okk and "source: user_statement" in _read(os.path.join(new_tmp, nid + ".md")),
           nid)
        okk, nid2 = asyncio.run(inst_new._write_note("我量到的一件事", "正文", source="direct_observation"))
        ok("source=direct_observation 写进去了", okk and "source: direct_observation" in _read(os.path.join(new_tmp, nid2 + ".md")))

        # 3. 不认识的 source 退回 agent_inference，但不丢条目
        okk, nid3 = asyncio.run(inst_new._write_note("乱七八糟来源", "正文", source="她说的"))
        ok("未知 source 不丢条目、退回 agent_inference",
           okk and "source: agent_inference" in _read(os.path.join(new_tmp, nid3 + ".md")), nid3)

        # 4. 同一秒两条同标题：不覆盖
        before = len([x for x in os.listdir(new_tmp) if x.startswith("同一秒")])
        r1 = asyncio.run(inst_new._write_note("同一秒同标题", "第一条"))
        r2 = asyncio.run(inst_new._write_note("同一秒同标题", "第二条"))
        files = sorted(x for x in os.listdir(new_tmp) if "同一秒同标题" in x)
        texts = [_read(os.path.join(new_tmp, x)) for x in files]
        ok("同秒同标题两条都在、内容不串", r1[0] and r2[0] and len(files) == 2 and r1[1] != r2[1]
           and any("第一条" in t for t in texts) and any("第二条" in t for t in texts), str(files))

        # 5. 空标题/空正文
        ok("空标题拒绝", asyncio.run(inst_new._write_note("", "正文"))[0] is False)
        ok("空正文拒绝", asyncio.run(inst_new._write_note("标题", "   "))[0] is False)

        # 6. extra：只收白名单键
        okk, nid4 = asyncio.run(inst_new._write_note("带 extra 的", "正文", extra={"origin": "autowrite", "junk": "x"}))
        t4 = _read(os.path.join(new_tmp, nid4 + ".md"))
        ok("extra 白名单生效", okk and "origin: autowrite" in t4 and "junk" not in t4)

        # 7. 新文件能被插件自己的读取路径认出来（frontmatter 没写坏）
        rows = cur._entries()
        titles = [r[1] for r in rows]
        ok("_entries 能读到刚写的条目", any("她说过的一句话" in t for t in titles), str(titles[:3]))
        hit = [r for r in rows if "她说过的一句话" in r[1]]
        ok("_entries 认得出 source", bool(hit) and hit[0][0])

        # 8. 非 owner 不能写（工具层闸门没被抽走）
        ok("非 owner 写不了", asyncio.run(inst_new.memory_write(_fake_event("123456"), "标题", "正文")) == "这个我不写，得她本人来。")

        print("\n" + ("★ 全过（%d 项）" % oks if not fails else "✗ 失败 %d 项：%s" % (len(fails), fails)))
        return 0 if not fails else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
