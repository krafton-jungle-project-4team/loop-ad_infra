# Local verification failures

All timestamps are UTC. These failures occurred before the final passing local artifacts and did
not cause any AWS API call.

1. LocalStack `2026.06.1` exited with code 55 because Kinesis required a cloud license. It was
   replaced with the pinned pre-sunset community image `3.8.1`; no auth token was introduced.
2. The first 50,000-row attempt stopped after 141.529116 seconds when the qualified producer did
   not receive clean local acceptance. The then-current diagnostic recorded 71 loopback SDK
   attempts and zero real AWS attempts. The local client read timeout was raised from 10 to 60
   seconds; producer code and retry policy were unchanged.
3. The second 50,000-row attempt stopped after 179.477088 seconds with `successful=0`,
   `final_failed=500`, `retry_records=0`, `partial_failures=0`, and ClientError. LocalStack logs
   showed its bundled Kinesis V8 process reaching the default 2 GiB heap and returning HTTP 500.
   The container-only `NODE_OPTIONS=--max-old-space-size=4096` setting was added and the emulator
   was recreated from empty state. The third attempt passed and is recorded in `async-flush.json`.

The correctness fixture initially placed both duplicate deliveries in one async-insert call, which
coalesced them into one part. The final deterministic test delivers the same event in two completed
handler invocations while merges are stopped, matching ESM redelivery: physical rows 2 and `FINAL`
rows 1.
