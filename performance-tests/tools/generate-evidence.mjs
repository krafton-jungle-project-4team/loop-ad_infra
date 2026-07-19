#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import {
  SECRET_PATTERNS,
  classifyCategory,
  classifyPhase,
  discoverRunSources,
  extractScalars,
  extractTimestamp,
  firstString,
  groupRunSources,
  importantEvidenceFiles,
  normalizeStatus,
  readJsonIfPresent,
} from "./lib/curation.mjs";

function args(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 2) parsed[argv[index].replace(/^--/, "")] = argv[index + 1];
  return parsed;
}

const options = args(process.argv.slice(2));
for (const required of ["source", "evidence", "snapshot-id", "archive-sha", "source-head"]) if (!options[required]) throw new Error(`missing --${required}`);
const sourceRoot = resolve(options.source);
const evidenceRoot = resolve(options.evidence);
if (existsSync(join(evidenceRoot, "experiment-index.json")) && options.refresh !== "true") throw new Error("experiment-index.json already exists; pass --refresh true only when regenerating from the same immutable snapshot");

const safeText = (value) => {
  if (typeof value !== "string" || !value.trim()) return "not_recorded";
  if (/https?:\/\//i.test(value) || SECRET_PATTERNS.some((pattern) => pattern.test(value))) return "redacted_sensitive_value";
  return value.trim().slice(0, 4000);
};

const directJsonObjects = (sourcePaths, names) => {
  const output = [];
  for (const sourcePath of sourcePaths) for (const name of names) {
    const path = join(sourceRoot, sourcePath, name);
    const value = readJsonIfPresent(path);
    if (value !== undefined) output.push({ value, path: `${sourcePath}/${name}` });
  }
  return output;
};

const statusFrom = (record, fallback = "not_recorded") => {
  if (!record || typeof record !== "object") return fallback;
  if (typeof record.passed === "boolean") return record.passed ? "passed" : "failed";
  return safeText(record.status ?? record.verdict ?? fallback);
};

mkdirSync(join(evidenceRoot, "experiments"), { recursive: true });
mkdirSync(join(evidenceRoot, "snapshots"), { recursive: true });
for (let phase = 0; phase <= 8; phase += 1) mkdirSync(join(evidenceRoot, "experiments", `phase${phase}`), { recursive: true });

const sources = discoverRunSources(sourceRoot);
const groups = groupRunSources(sources);
const indexEntries = [];

for (const group of groups) {
  const runRecords = directJsonObjects(group.sourcePaths, ["run.json", "summary.json"]);
  const records = runRecords.map((entry) => entry.value);
  const explicitPhase = firstString(records, ["phase", "phaseName"]);
  const phase = classifyPhase(group.runId, group.sourcePaths, explicitPhase === "not_recorded" ? undefined : explicitPhase);
  const category = classifyCategory(group.runId);
  const rawStatus = safeText(firstString(records, ["status", "state", "verdict"]));
  const status = normalizeStatus(rawStatus, group.runId);
  const explicitValidity = safeText(firstString(records, ["validity", "validityStatus"]));
  const validity = /invalidated/i.test(group.runId) || /invalid/i.test(explicitValidity) ? "invalidated" : ["valid", "inconclusive"].includes(explicitValidity) ? explicitValidity : "unknown";
  const metricsRecords = [...runRecords, ...directJsonObjects(group.sourcePaths, ["metrics-summary.json"])];
  const correctnessRecords = directJsonObjects(group.sourcePaths, ["correctness-summary.json"]);
  const cleanupRecords = directJsonObjects(group.sourcePaths, ["cleanup-verification.json"]);
  const costRecords = [];
  for (const sourcePath of group.sourcePaths) {
    const dir = join(sourceRoot, sourcePath);
    if (!existsSync(dir)) continue;
    for (const name of readdirSync(dir).filter((name) => /^cost.*\.json$/i.test(name))) {
      const value = readJsonIfPresent(join(dir, name));
      if (value !== undefined) costRecords.push({ value, path: `${sourcePath}/${name}` });
    }
  }
  const keyMetrics = metricsRecords.flatMap((entry) => extractScalars(entry.value, entry.path)).slice(0, 120).map((entry) => ({ ...entry, value: typeof entry.value === "string" ? safeText(entry.value) : entry.value }));
  const majorSettings = runRecords.flatMap((entry) => extractScalars(entry.value, entry.path, 20)).filter((entry) => /(target|duration|candidate|collector|shard|concurr|repetition|count)/i.test(entry.name)).slice(0, 40);
  const correctnessStatus = correctnessRecords.length ? statusFrom(correctnessRecords[0].value, "unknown") : "not_recorded";
  const cleanupStatus = cleanupRecords.length ? statusFrom(cleanupRecords[0].value, "unknown") : safeText(firstString(records, ["cleanupStatus", "cleanup_status"]));
  const costStatus = costRecords.length ? statusFrom(costRecords.at(-1).value, "unknown") : "not_recorded";
  const sourceRevision = safeText(firstString(records, ["sourceRevision", "source_revision", "sourceSha", "gitSha", "commit"]));
  const purpose = safeText(firstString(records, ["purpose", "objective", "goal"]));
  const hypothesis = safeText(firstString(records, ["hypothesis"]));
  const topology = safeText(firstString(records, ["topology", "executionMode", "execution_mode"]));
  const conclusion = safeText(firstString(records, ["conclusion", "decision", "verdict"]));
  const limitations = records.flatMap((record) => Array.isArray(record?.limitations) ? record.limitations.filter((item) => typeof item === "string").map(safeText) : []).slice(0, 20);
  if (!limitations.length) limitations.push("not_recorded");
  const summary = {
    schemaVersion: 1,
    runId: group.runId,
    phase,
    category,
    executedAt: extractTimestamp(group.runId),
    purpose,
    hypothesis,
    topology,
    majorSettings,
    sourceRevision,
    status,
    rawStatus,
    validity,
    keyMetrics,
    correctnessStatus,
    costStatus,
    cleanupStatus: cleanupStatus === "not_recorded" ? "unknown" : cleanupStatus,
    conclusion,
    limitations,
    relatedIncidents: [],
    provenance: { snapshotId: options["snapshot-id"], workspaceArchiveSha256: options["archive-sha"], sourceHeadAtSnapshot: options["source-head"], sourcePaths: group.sourcePaths },
    sourceEvidence: importantEvidenceFiles(sourceRoot, group.sourcePaths),
  };
  const relativeDir = `experiments/${phase}/${group.runId}`;
  const outputDir = join(evidenceRoot, relativeDir);
  mkdirSync(outputDir, { recursive: true });
  writeFileSync(join(outputDir, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  const report = `# ${group.runId}\n\n` +
    `This is a curated evidence report. Raw artifacts remain in \`${options["snapshot-id"]}\`.\n\n` +
    `## Classification\n\n- Phase: \`${phase}\`\n- Category: \`${category}\`\n- Status: \`${status}\` (source: \`${rawStatus}\`)\n- Validity: \`${validity}\`\n- Executed at: \`${summary.executedAt}\` (timezone not recorded)\n\n` +
    `## Purpose and hypothesis\n\n- Purpose: ${purpose}\n- Hypothesis: ${hypothesis}\n\n` +
    `## Results\n\n- Correctness: \`${correctnessStatus}\`\n- Cost: \`${costStatus}\`\n- Cleanup: \`${summary.cleanupStatus}\`\n- Extracted metric fields: ${keyMetrics.length}\n\n` +
    `## Conclusion\n\n${conclusion}\n\n## Limitations\n\n${limitations.map((item) => `- ${item}`).join("\n")}\n\n` +
    `## Provenance\n\n- Snapshot: \`${options["snapshot-id"]}\`\n- Workspace archive SHA-256: \`${options["archive-sha"]}\`\n- Source paths:\n${group.sourcePaths.map((path) => `  - \`${path}\``).join("\n")}\n- Hashed evidence files: ${summary.sourceEvidence.length}\n`;
  writeFileSync(join(outputDir, "report.md"), report);
  indexEntries.push({ runId: group.runId, phase, category, status, validity, summaryPath: `${relativeDir}/summary.json`, reportPath: `${relativeDir}/report.md`, sourcePaths: group.sourcePaths, incidentIds: [] });
}

const countBy = (key) => Object.fromEntries([...new Set(indexEntries.map((entry) => entry[key]))].sort().map((value) => [value, indexEntries.filter((entry) => entry[key] === value).length]));
const inventory = {
  schemaVersion: 1,
  snapshotId: options["snapshot-id"],
  workspaceArchiveSha256: options["archive-sha"],
  sourceRootContract: "paths are relative to the captured performance-tests directory",
  sourcePathCount: sources.length,
  uniqueRunIdCount: groups.length,
  duplicateRunIds: groups.filter((group) => group.sourcePaths.length > 1).map((group) => ({ runId: group.runId, sourcePaths: group.sourcePaths })),
  sources,
};
const index = { schemaVersion: 1, snapshotId: options["snapshot-id"], workspaceArchiveSha256: options["archive-sha"], experimentCount: indexEntries.length, counts: { phase: countBy("phase"), category: countBy("category"), status: countBy("status") }, experiments: indexEntries };
writeFileSync(join(evidenceRoot, "snapshots", "source-run-inventory.json"), `${JSON.stringify(inventory, null, 2)}\n`);
writeFileSync(join(evidenceRoot, "experiment-index.json"), `${JSON.stringify(index, null, 2)}\n`);
console.log(`generated ${groups.length} experiment records from ${sources.length} source paths`);
