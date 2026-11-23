#!/usr/bin/env node
// 按 plugins/*/metadata.yaml 生成 README 的插件表。改完插件重跑一次即可。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const pluginsDir = path.join(root, "plugins");

const field = (text, key) => {
  const m = new RegExp("^" + key + ":\\s*(.*)$", "m").exec(text);
  if (!m) return "";
  return m[1].replace(/^["']|["']$/g, "").replace(/\s+#.*$/, "").trim();
};

const rows = [];
for (const name of fs.readdirSync(pluginsDir).sort()) {
  const meta = path.join(pluginsDir, name, "metadata.yaml");
  if (!fs.existsSync(meta)) continue;
  const text = fs.readFileSync(meta, "utf8");
  const title = field(text, "display_name") || field(text, "name") || name;
  const raw = (field(text, "short_desc") || field(text, "desc") || "").replace(/\s+/g, " ").trim();
  const desc = raw.length > 64 ? raw.slice(0, 64).replace(/[，,、；;\s]+$/, "") + "…" : raw;
  const ver = field(text, "version");
  rows.push(`| \`${name}\` | ${title} | ${desc} | ${ver} |`);
}

const table = [
  "| 目录 | 名称 | 作用 | 版本 |",
  "| --- | --- | --- | --- |",
  ...rows,
].join("\n");

const readme = path.join(root, "README.md");
const marker = { start: "<!-- PLUGIN_TABLE:START -->", end: "<!-- PLUGIN_TABLE:END -->" };
let body = fs.readFileSync(readme, "utf8");
if (body.includes(marker.start)) {
  body = body.replace(
    new RegExp(marker.start + "[\\s\\S]*?" + marker.end),
    marker.start + "\n" + table + "\n" + marker.end,
  );
  fs.writeFileSync(readme, body, "utf8");
  console.log(`已更新 README 表格：${rows.length} 个插件`);
} else {
  console.log(table);
}
