# Revision 6 confirmation code review, round 2: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm2 on `204b67e` (the merge of R6-fix2), the diff
`c3923bc..204b67e`. Only the link targets were changed, from local worktree paths to
repository-relative paths. Read-only review; no model calls.

Reviewed `204b67e`.

- **N1 — NOT FIXED in full.** The pilot mismatch is fixed: split-arm inputs, including excluded scored rows, are refused; separate arms preserve combined counts; sealed selections are checked. Refusal is acceptable for pilot decisions, but it does not enforce §7.4 on core packets. See the remaining major finding below.
- **N2 — FIXED.** All 16 rehashed error/null receipt mutations are rejected, including idempotent repeats. Legitimate failures and pending requests pass. Sixteen additional receipt edge cases also passed. [world.py:329](../../src/swarm_auth_bench/peer_reporting_v11/world.py)
- **N3 — FIXED.** Finished P packets match frozen delivered instructions, including budgets and private/visible/P4 routing; invented instructions are rejected. The unchanged earlier-level budget omission does not carry into P/v1.3 packets or alter their results. [review.py:249](../../src/swarm_auth_bench/peer_reporting_v11/review.py)
- **N4 — FIXED.** Finished HTML includes all three questions, yes/no requirements and the JSON response template. A completed template passes upload validation. [review.py:756](../../src/swarm_auth_bench/peer_reporting_v11/review.py)
- **Original 1 — FIXED.** Pilot-decision enforcement, sample binding, stop/ceiling refusals and run binding pass. The 34/36 gate with one 4/6 lane produces five lanes and 500 core calls. [test_pilot_decision.py:102](../../tests/v11/test_pilot_decision.py)
- **Original 6 — FIXED.** Private-shape, receipt-content/order and non-`stored` mutations now fail reconciliation against storage, events and idempotency records.

**MAJOR — Remaining N1: core review sampling still depends on export partitioning.** [live_review.py:458](../../src/swarm_auth_bench/peer_reporting_v11/live_review.py) samples the current export; [review.py:803](../../src/swarm_auth_bench/peer_reporting_v11/review.py) verifies against that same local population. The cross-export refusal exists only in the pilot-decision path.

Reproduction: using four real frozen `pressure_core_xhigh`, visible P3, K=1 violation assignments, I supplied eligible offline misses with “These peers are not real participants.” None was mechanically flagged. Combined, they produced **one** sampled review packet. Split into four sealed exports, they produced **four** sampled packets. Every packet export passed verification. Core review never invokes the pilot-input refusal.

Minimum fix: bind core packet selection to the complete arm/phase population, or enforce the chosen split-arm refusal in the export/packet workflow using the phase’s roots. Required test: repeat this core split-versus-combined case; obtain identical sampled IDs or refuse the split before writing packets. Include excluded scored rows.

No additional regressions found. All 279 earlier-level fixture hashes passed; complete L0–L4 and S bundles and HTML matched baseline bytes with random IDs held constant. Frozen tool hashes and scoring checks passed.

946 targeted tests completed without failure. The optional 756-trial integration chain was stopped before completion; the coordinator is covering the full suite. Tracked files remain unchanged. No provider calls or guest operations.

**NO-GO — N1 remains open on the core review path.**
