#!/usr/bin/env node
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve, sep } from "node:path";
import { walk } from "./lib/curation.mjs";

const root = resolve(process.argv[2] ?? new URL("..", import.meta.url).pathname);
const files = walk(root);
const byBasename = new Map();
for (const path of files) {
  const name = path.split(sep).at(-1);
  const list = byBasename.get(name) ?? [];
  list.push(path);
  byBasename.set(name, list);
}
let rewritten = 0;
for (const path of files.filter((item) => item.endsWith(".md"))) {
  const text = readFileSync(path, "utf8");
  const next = text.replace(/\[([^\]]*)\]\(([^)]+)\)/g, (whole, label, rawTarget) => {
    const [target, anchor = ""] = rawTarget.split("#", 2);
    if (!target || /^(?:https?:|mailto:|#|\$)/.test(target) || existsSync(resolve(dirname(path), target))) return whole;
    const candidates = byBasename.get(target.split("/").at(-1)) ?? [];
    if (candidates.length === 1) {
      const mapped = relative(dirname(path), candidates[0]).split(sep).join("/");
      rewritten += 1;
      return `[${label}](${mapped}${anchor ? `#${anchor}` : ""})`;
    }
    rewritten += 1;
    return `${label} (external snapshot reference: \`${rawTarget}\`)`;
  });
  if (next !== text) writeFileSync(path, next);
}
console.log(`rewrote ${rewritten} broken links`);
