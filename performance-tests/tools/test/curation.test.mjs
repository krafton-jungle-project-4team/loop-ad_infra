import assert from "node:assert/strict";
import test from "node:test";
import { classifyCategory, classifyPhase, groupRunSources, normalizeStatus, SECRET_PATTERNS, validateSchema } from "../lib/curation.mjs";

test("groups duplicate source copies under one run ID", () => {
  assert.deepEqual(groupRunSources([{ runId: "run_a", path: "run_a" }, { runId: "run_a", path: "copy/run_a" }]), [{ runId: "run_a", sourcePaths: ["copy/run_a", "run_a"] }]);
});

test("classifies explicit phases and non-experiment categories", () => {
  assert.equal(classifyPhase("run_20260717_phase7_2_aws_integration"), "phase7");
  assert.equal(classifyPhase("run_20260714_connection_path"), "phase1");
  assert.equal(classifyCategory("run_x_deployment_readiness"), "deployment_readiness");
  assert.equal(classifyCategory("run_x_final_cleanup"), "cleanup");
});

test("does not turn generic completion into a performance pass", () => {
  assert.equal(normalizeStatus("completed"), "unknown");
  assert.equal(normalizeStatus("passed"), "passed");
  assert.equal(normalizeStatus("failed-preflight"), "failed");
});

test("validates required and enum fields", () => {
  const schema = { type: "object", required: ["status"], properties: { status: { type: "string", enum: ["passed"] } } };
  assert.equal(validateSchema(schema, {}).length, 1);
  assert.equal(validateSchema(schema, { status: "failed" }).length, 1);
  assert.equal(validateSchema(schema, { status: "passed" }).length, 0);
});

test("detects representative secret forms", () => {
  const accessKey = ["AKIA", "ABCDEFGHIJKLMNOP"].join("");
  const signedUrl = `https://x?${["X-Amz", "Signature"].join("-")}=abc`;
  assert.ok(SECRET_PATTERNS.some((pattern) => pattern.test(accessKey)));
  assert.ok(SECRET_PATTERNS.some((pattern) => pattern.test(signedUrl)));
});
