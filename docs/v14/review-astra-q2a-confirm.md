# Q2a confirmation review, round 2: GPT-6-Astra (xhigh)

Verbatim final reply to the Q2a confirmation brief, reviewing `888b633..03f0156` at the
merge `3dc4b21`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Q2a (running level Q) is GO after this round, with one minor finding (R5) left open.

R1–R4 and the Q-C3 membership item are FIXED within Q2a. One new MINOR finding remains.

- **R1 — FIXED.** Missing screening members and whole groups are rejected at build, prepare, verify, export and successor boundaries. Primary study evidence remained unchanged. A partially consumed pair and a three-root successor chain passed. See `live.py:948`.
- **R2 — FIXED.** Both token stops trigger independently with the opposite field missing or an earlier usage-less completion. Duplicate completions remain deduplicated, and missing accounting remains marked unavailable. See `live_runtime.py:538`.
- **R3 — FIXED.** Crossing the wall deadline during generation after `agent_finish` now produces `per_trial_limit`, `cap_ended=true` and a binding smoke cap. Generation duration excludes cleanup. Cleanup alone crossing the deadline does not make a completed generation cap-ended. See `queue_evidence.py:113`.
- **R4 — FIXED.** Neither, before-only, after-only and both-side compactions classify correctly. Pre-focal compaction still causes ineligibility. See `queue_evidence.py:138`.
- **Q-C3 memberships — FIXED.** Candidates retain their sealed memberships; supported Q2a plans and exports record executed memberships separately. Gate/main execution remains Q2b work. See `live.py:615`.

For item 2, I found no valid-plan rejection or functional regression. Q and P successor exports passed. Earlier fixture bytes, revision-6 study and compatibility evidence, and default-runtime attempts, receipts and exports passed their preservation checks.

**MINOR R5. Prior-start validation repeats ancestor verification exponentially.**

At `live.py:989`, each prior root enters `consumed_attempts_in_root`, which recursively invokes the same check at `live.py:2088`. Each successor already lists every predecessor.

Reproduction: prepare six valid, unstarted smoke roots successively, then check the sixth root's prior starts. Five distinct ancestors caused **31 primary-evidence reads**, taking **8.875 seconds**. The traversal performs `2^n − 1` reads for `n` predecessors.

Minimum fix: share verified ancestor results within one validation operation. Add a test requiring each ancestor to be audited once, while still rejecting changed ancestor evidence on a subsequent operation. This is nonblocking because the demonstrated chain validates correctly.

Validation: **40 functional checks passed**, plus the R5 reproduction. Temporary successor/population checks used immutable in-memory receipt snapshots instead of commits; they retained primary-file hash comparisons but did not retest Git anchoring for those roots. All four retained P receipts matched committed HEAD.

The worktree remains clean at `3dc4b21`; `git diff --check` passed. No model/provider calls, VM access, commits, pushes, merges or reads of the excluded repository occurred. No blocking items remain.

**GO**
