import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

export const SENTINELS = ["not_recorded", "not_measured", "unknown", "invalidated", "inconclusive"];

export function walk(root, options = {}) {
  const results = [];
  const visit = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (options.skip?.has(entry.name)) continue;
      const path = join(dir, entry.name);
      if (entry.isDirectory()) visit(path);
      else if (entry.isFile()) results.push(path);
    }
  };
  if (existsSync(root)) visit(root);
  return results.sort();
}

export function sha256File(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

export function posixRelative(root, path) {
  return relative(root, path).split(sep).join("/");
}

export function discoverRunSources(performanceRoot) {
  const sources = [];
  const visit = (dir, depth) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      if (["node_modules", ".git", "artifacts", "evidence", "reference"].includes(entry.name)) continue;
      const path = join(dir, entry.name);
      if (/^run_/.test(entry.name) || (depth === 0 && /^local_/.test(entry.name))) {
        sources.push({ runId: entry.name, path: posixRelative(performanceRoot, path) });
      }
      visit(path, depth + 1);
    }
  };
  visit(performanceRoot, 0);
  return sources.sort((a, b) => a.path.localeCompare(b.path));
}

export function groupRunSources(sources) {
  const groups = new Map();
  for (const source of sources) {
    const current = groups.get(source.runId) ?? [];
    current.push(source.path);
    groups.set(source.runId, current);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([runId, paths]) => ({ runId, sourcePaths: paths.sort() }));
}

export function classifyPhase(runId, sourcePaths = [], explicitPhase) {
  const explicit = String(explicitPhase ?? "").match(/[0-8]/)?.[0];
  if (explicit) return `phase${explicit}`;
  const text = `${runId} ${sourcePaths.join(" ")}`.toLowerCase();
  const named = text.match(/phase[_-]?([0-8])/);
  if (named) return `phase${named[1]}`;
  if (/(kinesis|capacity_|latency_tcp|fanout_crossover|protocol_crossover|independent_crossover|connection_path|t3_e2e|oha_p95|haproxy_auto)/.test(text)) return "phase1";
  return "phase0";
}

export function classifyCategory(runId) {
  const text = runId.toLowerCase();
  if (/cleanup/.test(text)) return "cleanup";
  if (/deployment_readiness|archive_readiness|preflight|readiness/.test(text)) return "deployment_readiness";
  if (/correctness/.test(text)) return "correctness";
  if (/diagnos|debug|bootstrap_fix|retry/.test(text)) return "diagnostic";
  if (/local_integration|_local$|validation|wiring|admission_control|payload_contract|debug_metrics/.test(text)) return "validation";
  if (/qualification/.test(text)) return "qualification";
  if (/smoke/.test(text)) return "smoke";
  return "experiment";
}

export function normalizeStatus(rawStatus, runId = "") {
  const value = String(rawStatus ?? "").toLowerCase();
  if (/invalid/.test(value) || /invalidated/.test(runId)) return "inconclusive";
  if (/blocked|planned|initialized|authentication.*required/.test(value)) return "blocked";
  if (/abort|interrupt/.test(value) || /interrupted/.test(runId)) return "aborted";
  if (/fail|terminal/.test(value) || /failed/.test(runId)) return "failed";
  if (/^passed$|^succeeded$|completed_qualified/.test(value)) return "passed";
  if (/completed_without_qualified|target.missed/.test(value)) return "inconclusive";
  return "unknown";
}

export function extractTimestamp(runId) {
  const match = runId.match(/(20\d{6})[_-](\d{6})/);
  if (!match) return "not_recorded";
  const d = match[1];
  const t = match[2];
  return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}T${t.slice(0, 2)}:${t.slice(2, 4)}:${t.slice(4, 6)}`;
}

export function readJsonIfPresent(path) {
  if (!existsSync(path)) return undefined;
  try { return JSON.parse(readFileSync(path, "utf8")); } catch { return undefined; }
}

export function firstString(objects, keys) {
  for (const object of objects) {
    if (!object || typeof object !== "object") continue;
    for (const key of keys) {
      const value = object[key];
      if (typeof value === "string" && value.trim() && value.length <= 4000) return value.trim();
    }
  }
  return "not_recorded";
}

const metricKey = /(rps|throughput|latency|requests?|records?|events?|errors?|cpu|memory|duration|count|rate|p50|p90|p95|p99|passed|verdict|status|usd|cost)/i;
export function extractScalars(value, sourcePath, limit = 80) {
  const output = [];
  const visit = (current, path) => {
    if (output.length >= limit || current === null || current === undefined) return;
    if (Array.isArray(current)) {
      current.slice(0, 20).forEach((item, index) => visit(item, `${path}[${index}]`));
      return;
    }
    if (typeof current === "object") {
      for (const [key, child] of Object.entries(current)) visit(child, path ? `${path}.${key}` : key);
      return;
    }
    const key = path.split(/[.[]/).at(-1) ?? path;
    if (!metricKey.test(key)) return;
    if (typeof current === "string" && (current.length > 300 || /https?:\/\//i.test(current))) return;
    output.push({ name: path, value: current, source: sourcePath });
  };
  visit(value, "");
  return output;
}

export function importantEvidenceFiles(sourceRoot, sourcePaths) {
  const allowed = /^(run|summary|metrics-summary|correctness-summary|cleanup-verification|cost[^/]*|report|verdict|hypothesis|infra|commands|artifacts)\.(json|md|txt)$/i;
  const entries = [];
  for (const sourcePath of sourcePaths) {
    const absolute = join(sourceRoot, sourcePath);
    if (!existsSync(absolute)) continue;
    for (const name of readdirSync(absolute)) {
      const path = join(absolute, name);
      if (!statSync(path).isFile() || !allowed.test(name) || statSync(path).size > 5 * 1024 * 1024) continue;
      entries.push({ path: posixRelative(sourceRoot, path), bytes: statSync(path).size, sha256: sha256File(path) });
    }
  }
  return entries.sort((a, b) => a.path.localeCompare(b.path));
}

export function validateSchema(schema, value, path = "$") {
  const errors = [];
  const types = Array.isArray(schema.type) ? schema.type : schema.type ? [schema.type] : [];
  const actual = value === null ? "null" : Array.isArray(value) ? "array" : typeof value === "number" && Number.isInteger(value) ? "integer" : typeof value;
  if (types.length && !types.includes(actual) && !(actual === "integer" && types.includes("number"))) errors.push(`${path}: expected ${types.join("|")}, got ${actual}`);
  if (schema.const !== undefined && value !== schema.const) errors.push(`${path}: expected constant ${JSON.stringify(schema.const)}`);
  if (schema.enum && !schema.enum.includes(value)) errors.push(`${path}: not in enum`);
  if (schema.required && value && typeof value === "object" && !Array.isArray(value)) for (const key of schema.required) if (!(key in value)) errors.push(`${path}.${key}: required`);
  if (schema.properties && value && typeof value === "object" && !Array.isArray(value)) for (const [key, child] of Object.entries(schema.properties)) if (key in value) errors.push(...validateSchema(child, value[key], `${path}.${key}`));
  if (schema.items && Array.isArray(value)) value.forEach((item, index) => errors.push(...validateSchema(schema.items, item, `${path}[${index}]`)));
  if (schema.minItems !== undefined && Array.isArray(value) && value.length < schema.minItems) errors.push(`${path}: fewer than ${schema.minItems} items`);
  return errors;
}

export const SECRET_PATTERNS = [
  /AKIA[0-9A-Z]{16}/,
  /ASIA[0-9A-Z]{16}/,
  /-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----/,
  /github_pat_[A-Za-z0-9_]{20,}/,
  /gh[pousr]_[A-Za-z0-9_]{20,}/,
  /[?&]X-Amz-(?:Credential|Signature|Security-Token)=/i,
  /authorization:\s*(?:bearer|basic)\s+[A-Za-z0-9._~+/-]+/i,
];
