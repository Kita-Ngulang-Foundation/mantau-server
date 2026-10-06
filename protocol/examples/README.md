# Golden envelope examples

These test fixtures were generated with `mantau_core.contracts`. The Python
tests in `tests/test_contract_envelopes.py` and Android tests in
`android-agent/app/src/test/java/id/mantau/agent/uplink/GoldenProtocolTest.kt`
compare emitted envelopes and signatures against them.

Both fixtures use the test-only secret
`golden-example-shared-secret-do-not-use`. If the contract changes, regenerate
the fixtures with the fixed `sent_at` and `occurred_at` timestamp
`2026-09-13T04:12:03.114000Z` so changes remain visible in version control.
