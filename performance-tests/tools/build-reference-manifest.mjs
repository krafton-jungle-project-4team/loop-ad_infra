#!/usr/bin/env node
import { createHash } from "node:crypto";
import { readFileSync, statSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { posixRelative, walk } from "./lib/curation.mjs";

const [treeArg, outputArg, sourceMetadataArg, mode] = process.argv.slice(2);
if (!treeArg || !outputArg || !sourceMetadataArg) throw new Error("usage: build-reference-manifest <source-tree> <output> <source-metadata.json>");
const tree = resolve(treeArg);
const source = JSON.parse(readFileSync(sourceMetadataArg, "utf8"));
const storedPrefix = mode === "direct" ? "" : "source-tree/";
const outputPath = resolve(outputArg);
const files = walk(tree).filter((path) => path !== outputPath).map((path) => {
  const stored = posixRelative(tree, path);
  const original = stored.endsWith(".reference") ? stored.slice(0, -10) : stored;
  const bytes = readFileSync(path);
  return { originalPath: original, storedPath: `${storedPrefix}${stored}`, restorePath: original, storageTransform: stored === original ? "none" : "remove .reference suffix", bytes: statSync(path).size, sha256: createHash("sha256").update(bytes).digest("hex") };
});
writeFileSync(outputArg, `${JSON.stringify({ schemaVersion: 1, purpose: "restorable performance-infrastructure reference; not an active build target", source, fileCount: files.length, files }, null, 2)}\n`);
