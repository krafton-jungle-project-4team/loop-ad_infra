#!/usr/bin/env node
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { walk, validateSchema } from "./lib/curation.mjs";

const evidenceRoot = resolve(process.argv[2] ?? join(dirname(new URL(import.meta.url).pathname), "..", "evidence"));
const experimentSchema = JSON.parse(readFileSync(join(evidenceRoot, "schema", "experiment.schema.json"), "utf8"));
const incidentSchema = JSON.parse(readFileSync(join(evidenceRoot, "schema", "incident.schema.json"), "utf8"));
const index = JSON.parse(readFileSync(join(evidenceRoot, "experiment-index.json"), "utf8"));
const inventory = JSON.parse(readFileSync(join(evidenceRoot, "snapshots", "source-run-inventory.json"), "utf8"));
const errors = [];
const ids = new Set();
const mappedPaths = new Map();

for (const entry of index.experiments) {
  if (ids.has(entry.runId)) errors.push(`duplicate run ID: ${entry.runId}`);
  ids.add(entry.runId);
  for (const path of entry.sourcePaths) mappedPaths.set(path, (mappedPaths.get(path) ?? 0) + 1);
  for (const path of [entry.summaryPath, entry.reportPath]) if (!existsSync(join(evidenceRoot, path))) errors.push(`missing indexed file: ${path}`);
  const summaryPath = join(evidenceRoot, entry.summaryPath);
  if (existsSync(summaryPath)) errors.push(...validateSchema(experimentSchema, JSON.parse(readFileSync(summaryPath, "utf8"))).map((error) => `${entry.runId}: ${error}`));
}

for (const source of inventory.sources) if (mappedPaths.get(source.path) !== 1) errors.push(`source path mapped ${mappedPaths.get(source.path) ?? 0} times: ${source.path}`);
for (const [path, count] of mappedPaths) if (count !== 1 || !inventory.sources.some((source) => source.path === path)) errors.push(`unexpected or duplicate mapped path: ${path}`);
const indexedFiles = new Set(index.experiments.flatMap((entry) => [entry.summaryPath, entry.reportPath]));
for (const path of walk(join(evidenceRoot, "experiments"))) {
  if (path.endsWith("/.gitkeep")) continue;
  const relative = path.slice(evidenceRoot.length + 1).split("\\").join("/");
  if (!indexedFiles.has(relative)) errors.push(`orphan experiment file: ${relative}`);
}

for (const path of walk(join(evidenceRoot, "incidents")).filter((path) => path.endsWith("summary.json"))) errors.push(...validateSchema(incidentSchema, JSON.parse(readFileSync(path, "utf8"))).map((error) => `${path}: ${error}`));
for (const path of walk(evidenceRoot).filter((path) => path.endsWith(".json"))) try { JSON.parse(readFileSync(path, "utf8")); } catch (error) { errors.push(`invalid JSON ${path}: ${error.message}`); }

if (index.experiments.length !== inventory.uniqueRunIdCount) errors.push(`index count ${index.experiments.length} != inventory unique count ${inventory.uniqueRunIdCount}`);
if (errors.length) { console.error(errors.join("\n")); process.exit(1); }
console.log(`validated ${index.experiments.length} experiments and ${inventory.sources.length} source paths`);
