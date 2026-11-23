# Hers_Life For AstrBot

一只住在 AstrBot 里的 AI 的骨架。它拆成两半：**QQ 那侧负责活着，本机工作区那侧负责「我是谁、我记得什么」。**

- **Hers Life** —— 一个普通文件夹。人设卡、日记、见闻、群聊流水、待办、记忆笔记，全是明文文件。没有数据库，没有服务。
- **AstrBot 插件组** —— 下面这 24 个插件，把那个文件夹接进 QQ：读人设、记流水、按心跳开口、把话切短、发完记账。

两半之间没有中间层，只有文件和 JSONL。一侧写下的，另一侧下一轮就读得到——这就是全部联动。

## 两半怎么咬合

| 工作区里的东西 | 谁写 | 谁读 | 作用 |
| --- | --- | --- | --- |
| `live/qq-persona.md` | 本机 | `ming_persona` | 人设卡，按 mtime 每轮覆盖 system prompt |
| `live/bus.jsonl` | 两侧都写 | `ming_bus` | 只递四类：定下来的事、答应的、改了哪个文件、非留不可的原话 |
| `live/qq-outbox/` | 本机 | `ming_outbox` | 本机想说的话落成 json，QQ 侧发出去 |
| `qq-inbox.jsonl` | `inbox_record` | 本机 | 群里的话落盘，本机按意思搜 |
| `live/qq-said.jsonl` | `ming_said` | 本机 | QQ 侧说过的每一句，本机知道 |
| `live/qq-live/` | `ming_live_history` | `ming_persona` | 最近几轮对话，按话题段注入 |
| `notes/` | `ming_memory` / 本机 | 两侧 | 归档记忆同一份，谁都能读 |
| 本机 DSH 会话 | `dsh_acp` | — | QQ 里派的活丢给本机跑，结果回一句话 |

一句话：**QQ 是嘴和耳朵，工作区是脑子和日记本。** 插件做的是这两者之间的神经。

为什么这么切：模型侧的重活（读文件、抓网页、跑脚本、长篇思考）留在本机，QQ 那侧只负责即时反应。两边共用一个文件夹，所以「她刚才说了什么」「我上次写到哪」不需要通过对话历史传递——直接读文件。

## 设计上的几个取舍

- **没有状态机框架。** 该记的写文件，该判断的当场判断。
- **允许不说。** 心跳到点只是给一次机会，想不出内容就回 NONE。
- **先过闸再花钱。** 宵禁、对方刚说过话、今天开口已够——三道闸都在调模型之前。
- **嘴和脑子分开部署。** 换壳（QQ-Agent → AstrBot）时只换了嘴，脑子和日记一个字没动。
- **话要短。** 出站长句按标点切，一条一个意思。

## 插件表

<!-- PLUGIN_TABLE:START -->
| 目录 | 名称 | 作用 | 版本 |
| --- | --- | --- | --- |
| `astrbot_plugin_admin_deny_mute` | 溟·权限提示静音 | 群管插件拒绝非管理员指令时静默，不再在群里发「仅管理员可以使用此功能」 | 0.1.0 |
| `astrbot_plugin_dsh_acp` | 溟·DSH（ACP） | 走 ACP 协议调本机 DSH，独立工作区、独立会话 | 0.1.0 |
| `astrbot_plugin_forward_expand` | 合并转发展开 | 把 QQ 的合并转发消息展开成正文再交给模型 | 0.1.0 |
| `astrbot_plugin_inbox_record` | astrbot_plugin_inbox_record | 把入站消息写进 qq-inbox.jsonl，供 dsh-ming-interest 读取（AstrBot 时代的消息源） | 0.1.0 |
| `astrbot_plugin_ming_bili` | 溟·看B站 | 搜片、读简介、取文字稿、看热评、抽帧看图——不用登录态的先走匿名接口 | 0.1.0 |
| `astrbot_plugin_ming_bus` | 溟·总线 | 两个壳之间只递要紧的那一层。她那一间私聊注入 live/bus.jsonl 最近 15 条；工具 ming_bus 让这边写 d… | 0.1.0 |
| `astrbot_plugin_ming_fishtalk` | 溟·鱼话（人味 + 自检） | 照真人基线说话，说完自己过一遍：鱼八股、AI 腔、结构腔扎堆 | 0.1.0 |
| `astrbot_plugin_ming_group_roles` | 溟·群角色识别 | 认得出群里谁是群主、谁是管理员（缓存 6 小时，启动即拉） | 0.1.0 |
| `astrbot_plugin_ming_interest` | astrbot_plugin_ming_interest | 兴趣唤醒：群里说到我在乎的事，才接一句（AstrBot 时代，替代 dsh-ming-interest 的叫醒腿） | 0.1.0 |
| `astrbot_plugin_ming_learn` | 溟·学群话 | 学群话：相邻两条不同人的真人消息算一对，数次数 + 二次衰减（0.01*(天数/15)^2），掉到 0.2 删。不调 LLM、不… | 0.1.0 |
| `astrbot_plugin_ming_live_history` | 溟·实时聊天流水 | 记录每个会话的消息流水，每轮按「话题段」注入（带时间与距现在多久） | 0.2.0 |
| `astrbot_plugin_ming_local_rp` | 溟 · 本地不过滤模型 | /rp 打开的那一间改走本机模型（Ollama 跑 Qwen2.5-14B abliterated GGUF Q4，127.0.… | 0.1.0 |
| `astrbot_plugin_ming_memory` | astrbot_plugin_ming_memory | 溟·记忆：在 QQ 侧直接搜/读/写归档记忆（共用 DSH 的 notes/），另带待办清单 | 0.1.0 |
| `astrbot_plugin_ming_outbox` | 溟·出站箱 | 消费 live/qq-outbox，把 DSH 侧写的气泡发到 QQ | 0.1.0 |
| `astrbot_plugin_ming_persona` | 溟·人设注入 | 从 hers_workplace 读人设与动态页，注入提示词 | 0.1.0 |
| `astrbot_plugin_ming_proactive` | astrbot_plugin_ming_proactive | 给自己的开口机会：三角分布心跳（45/75/120 分钟），过闸才调模型，允许不说 | 0.1.0 |
| `astrbot_plugin_ming_roleplay_gate` | 溟 · 角色扮演必读闸 | 角色扮演启用后第一轮把 SKILL.md 全文整份读进上下文（不拆页），之后每轮只留路径提示。 | 0.1.0 |
| `astrbot_plugin_ming_said` | 溟·出站记账 | 出站前把这一句落到 live/qq-said.jsonl。不改内容、不拦、不重发。只盖被动回话，主动起话各自有账。 | 0.1.0 |
| `astrbot_plugin_ming_sendfile` | 溟·主动发文件 | 工具 ming_send_file 直接把工作区里的文件发到群/私聊；另有 live/file-outbox 收件箱，任何一边丢… | 0.1.0 |
| `astrbot_plugin_ming_typing` | 溟·输入状态回执 | 私聊里回消息的时候给 QQ 发「正在输入」，发完取消 | 0.1.0 |
| `astrbot_plugin_ming_voice` | 溟·嗓子 | 用百炼 cosyvoice 复刻音色把文字回复换成语音，按会话白名单生效 | 0.1.0 |
| `astrbot_plugin_ming_voice_paint` | astrbot_plugin_ming_voice_paint | 溟的声音和画：edge-tts 发语音消息，paint.py 出图发 QQ | 0.1.0 |
| `astrbot_plugin_obs_export` | astrbot_plugin_obs_export | 把对话炼成观察喂给星图：消息归档 + 定期提炼（QQ-Agent 旧库与新话合并） | 0.1.0 |
| `astrbot_plugin_reply_policy` | astrbot_plugin_reply_policy | 群聊回复概率与每分钟额度，私聊不受限 | 0.1.0 |
<!-- PLUGIN_TABLE:END -->

## 这套部署里还装了哪些别人的插件

下面这 11 个**不在本仓库里**，是各自作者的 AstrBot 插件，从插件市场或 GitHub 直接装的。列出来是因为上面那 24 个不是孤立工作的——它们默认身旁有这些插件在跑。要装请去原作者仓库拿，本仓库不复制、不分发、不打包别人的代码。

| 插件 | 作者 | 出处 | 在这套里干什么 |
| --- | --- | --- | --- |
| `astrbot_plugin_burst` | Lansheen3096 | [GitHub](https://github.com/Lansheen3096/astrbot_plugin_burst) | 入站防抖：连发的几条先合并成一条再交给模型，省 token 也更好懂 |
| `astrbot_plugin_pacer` | Lansheen3096 | [GitHub](https://github.com/Lansheen3096/astrbot_plugin_pacer) | 出站打断：对方插话就停发剩余分段，并把历史截成实际发出去的部分 |
| `astrbot_plugin_group_guardian` | zhaisir | [GitHub](https://github.com/zcj-ui/astrbot_plugin_group_guardian) | 群管 + LLM 审核：28 项群管工具、正则初筛 + 上下文审核、WebUI 面板 |
| `astrbot_plugin_qq_tools` | YUMU1658 | [GitHub](https://github.com/YUMU1658/astrbot_plugin_qq_tools) | QQ 侧动手能力：引用回复、撤回、检索最近消息、群管、网页、视频分析、定时唤醒 |
| `astrbot_plugin_astrbot_enhance_mode` | 阿汐 | [GitHub](https://github.com/Axi404/astrbot_plugin_astrbot_enhance_mode) | 群聊增强：React 模式、主动回复、标签解析、封禁控制、Memory RAG + WebUI |
| `astrbot_plugin_anysearch` | AgIzT | [GitHub](https://github.com/AgIzT/astrbot_plugin_anysearch) | 联网搜索：通用网页、新闻、文档检索与正文提取 |
| `astrbot_plugin_sticker_sense` | YanZephyr14 & AstrBot Community | [GitHub](https://github.com/YanZephyr14/astrbot_plugin_sticker_sense) | 表情感知：区分表情包与照片，语义化解析 QQ 表情，让模型会接梗 |
| `astrbot_plugin_meme_manager` | anka | [GitHub](https://github.com/anka-afk/astrbot_plugin_meme_manager) | 表情包管理：发送、语义检索、云端同步 |
| `astrbot_plugin_smart_imagechat_hub` | 青尘工作室 | [GitHub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) | 图片对话：多模态标签、斗图、主动表情 |
| `astrbot_plugin_get_px` | Sham1k0 | [GitHub](https://github.com/shitianyaa/astrbot_plugin_get_px) | 发图与签到：Lolicon / Pixiv 双源回退，分群内容规则 |
| `astrbot_plugin_codeforces_helper` | Zinc-acetate | [GitHub](https://github.com/Zinc-acetate/astrbot_plugin_codeforces_helper) | Codeforces 训练助手：AC 记录同步、Rating、排行榜、比赛提醒 |

**分工上的咬合点**（为什么这些不能省）：

- `burst` 管入站合流，`pacer` 管出站被打断，中间那段「怎么切、怎么发、发完记账」才是 `ming_outbox` / `ming_said` 的活。三者不重叠。
- `group_guardian` 做的是「该不该撤回、该不该禁」，`reply_policy` 做的是「这一轮该不该开口、能开几句话」。一个管内容，一个管频率。
- `sticker_sense` / `meme_manager` / `smart_imagechat_hub` 负责把图变成可读的东西和可发的表情；没有它们，这套插件收到的图只是一串链接。

**注意：其中 6 个我们本地改过代码**（`pacer` 16 处备份、`burst` 3 处，`enhance_mode` / `group_guardian` / `qq_tools` / `smart_imagechat_hub` 各 1 处）。改动只留在本机，没有提 PR、没有回馈上游。所以你装原版时，行为可能和这里描述的不完全一样。

另外 AstrBot 根下还有 `data/plugins-off/`、`plugins_off/` 两个停用目录，里面是试过但没开的插件，不计入上表。
## 安装

```bash
git clone https://github.com/Chess12333/Hers_Life_For_Astrbot.git
cd Hers_Life_For_Astrbot
```

把要用的目录整个拷进 AstrBot 的 `data/plugins/`，重启 AstrBot：

```bash
cp -r plugins/astrbot_plugin_ming_persona /path/to/astrbot/data/plugins/
```

AstrBot 启动时会加载 `data/plugins/<name>/main.py`，插件会出现在 WebUI 的「插件管理」里，配置项看各插件的 `_conf_schema.json`。

## 配置与依赖

- **没有内置任何密钥。** 需要 API Key 的插件（语音、画图、模型）在自己的 `_conf_schema.json` 对应字段里填，或走 AstrBot 的 provider 配置。
- **工作区不用改代码了（2026-11-23 起）。** 每个插件顶部的常量都是 `os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"`——换机器只要在启动 AstrBot 之前把环境变量设上，一个文件都不用动。

### 换机器：先设这几个环境变量

| 变量 | 作用 | 不设会怎样 |
|---|---|---|
| `MING_WORKSPACE` | 数据工作区根目录（`live/`、`people/`、`about/`、`diary/`） | 退回作者本机路径，读不到就静默空白，最难查 |
| `MING_PLATFORM_ID` | AstrBot 里这个平台的 id（umo 前缀，例如 `snowluma`） | 默认 `ming`；你的平台 id 不是它，主动消息/发文件/点名全部落空 |
| `MING_SELF_NAME` | 群里喊名字时认的那个称呼 | 默认「溟」；别人装这套，他的 AI 不叫溟 |
| `MING_HER_QQ` | 主人的 QQ 号（`HER_QQ` / `OWNER_QQ` / `HER_ID` 共用） | 退回占位号 `100000001`，等于谁都不是主人 |
| `MING_OWNERS` | 逗号分隔：算「自己人」的号 | 退回占位集合 |
| `MING_GROUPS` | 逗号分隔：要认的群号 | 退回占位集合（兴趣唤醒、主动发文件按它判断） |
| `MING_OWNER_UMO` | 主动消息投递的完整 umo | 按 `MING_PLATFORM_ID` + `MING_HER_QQ` 自动拼 |
| `MING_OLD_OBS` | 旧壳（QQ-Agent）`observations.jsonl` 路径 | 留空 = 不合并旧库，正常 |

设完跑一次自检（列出还剩哪些占位值、哪些变量没设、哪个插件缺 requirements）：

```bash
python tools/check-placeholders.py
```

**注意**：变量要设在**启动 AstrBot 的那个环境**里。Windows 上如果 AstrBot 是计划任务/服务起的，在另一个 cmd 窗口 `set` 是没用的。
- 依赖：默认只用标准库和 `astrbot.api`。只有两个插件带 `requirements.txt`（AstrBot 会自己读它装）：`ming_voice_paint` → `edge-tts`（顶部无条件 import，缺了整个插件不加载）、`ming_bili` → `aiohttp`。
- `Pillow`（拼图）、`numpy`（记忆向量）、`dashscope`（语音引擎）都是**可选**的，缺了只有对应功能降级，不会让插件加载失败。
- 第三方插件 `astrbot_plugin_burst` 默认 `idle_seconds: 20`：连发的消息合并成一条再交给模型，群里这样是对的，**私聊里就像卡住了**。想立刻回就把自己的 umo 加进它的 `bypass_umos`。
- `astrbot_version` 标的是 `>=4.16,<5`，旧版本 API 可能对不上。

## 关于脱敏

这个仓库是从一台真在跑的机器上同步出来的，同步时会把能指认到具体人的字符串换成占位符（`tools/sync-from-live.mjs` 里的 `REDACT` 表）：

- `1000000xx` —— QQ 号占位（`100000001` 是主人，其余是白名单里的其他人）
- `7000000xx` —— 群号占位
- `C:\Users\you` —— 本机用户名
- `她` / `群友` / `另一个使用者` —— 人名与群友昵称

占位符只改字面，不改逻辑：`GROUPS`、`OWNERS`、`HER_QQ` 这些集合的结构原样保留，换回你以为的号就能跑。

**脱敏表管得住号码和人名，管不住「她的原话被写进提示词」。** 那一类靠自检的第五项：

```bash
python tools/check-placeholders.py --strict
```

它只看**会进模型**的字符串常量（docstring、版本号、正则不算），查四种：她的原话被嵌进提示词（`她…说「三四个字以上」`）、日期旁边还带着具体时刻、真实群名、脱敏表里的真实人名。有命中就返回 1——这一类没有「默认值」可退，留着就是留在人眼前。

从线上同步之后也顺手跑一遍，`sync-from-live.mjs` 末尾会提醒。

## 这个仓库里没有什么

同步脚本（`tools/sync-from-live.mjs`）挡在外面：

- `*.bak-*` / `.before-*` / `.pretest`（改代码时的备份，线上有上百个）
- `__pycache__`、`*.pyc`
- `audio/`（TTS 缓存）
- 配置真值、人设正文、群聊语料、日记与见闻——这些从来不在插件目录里，也不会被同步过来

## 开发

源码在 `plugins/` 下，改完直接提交。从线上运行环境重新同步：

```bash
node tools/sync-from-live.mjs --src=/path/to/astrbot/data/plugins --dry   # 先看
node tools/sync-from-live.mjs --src=/path/to/astrbot/data/plugins        # 再写
node tools/gen-readme.mjs                                               # 更新上面的表
```

## License

MIT，见 [LICENSE](LICENSE)。
