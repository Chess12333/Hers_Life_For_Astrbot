# 安装问题回执（2026-11-23）

对着 `安装问题.md` 逐条处理的结果。改都在两个仓里：
`C:\dsh\Hers_Life`（DSH 插件）与 `C:\dsh\Hers_Life_For_Astrbot`（AstrBot 插件）。

**一句话**：你那四条「换机器真会拦住人」的，现在都不需要改代码了——设四个环境变量就行。

## 1. 必须改

### 1.1 `qqObservations` 默认空串却直接进 readFile —— 已改
`dsh-ming-memory/lib/neuron.mjs`：`readQqObservations()` 开头加 `if (!file) return []`。
验过：`node -e "readQqObservations('')"` → `[]`，不再 ENOENT。

### 1.2 工作区在加载期算成常量 —— 已改成每次现算
`dsh-ming-graph/index.js`：`const WORKSPACE = ...` → `function workspace()`，6 处调用点（`skillsCwd` /
`costPricesFile` / `costAutoFile` 改成 getter，`readAcp` 与 `/acp` 路由每次现读）。
现在：建好 `~/.dsh/ming-graph.json` 后**点一次面板刷新就行，不用重启 DSH**。
其余非路径键（`ownerName` / `roomLabels`）仍是加载期读一次，这条差异写进了 README。
配置键全表也补了（`dsh-ming-graph/README.md`「键一览」）。

### 1.3 缺 requirements.txt —— 补了，但不是 24 个都需要
扫过全部 import：真正**无条件** import 第三方模块的只有两个插件。
- `astrbot_plugin_ming_voice_paint/requirements.txt` → `edge-tts>=6.1.9`
- `astrbot_plugin_ming_bili/requirements.txt` → `aiohttp>=3.9`
其余插件只用标准库 + `astrbot.api`；`Pillow` / `numpy` / `dashscope` 是**可选**的（都在 try 里，缺了只降级），
故意没写进 requirements——写进去反而会在网络差的机器上让整个插件加载失败。
`tools/check-placeholders.py` 会自动查「谁 import 了第三方却没有 requirements.txt」。

### 1.4 路径写死且两种写法 —— 已统一到环境变量
21 个文件、52 处常量改成：
```python
_MING_WS = os.environ.get("MING_WORKSPACE") or r"C:\dsh\hers_workplace"
```
后面所有 `ROOT` / `LIVE` / `HER` / `BUS` / `PROBE` / `KNOWN_GROUPS` 都由它派生。
包括你踩到的那处拼接式（`ming_sendfile`：`"C:" + os.sep + ...`）——现在它也是 `_MING_WS`，没有第二种写法了。
**换机器不用再逐个文件搜索替换。**

### 1.5 脱敏号是假号，且没有检查手段 —— 加了检查脚本，号也能用环境变量覆盖
- 新增 `tools/check-placeholders.py`：列出占位号/写死路径的 **文件:行号**，分「代码 / 默认值 / 说明」三类；
  同时打印哪些环境变量没设；再查 requirements。 `--strict` 有代码级残留就返回 1。
- 关键号接上了环境变量：`MING_HER_QQ`（`HER_QQ`/`OWNER_QQ`/`HER_ID`）、`MING_OWNERS`、`MING_GROUPS`、
  `MING_ABOUT_BY_QQ`、`MING_OWNER_UMO`。不设就用原来的占位值，行为不变。

### 1.6 平台 id 写死 `ming:` —— 已参数化，5 处全改
- `ming_proactive`：`UMO` = `MING_OWNER_UMO` 或 `f"{PLATFORM_ID}:FriendMessage:{HER_QQ}"`
- `obs_export`：`get_using_provider(f"{PLATFORM_ID}:FriendMessage:...)"`
- `ming_sendfile`：caption 的 umo 前缀
- `reply_policy` / `ming_persona`：`umo.startswith(PLATFORM_ID + ":")` → 返回 `SELF_NAME`
- `MING_PLATFORM_ID` 默认 `ming`，`MING_SELF_NAME` 默认「溟」——你那台设成 `snowluma` / 「鲸鲸」即可。
- 额外加一条保险：`ming_voice` 的白名单如果全是别的平台的 umo，会 **warning 一次**说清原因
  （以前是静默不发语音，谁都查不出来）。

## 2. 建议改

| 条 | 处理 |
|---|---|
| 2.1 / 2.7 人设只同步一次 | 已改：`sync_persona` 拆出 `_push_persona()`，另起后台任务每 20 秒 stat 一次 `live/qq-persona.md`，mtime 变了就重新同步。不用重启 AstrBot |
| 2.2 `<system_reminder>` 尾巴进上下文 | 已改两处：`inbox_record` 落盘前剥（源头），`dsh-ming-interest` 读入时再剥（兜底） |
| 2.3 `obs_export` 旧壳绝对路径 | 已改：`OLD_OBS = os.environ.get("MING_OLD_OBS") or ""`，空串在 `os.path.exists` 那里直接跳过 |
| 2.4 默认值会「自己动起来」 | 已改：`dsh-ming-browse` / `dsh-ming-interest` 的 `enabled`、`dsh-ming-mousepet` 的 `companion.enabled` 全部默认 `false`，各带一行原因 |
| 2.5 占位符写法不一致 | 已改：`dsh-ming-interest` 的 `inboxPath` / `chatSessionsPath` 改成相对路径（`live/inbox.jsonl`），代码里相对路径按 `workspacePath` 解 —— 占位符不再出现在字符串中间 |
| 2.6 burst 的 20 秒防抖 | 第三方插件，没动代码；写进 README 的依赖小节（私聊把自己 umo 加进 `bypass_umos`） |

## 3. 环境问题

`dsh\Hers_Life\docs\install.md` 新增第 9 节，把你列的 WSL keepalive / compose 端口 / noVNC 密码 /
镜像与网络绕法 / ubuntu-base 没 CA 没 systemd 全部收进去。

**3.5 `launcher.bat` 调 PATH 里的 node** —— 这两个仓里没有 `launcher.bat`（它在打包发布那个仓），
这条没能在本仓修，需要到那个仓改 `"%~dp0node.exe" ./index.mjs`。

## 4. 你本机改的那些

不需要再改代码了。你那台只需要在启动 AstrBot / DSH 的环境里设：

```
MING_WORKSPACE=D:\DSH\SOME_FISH
MING_PLATFORM_ID=snowluma
MING_SELF_NAME=鲸鲸
MING_HER_QQ=<你的号>
MING_GROUPS=（有群再填）
```

然后 `python tools/check-placeholders.py` 核对一遍。

## 5. 验证到什么程度

- 24 个插件的 `main.py` + 改过的 `tools/*.py`：全部 `ast.parse` 通过（语法层）。
- `index.js` / `neuron.mjs`：`node --check` 通过；`readQqObservations('')` 实测返回 `[]`。
- `tools/check-placeholders.py`：在本仓实跑，输出正常。`--strict` 当时会返回 1（剩下的都是 `_conf_schema` 默认值和示例文案）；
  **当晚这一条已经清掉了**：死号接上 `MING_HER_QQ` / `MING_QUIET_EXEMPT`，配置模板默认值与 `qa_` 脚本不再算代码残留，现在返回 0。
- **没验的**：没有真起一台 AstrBot 跑一轮对话，也没有重启 DSH 看面板。语法层干净不等于运行期没坑，
  尤其 `persona` 那个后台任务和 `dsh-ming-graph` 的 getter 展开——装完第一件事看日志有没有 warning。
