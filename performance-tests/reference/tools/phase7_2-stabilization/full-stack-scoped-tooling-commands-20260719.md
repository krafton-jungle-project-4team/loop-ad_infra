# Full-stack scoped archive focused verification

Executed after implementation commits `ab1b62fe` and `e1ae6a3a`, before any new AWS resource or
paid boundary:

1. Python compile passed for the changed Phase 7 AWS tooling.
2. Focused Python suites passed 60/60: scoped archive, archive runtime, AWS tooling and legacy
   runner policy regression.
3. `npx jest --runInBand test/perf-phase7-integration.test.ts` passed 16/16.
4. `npm run build` passed.
5. Exact-context, `--no-lookups` synth of `LoopAdPerfPhase7IntegrationStack` passed with 150
   resources, five Launch Templates, five task definitions and a 120-shard Kinesis stream. The
   validation-only Run/Session identity made zero AWS requests and is not reusable for an attempt.
6. `validate_template.py` passed for all five task definitions.
7. Decoded Launch Template user-data upper bounds were 7,805, 9,524, 15,316, 8,527 and 7,779
   bytes. Every value is at or below 16,384; load generator is at or below 15,360.
8. Pinned cfn-lint 1.53.0 returned raw exit 6 with only the accepted CDK assembly findings:
   E1022 x8 and W3005 x3. Unexpected findings were zero.
9. The deterministic fixture calculated active prior `$0.950000`, scoped charge `$18.950729`,
   operational maximum `$19.900729`, cleanup reserve `$5.000000`, and total maximum
   `$24.900729 <= $60.000000`. Future strict and paid Phase 8 reservations are both `$0.000000`.
10. Read-only re-audit closed the strict CLI fail-open and composite anchor findings: P0 0, P1 0.

This verification does not authorize AWS work by itself. Fresh exact identity, global inventory
zero, public prices, quota/offering/bootstrap, source seal, images and absent/prepared preflights
remain mandatory.
