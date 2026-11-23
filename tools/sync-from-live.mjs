#!/usr/bin/env node
// 把 AstrBot 的 data/plugins 下自研插件同步进本仓库的 plugins/ 目录。
// 同步时做两件事：滤掉缓存与备份，把能指认到人的字符串换成占位符。
//
// 脱敏表放在 tools/redact.local.json（不进仓库，见 .gitignore）。
// 没有这张表就只做过滤、不做脱敏，并在最后提示一句。
//
// 用法：
//   node tools/sync-from-live.mjs --dry
//   node tools/sync-from-live.mjs
//   node tools/sync-from-live.mjs --src=D:/somewhere/data/plugins
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const dstRoot = path.join(root, "plugins");

const args = process.argv.slice(2);
const dry = args.includes("--dry");
const srcArg = args.find((a) => a.startsWith("--src="));
const srcRoot = srcArg ? path.resolve(srcArg.slice(6)) : "C:/dsh/astrbot/data/plugins";

const PLUGINS = [
  "astrbot_plugin_ming_persona",
  "astrbot_plugin_ming_proactive",
  "astrbot_plugin_reply_policy",
  "astrbot_plugin_ming_outbox",
  "astrbot_plugin_ming_said",
  "astrbot_plugin_ming_bus",
  "astrbot_plugin_ming_memory",
  "astrbot_plugin_ming_learn",
  "astrbot_plugin_ming_interest",
  "astrbot_plugin_ming_fishtalk",
  "astrbot_plugin_ming_group_roles",
  "astrbot_plugin_ming_live_history",
  "astrbot_plugin_inbox_record",
  "astrbot_plugin_ming_bili",
  "astrbot_plugin_ming_voice",
  "astrbot_plugin_ming_voice_paint",
  "astrbot_plugin_ming_typing",
  "astrbot_plugin_ming_local_rp",
  "astrbot_plugin_ming_roleplay_gate",
  "astrbot_plugin_ming_sendfile",
  "astrbot_plugin_forward_expand",
  "astrbot_plugin_admin_deny_mute",
  "astrbot_plugin_obs_export",
  "astrbot_plugin_dsh_acp",
];

// 不往仓库里带的东西：字节码、备份、试跑稿、TTS 缓存、依赖
const SKIP_DIR = new Set(["__pycache__", "audio", "node_modules", ".pytest_cache", "logs", "data"]);
const SKIP_FILE = [
  /\.pyc$/i,
  /\.pyo$/i,
  /\.bak(\.|-|$)/i,
  /\.before-/i,
  /\.pretest$/i,
  /^\.DS_Store$/,
];

// 脱敏表：[["原串", "占位"], ...]。文件不进仓库。
const redactPath = path.join(__dirname, "redact.local.json");
const REDACT = fs.existsSync(redactPath) ? JSON.parse(fs.readFileSync(redactPath, "utf8")) : [];

const TEXT_EXT = /\.(py|ya?ml|json|md|txt|toml|cfg|ini)$/i;

const skipFile = (name) => SKIP_FILE.some((re) => re.test(name));

let copied = 0, skipped = 0, replaced = 0;
const report = [];

function copyFile(s, d) {
  if (TEXT_EXT.test(s)) {
    let text = fs.readFileSync(s, "utf8");
    for (const [from, to] of REDACT) {
      if (text.includes(from)) {
        replaced += text.split(from).length - 1;
        text = text.split(from).join(to);
      }
    }
    fs.writeFileSync(d, text, "utf8");
  } else {
    fs.copyFileSync(s, d);
  }
  copied++;
}

function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      if (SKIP_DIR.has(entry.name)) { skipped++; continue; }
      copyDir(s, d);
    } else {
      if (skipFile(entry.name)) { skipped++; continue; }
      copyFile(s, d);
    }
  }
}

for (const name of PLUGINS) {
  const src = path.join(srcRoot, name);
  if (!fs.existsSync(src)) { report.push(`  !! 缺失: ${name}`); continue; }
  const dst = path.join(dstRoot, name);
  if (!dry && fs.existsSync(dst)) fs.rmSync(dst, { recursive: true, force: true });
  const before = copied;
  copyDir(src, dst);
  report.push(`  ${name}: ${copied - before} 个文件`);
}

console.log(`src = ${srcRoot}`);
console.log(`dst = ${dstRoot}${dry ? "（dry，未写盘）" : ""}`);
console.log(report.join("\n"));
console.log(`合计 ${copied} 个文件，跳过 ${skipped} 个（缓存/备份/音频），脱敏替换 ${replaced} 处`);
if (!REDACT.length) console.log("!! 没找到 tools/redact.local.json，这次只过滤没脱敏");
