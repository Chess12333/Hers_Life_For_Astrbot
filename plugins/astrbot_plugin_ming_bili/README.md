# astrbot_plugin_ming_bili

给 QQ 这一侧的溟补上「看 B 站」。2026-11-16 立。

## 为什么有它

14:11 群里丢来一个 `b23.tv` 短链，模型调了现成的 `astrbot_plugin_biliread`，
一句「Credential 类未提供 sessdata 或者为空」就死了——B 站的字幕元数据要登录态，
它把这一步当成必经路径。那一轮只能转给 DSH 侧代看。

这里把「看」拆开，能不用登录态的就不碰。

## 有哪些能力

| 工具 | 干什么 | 要不要登录态 |
| --- | --- | --- |
| `bili_search(keyword, order, limit)` | 搜视频，拿 BV 号 | 不用（WBI 匿名签名） |
| `bili_video(url_or_bvid, with_comments, comment_limit)` | 标题、UP、时长、播放/点赞/投币、发布时间、简介、分P、热评 | 不用 |
| `bili_comments(url_or_bvid, sort, page, limit)` | 评论区（hot / time / reply） | 不用 |
| `bili_transcript(url_or_bvid, max_chars, allow_asr)` | 文字稿：匿名字幕轨 → bili-humanism CLI → 本地 ASR | 部分（走 CLI 时它自己带凭证文件） |
| `bili_frames(url_or_bvid, count=0, describe=True, interval=1.5, max_frames=24)` | 按秒间隔抽帧（默认 1.5s）→每 6 格一张拼图（帧多自动分多张，全部发到聊天）→逐张视觉描述；传 `count>0` 回到老模式（均匀抽 count 帧、只出一张） | 不用 |

手打入口：`/看b站 BV1xxxxxxxxx`（别名 `/看片`、`/bili`），出一张卡（标题、UP、数据、热评、封面）。

`url_or_bvid` 三种写法都收：`BV1Svs36LEdr`、`https://b23.tv/xxxx`、带 BV 的完整链接。

## 依赖

- `node` + `C:\dsh\hers_workplace\scripts\bili-frames.mjs`（抽帧：playurl → curl 落盘 → ffmpeg）
- `ffmpeg` 在 PATH
- `C:\dsh\hers_workplace\scripts\bili-get.py`（取稿，官方通道 + 本地 ASR）
- `C:\dsh\bili-humanism\bin\cli.mjs`（它自己读 `credential.json`，插件不读任何凭证）
- Pillow（AstrBot venv 里已有）

## 配置

`_conf_schema.json`：

- `vision_provider_id` — 描述画面的模型，要能看图。默认 `openai/mimo-v2.6-pro`。
- `enable_asr` — 没有字幕轨时走本地转写，默认开。
- `max_asr_seconds` — 超过这个秒数不自动转写，默认 900。
- `send_frames_to_chat` — 抽帧拼图要不要也发到聊天，默认发。

## 已知边界

- **写操作不在**：点赞、投币、收藏、评论都不做——那些留在 DSH 侧，那边有台账和护栏。
- 搜索撞风控（`request was banned`）就这一分钟别再重试，换关键词或隔一会儿。
- 匿名接口常常拿不到字幕轨（B 站把字幕元数据挂在登录态后面），所以文字稿多半走 ASR。
- 长片（> `max_asr_seconds`）不自动转写，工具会改为提示看简介和热评。
- 帧是 480P（无登录态最高），别指望认小字。

## 自测

`C:\dsh\tmp\test_ming_bili.py`、`test_ming_bili2.py` 是离线自测脚本：不起 AstrBot，
直接调工具函数，验证搜索 / 简介 / 评论 / 短链 / ASR / 抽帧六条路。
