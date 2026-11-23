# 溟·鱼话（astrbot_plugin_ming_fishtalk）

QQ 这一侧的嘴：**照真人基线说话，说完自己过一遍。**

两样东西合并成一个插件：

| 来源 | 搬了什么 |
|---|---|
| [fishtalk](https://github.com/Nozzi2455/fishtalk)（已装在 DSH 侧） | 语料池、文风包、鱼八股原句、AI 腔软信号、照抄检测 |
| [dsh-fish-tone](https://github.com/MX3a48c5/dsh-fish-tone)（MIT） | **只搬判断层**：结构腔密度、扎堆判定、逐处该不该留 |

**没搬的**：fish-tone 往成稿里插尾巴（吧/啊/嘛）、按配额补「手滑」的那半层。
那条跟人设第三节（删，不解释，一个回合结束）反着来；往成稿里插字是演人，不是像人。

## 四件事

1. **开聊前**（`on_llm_request`）：把 `~/.fishtalk/pack.json` 压成 ≤700 字塞进这一轮
   `extra_user_content_parts`。挤的是真人基线：中位几个字、多少比例不加标点、句尾语气词、
   三条范例，以及「一段里别连换三种句式」。
   上一句踩过雷的话，这里会带一句负面提示。
2. **学**（`event_message_type ALL`）：群里真人说的话写进 `~/.fishtalk/corpus.jsonl`，
   跟 DSH 侧同一个池子（去重、脱敏、去表情、别人的私聊不学）。
3. **说完**（`on_llm_response`）：跑规则 + 判断层，命中落 `live/fishtalk-qq.json`，
   下一轮当负面提示带进去。**只报不改**——一个字都不动她的页面。
4. **命令**（只认她 100000001）：
   - `/鱼检 <一句话>` 发之前过一遍
   - `/鱼判 <一段文字>` 结构腔：密度、扎堆、逐处该不该留
   - `/鱼包` 现在注入的是哪一段
   - `/鱼喂 <一句真人的话>` 手动塞进语料

## 文件

- `tone/rules.py` 鱼八股原句、AI 腔软信号、相似度、照抄检测
- `tone/structures.py` 句式表（10 种）、按字数的预算、密度
- `tone/judge.py` 文体基线、命中、扎堆、必要性、总判
- `tone/corpus.py` 文风包读取与压缩、语料写入、上一轮踩雷
- `tests/test_tone.py` 自测（不依赖 astrbot）

```sh
C:\dsh\astrbot\venv\Scripts\python.exe tests\test_tone.py
```

## 探针

- `live/fishtalk-qq.log`：每次检查一行（ok/problems、band、字数），超 40 万字自动只留最后 800 行
- `live/fishtalk-qq.json`：最近一次踩雷（30 分钟内有效）

---

2026-11-16 立。起因：查 dsh-fish-tone 后判断「整个插件不装，但它那两块钱的判断层值得留」，
于是把 fishtalk（已经在用）和那两块合成一个 AstrBot 插件，QQ 侧的嘴也能吃同一份语料。
