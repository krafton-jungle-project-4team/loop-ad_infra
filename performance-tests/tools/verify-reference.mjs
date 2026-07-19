#!/usr/bin/env node
import { createHash } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { posixRelative, walk } from "./lib/curation.mjs";

const manifestPath = resolve(process.argv[2] ?? new URL("../reference/infra/manifest.json", import.meta.url).pathname);
const infraRoot = dirname(manifestPath);
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
const errors = [];
const storedPaths = new Set();
const restorePaths = new Set();
for (const entry of manifest.files) {
  const path = join(infraRoot, entry.storedPath);
  if (storedPaths.has(entry.storedPath)) errors.push(`duplicate storedPath: ${entry.storedPath}`);
  if (restorePaths.has(entry.restorePath)) errors.push(`duplicate restorePath: ${entry.restorePath}`);
  storedPaths.add(entry.storedPath);
  restorePaths.add(entry.restorePath);
  if (!existsSync(path)) { errors.push(`missing: ${entry.storedPath}`); continue; }
  const bytes = readFileSync(path);
  const hash = createHash("sha256").update(bytes).digest("hex");
  if (hash !== entry.sha256) errors.push(`SHA-256 mismatch: ${entry.storedPath}`);
  if (statSync(path).size !== entry.bytes) errors.push(`size mismatch: ${entry.storedPath}`);
}
const treeRoot = existsSync(join(infraRoot, "source-tree")) ? join(infraRoot, "source-tree") : infraRoot;
const prefix = treeRoot === infraRoot ? "" : "source-tree/";
const actual = walk(treeRoot).filter((path) => path !== manifestPath).map((path) => `${prefix}${posixRelative(treeRoot, path)}`);
for (const path of actual) if (!storedPaths.has(path)) errors.push(`unlisted source-tree file: ${path}`);
if (actual.length !== manifest.fileCount || manifest.files.length !== manifest.fileCount) errors.push("manifest fileCount mismatch");
if (restorePaths.has("package.json")) for (const required of ["package.json", "package-lock.json", "tsconfig.json", "cdk.json", "src/perf-phase0-stack.ts", "src/perf-phase1-kinesis-stack.ts", "src/perf-phase4-clickhouse-stack.ts", "src/perf-phase6-archive-stack.ts", "src/perf-phase7-integration-stack.ts"]) if (!restorePaths.has(required)) errors.push(`RESTORE input missing: ${required}`);
if (errors.length) { console.error(errors.join("\n")); process.exit(1); }
console.log(`verified ${manifest.fileCount} reference files and restore mappings`);
