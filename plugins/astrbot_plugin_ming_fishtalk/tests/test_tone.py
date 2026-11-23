"""自测：规则、句式、判断层、语料。跑法——

    cd C:\\dsh\\astrbot\\data\\plugins\\astrbot_plugin_ming_fishtalk
    C:\\dsh\\astrbot\\venv\\Scripts\\python.exe tests\\test_tone.py

不依赖 astrbot，只测 tone/ 这一层（main.py 的钩子在重启后验）。
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from tone import corpus, judge, rules, structures  # noqa: E402

FAILED: list[str] = []


def ok(cond, name: str):
    if cond:
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name}")


def eq(a, b, name: str):
    ok(a == b, f"{name}（{a!r} == {b!r}）")


print("── 归一化与相似度")
eq(rules.normalize("我在，想你了！"), "我在想你了", "去标点")
eq(rules.normalize("[doge]好"), "好", "去方括号表情")
ok(rules.jaccard("今天好累", "今天好累") == 1.0, "同句相似度 1")
ok(rules.jaccard("今天好累", "完全不相干") < 0.2, "无关句相似度低")
eq(rules.lcs("我明天去看电影", "明天去看电影吧"), 6, "最长公共子串")

print("── 硬证据 / 软信号")
r = rules.score("这条我认")
ok("这条我认" in r["hard"], "硬原句命中")
ok(not rules.score("他说「这条我认」，笑死").get("hard"), "引号里的原句不算")
ok(not rules.score("1. 这条我认\n2. 我记下了\n3. 收到主人")["hard"], "编号列表里的不算")
r = rules.score("这不是钱的问题，而是时间的问题。本质上就是拖延。")
ok(r["fish"], "两条软信号判鱼")
ok("not-a-but-b" in r["soft"] and "itself" in r["soft"], "软信号都抓到")

print("── 黑名单")
hits = rules.blacklist_hits("（尾巴摇摇）好开心")
ok(any("动作括号" in h for h in hits), "角色扮演动作括号")
ok(any("客服腔" in h for h in rules.blacklist_hits("希望这些对你有帮助")), "客服腔")

print("── 结构腔")
rows = structures.scan("这不是钱的问题，而是时间的问题。")
eq([r["id"] for r in rows], ["not-but"], "命中 not-but")
b = structures.budget(1000)
eq(b["not-but"], 0, "千字里 strict 预算为 0")
ok(b["emdash"] >= 1, "千字里 soft 至少给 1")
d = structures.density("先说清楚再动手。")
ok(d["total"] >= 1, "先…再… 算一个")

print("── 判断层")
eq(judge.register_of("哈哈哈好耶"), "chat", "闲聊体")
eq(judge.register_of("第一步开火，然后翻炒，接着装盘"), "steps", "步骤体")
txt = "不是他懒而是他怕。最难受的是没人说。先说清楚再动手。"
v = judge.assess(txt)
ok(v["cluster"]["maxDistinct"] >= 3, "三种句式扎堆")
eq(v["band"], "template", "判成模板腔")
v2 = judge.assess("今天挺累的")
eq(v2["band"], "clean", "短句干净")
h = {"id": "not-but", "at": 0, "span": "不是他很懒，而是他懒", "tier": "strict", "name": "不是X而是Y"}
n = judge.necessity(h, "不是他很懒，而是他懒。", "note")
eq(n["verdict"], "cut", "同义对立判删")
h2 = {"id": "not-but", "at": 8, "span": "不是别人说的那样，而是他自己选的", "tier": "strict", "name": "不是X而是Y"}
n2 = judge.necessity(h2, "有人以为他是被逼的，不是别人说的那样，而是他自己选的。", "note")
eq(n2["verdict"], "keep", "前面种过误会就留")
ok("判断" in judge.render(v), "render 出结论")

print("── 语料 / 文风包")
s = corpus.scrub("看这个 https://x.com/a 打 13812345678 好开心[doge]")
ok("http" not in s and "13812345678" not in s and "doge" not in s, "脱敏与去表情")
b = corpus.brief("QQ群")
if b:
    ok("说话的松紧" in b, "注入片段有标题")
    ok(len(b) <= 700, f"片段不超 700 字（{len(b)}）")
    print("    片段预览：", b.replace("\n", " / ")[:120], "…")
else:
    print("    （这台机器没有 ~/.fishtalk/pack.json，跳过文风包检查）")

print("── check")
res = rules.check("今天有点累，先睡了")
ok(res["ok"], "干净的话通过")
res = rules.check("值得一提的是，这个方案很好")
ok(not res["ok"], "报告腔被拦")

print()
if FAILED:
    print(f"失败 {len(FAILED)} 条：")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全过")
