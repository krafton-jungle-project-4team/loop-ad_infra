#!/usr/bin/env node
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { SECRET_PATTERNS, walk } from "./lib/curation.mjs";

const root = resolve(process.argv[2] ?? new URL("..", import.meta.url).pathname);
const errors = [];
const warnings = [];
const forbiddenDirs = /\/(?:node_modules|target|cdk\.out|cache)\//;
const forbiddenRaw = /\.(?:tgz|tar|gz|pcap|ndjson)$/i;
for (const path of walk(root)) {
  const size = statSync(path).size;
  const relative = path.slice(root.length + 1);
  if (size >= 100 * 1024 * 1024) errors.push(`file >=100MiB: ${relative}`);
  if (forbiddenDirs.test(path) || forbiddenRaw.test(path)) errors.push(`forbidden generated/raw artifact: ${relative}`);
  if (relative.startsWith("evidence/") && size > 5 * 1024 * 1024) errors.push(`evidence file >5MiB: ${relative}`);
  else if (relative.startsWith("evidence/") && size > 1024 * 1024) warnings.push(`evidence file >1MiB: ${relative}`);
  if (size <= 5 * 1024 * 1024 && !/\.(?:png|jpg|jpeg|gif|zip|jar)$/i.test(path)) {
    const text = readFileSync(path, "utf8");
    for (const pattern of SECRET_PATTERNS) if (pattern.test(text)) errors.push(`secret pattern ${pattern} in ${relative}`);
    if (path.endsWith(".md")) for (const match of text.matchAll(/\[[^\]]*\]\(([^)]+)\)/g)) {
      const target = match[1].split("#")[0];
      if (!target || /^(?:https?:|mailto:|#|\$)/.test(target)) continue;
      if (!existsSync(resolve(dirname(path), target))) errors.push(`broken internal link in ${relative}: ${match[1]}`);
    }
  }
}
if (warnings.length) console.warn(warnings.join("\n"));
if (errors.length) { console.error(errors.join("\n")); process.exit(1); }
console.log(`checked ${walk(root).length} curated files`);
