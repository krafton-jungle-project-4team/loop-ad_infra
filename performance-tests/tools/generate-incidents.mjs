#!/usr/bin/env node
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const evidenceRoot = resolve(process.argv[2] ?? new URL("../evidence", import.meta.url).pathname);
const indexPath = join(evidenceRoot, "experiment-index.json");
const index = JSON.parse(readFileSync(indexPath, "utf8"));
const summaries = new Map(index.experiments.map((entry) => [entry.runId, JSON.parse(readFileSync(join(evidenceRoot, entry.summaryPath), "utf8"))]));
const candidates = index.experiments.filter((entry) => ["diagnostic", "deployment_readiness"].includes(entry.category));
const groups = new Map();

function groupKey(entry) {
  if (/phase1_kinesis_generator_diagnosis/.test(entry.runId)) return "phase1-generator-diagnosis";
  if (entry.phase === "phase6") return "phase6-archive-local-fixes";
  const timestamp = entry.runId.match(/20\d{6}_\d{6}/)?.[0]?.replace("_", "-");
  if (entry.phase === "phase7" && timestamp) return `phase7-${timestamp}`;
  return entry.runId.replace(/^run_20\d{6}_\d{6}_/, "").replace(/_retry\d*$/, "");
}

for (const entry of candidates) {
  const key = groupKey(entry);
  const list = groups.get(key) ?? [];
  list.push(entry);
  groups.set(key, list);
}
for (const [key, entries] of groups) if (key.startsWith("phase7-")) {
  const stamp = key.slice("phase7-".length).replace("-", "_");
  for (const entry of index.experiments.filter((item) => item.phase === "phase7" && item.runId.includes(stamp) && /aws_integration/.test(item.runId))) if (!entries.some((item) => item.runId === entry.runId)) entries.push(entry);
}

for (const [key, entries] of [...groups.entries()].sort()) {
  const incidentId = `incident-${key}`.replace(/_/g, "-");
  const affected = entries.map((entry) => entry.runId).sort();
  const dates = affected.map((id) => summaries.get(id).executedAt).filter((value) => value !== "not_recorded").sort();
  const provenance = {
    snapshotId: index.snapshotId,
    workspaceArchiveSha256: index.workspaceArchiveSha256,
    sourceEvidence: affected.flatMap((id) => summaries.get(id).sourceEvidence).filter((entry, position, all) => all.findIndex((candidate) => candidate.path === entry.path) === position),
  };
  const summary = {
    schemaVersion: 1,
    incidentId,
    symptom: "not_recorded",
    rootCause: "not_recorded",
    fix: "not_recorded",
    regressionResult: entries.map((entry) => `${entry.runId}:${entry.status}`).join(", "),
    affectedRunIds: affected,
    period: { start: dates[0] ?? "not_recorded", end: dates.at(-1) ?? "not_recorded", timezone: "not_recorded" },
    invalidationScope: "unknown",
    provenance,
  };
  const dir = join(evidenceRoot, "incidents", incidentId);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  writeFileSync(join(dir, "report.md"), `# ${incidentId}\n\nThis incident groups run records by an explicit campaign family or identical run timestamp. It does not infer a missing root cause or fix.\n\n## Recorded state\n\n- Symptom: not_recorded\n- Root cause: not_recorded\n- Fix: not_recorded\n- Invalidation scope: unknown\n- Regression results: ${summary.regressionResult}\n\n## Affected runs\n\n${affected.map((id) => `- \`${id}\``).join("\n")}\n\n## Provenance\n\n- Snapshot: \`${index.snapshotId}\`\n- Workspace archive SHA-256: \`${index.workspaceArchiveSha256}\`\n- Hashed evidence files: ${provenance.sourceEvidence.length}\n`);
  for (const entry of entries) {
    entry.incidentIds = [...new Set([...entry.incidentIds, incidentId])].sort();
    const summaryPath = join(evidenceRoot, entry.summaryPath);
    const runSummary = summaries.get(entry.runId);
    runSummary.relatedIncidents = entry.incidentIds;
    writeFileSync(summaryPath, `${JSON.stringify(runSummary, null, 2)}\n`);
  }
}
index.incidentCount = groups.size;
writeFileSync(indexPath, `${JSON.stringify(index, null, 2)}\n`);
console.log(`generated ${groups.size} incidents`);
