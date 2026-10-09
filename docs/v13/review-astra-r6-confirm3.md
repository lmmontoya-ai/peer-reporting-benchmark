# Revision 6 confirmation code review, round 3: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm3 on `9b58df7` (the merge of R6-fix3), the diff
`d01c52d..9b58df7`. Only the link targets were changed, from local worktree paths to
repository-relative paths. Read-only review; no model calls.

N1 (core path): **NOT FIXED in full.** Splits across registered roots now refuse before packet creation, including excluded/unrun assignments. However, partitioned exports citing one registered root still produce four verified packets versus one combined packet.

Execution boundary: **NOT FIXED in full.** Import isolation passes, but core sealing accepts a decision that fails full offline verification. The core CLI also crashes.

1. **MAJOR — Core preparation no longer verifies the final decision.** [pilot.py:33](../../src/swarm_auth_bench/peer_reporting_v11/pilot.py), called by [live.py:1702](../../src/swarm_auth_bench/peer_reporting_v11/live.py).

   Reproduction: obtain a valid core build tuple. Separately create a decision from failed-gate exports: **0/36 hits, `stop`, no eligible lanes**. Change it to `proceed` with six eligible lanes, reseal it, and replace the tuple’s decision and decision hash. The full offline validator rejects this record. Nevertheless, `prepare_live_root` seals **600 calls**, and `verify_live_root` accepts all six lanes. No validator was replaced or bypassed with a callback.

   Minimum fix: require full offline verification of the final embedded decision at the core prepare/seal boundary, through an injected verifier. Keep runtime checks independent of post-hoc code. Test this replacement between build and prepare; require refusal before writing the root.

2. **MAJOR — Remaining N1: single-root export partitions bypass population completeness.** [review_population.py:80](../../src/swarm_auth_bench/peer_reporting_v11/review_population.py).

   The check establishes only that exported assignments belong to the root. It does not require complete membership for an arm. I registered the four frozen core assignments together, then created four resealed singleton exports citing that same root. Each passed `write_review_packets` and produced one sampled packet. The combined population produces one.

   Minimum fix: require every represented arm’s complete planned assignment population, including excluded and unscored rows, before accepting its selection. Test combined versus partitioned exports from the **same** root and reject incomplete partitions before writing packets.

3. **MAJOR — The core CLI fails before decision verification.** [cli.py:366](../../src/swarm_auth_bench/peer_reporting_v11/cli.py).

   `build --phase calibration --arm pressure_core_xhigh --pilot-decision ...` raises `UnboundLocalError: cannot access local variable 'read_sealed'`. Its import occurs only in the collection branch at line 348. This predates fix3 but remains a readiness blocker.

   Minimum fix: import `read_sealed` in the common build scope. Add CLI tests for a successful core build and controlled refusals of invalid decisions.

`pilot_decision.py` **remains in the sealed implementation hashes**. `POST_HOC_MODULES` exempts changes only from execution blocking; verification still reports them. After sealing, the embedded decision and its hash remain bound by the registered, authorized plan hash. That exemption is not itself a finding; finding 1 concerns the decision accepted **before** sealing.

All **257 targeted repository tests passed**, including the unchanged import-boundary test, runtime isolation, 279 earlier-level fixture comparisons, and regressions for N2–N4 and original finding 6. The independent reproductions above still succeeded. The worktree is clean at `9b58df7`; no provider calls or guest operations occurred.

**NO-GO — final-decision verification bypass, incomplete core sampling population, and broken core CLI.**
