# Q2a code review: GPT-6-Astra (xhigh)

Verbatim final reply to the Q2a review brief, reviewing `606235f..0e20483` at the merge
`b253dc5`. Read-only review; no model calls. Links to local files are reduced to their
file names.

Three MAJOR findings remain open.

Answers to review items 1–7:

1. Earlier-level byte guards, revision-6 study/compatibility verification and both execution-boundary tests passed. The four retained level P receipts match committed HEAD content. I did not re-verify production P roots/exports or run tests that create Git commits.
2. Candidate arms, cells, blocks, splits, seeds, hash ordering and shared assignments match the brief. Root validation does not enforce complete groups or the full required population; see R1.
3. Instructions, owner messages, tool set v3, world binding and pinned runtime options are connected correctly. Targeted option-mismatch tests passed.
4. Provisional caps and the 15,500,000-token reservation are correct. Complete usage is deduplicated and summed correctly. Missing usage can disable stops, and wall stops after `agent_finish` are misreported; see R2–R3.
5. Q exports require committed receipts. Derived-record checks, reasoning evidence, mixed-level refusal, successor handling and abandonment are connected to the existing evidence machinery. R1 remains a derived-plan consistency defect. The full receipt-writing chain test was excluded under the no-commit rule.
6. The budget command uses the pinned client’s serialization path with an isolated loopback fake provider. Fake-client preservation and alteration tests passed; the guest check was not run. All five smoke checks have passing/failing tests, but smoke cap reporting misses R3.
7. Exports retain world events/state, tool requests/results, outputs, closure texts, reasoning, usage, compactions and eligibility inputs. R3–R4 make some exported classifications incorrect.

Judgments on Sol’s interpretations:

1. **Accept:** separately sealing Q preserves archived earlier studies.
2. **Accept:** `[template_id, block]` and the full model ID plus effort identify the intended world and lane.
3. **Accept:** four-lane attention candidates match the package brief; Q2b must select luna-low for execution.
4. **Accept:** one assignment with both memberships prevents duplicate standard episodes.
5. **Accept:** an active-generation token stop remains cap-ended after `agent_finish`. The same principle needs to cover wall stops.

Findings, most severe first:

1. **MAJOR R1 — Incomplete screening groups pass plan validation.**  
   `queue_study.py:34`, `live.py:926`.

   Reproduction: generate the canonical study, remove the stock-count/block-1/sol-low P3 screening row from the proposed plan, and retain the unchanged study manifest. `verify_order(order_rows(...))` accepts it. `build_assignment_plan`, `prepare_live_root` and `verify_live_root` also accept the resulting **39-episode** screening root.

   The order verifier checks only supplied rows; assignment binding checks membership without requiring every expected assignment. This violates sections 7.1–7.2 and the derived-record rule in section 2.1.

   Minimum fix: derive the required phase population from the sealed study and verified prior starts, compare the plan against that population, and validate complete groups and order at build and consumption boundaries.

   Required test: omit one member of a screening pair and separately omit an entire group. Both must refuse with unchanged primary study evidence. Include a valid successor case.

2. **MAJOR R2 — Missing usage disables both token stops.**  
   `live_runtime.py:520`.

   Reproductions through the fake transport:
   - Report 15,000,000 input tokens while omitting `outputTokens`.
   - Report one completion with missing usage, then another with 500,000 output tokens.

   Both finish as `natural_end`, without `turn/interrupt` or a token-stop event. The implementation requires every input and output count to be available before evaluating either stop.

   Minimum fix: evaluate each stop independently using deduplicated known counts as a lower bound. Missing accounting data must remain marked missing, but cannot suppress a stop whose threshold is already established.

   Required test: exercise both stops with the opposite field missing and with an earlier usage-less completion. Confirm interruption, cap-ended evidence and unchanged deduplication.

3. **MAJOR R3 — A wall stop after `agent_finish` disappears from cap and smoke classifications.**  
   `queue_evidence.py:129`, `queue_checks.py:109`.

   Reproduction: use a one-second wall cap for a targeted fake-transport test; report usage, call `agent_finish`, then keep generation active past the deadline. Cleanup completes and `turn/interrupt` occurs. Nevertheless, the result records `natural_end`, `cap_ended=false`, and a wall duration of approximately 0.08 seconds, taken from `agent_finish`. All five smoke checks pass and `caps_binding=false`.

   The first admission boundary survives the later generation stop. Q’s additional cap detection covers token stops only. This loses the cap-ended classification required by section 8.1 and can conceal a binding smoke cap.

   Minimum fix: retain generation-stop evidence separately from tool-admission closure. Derive cap-ended status and smoke wall-cap binding from that evidence, with generation duration measured separately from cleanup.

   Required test: finish tool use, cross the wall deadline during generation, and confirm clean reconciliation, `cap_ended=true`, and a binding smoke cap.

4. **MINOR R4 — Compactions before and after v are reported as pre-v only.**  
   `queue_evidence.py:127`.

   Reproduction: compact, serve through v, compact again, then finish. The export inputs report `compaction_before_focal=true` and `compaction_after_focal=false`.

   Minimum fix: compute each flag independently against the focal-serving sequence. Preserve ineligibility whenever a pre-v event exists.

   Required test: cover neither, before-only, after-only and both-side compactions.

Spec comments, separate from findings: no spec amendment is needed to fix R1–R4. Recording the exact world/lane key encoding and distinguishing candidate arm memberships from executed memberships would make the accepted interpretations explicit.

Validation: 38 targeted tests passed on CPython 3.12; the candidate-study/order test also passed in the initial 3.11 batch. Independent temporary reproductions confirmed all four findings. All 19 changed Git blobs are ASCII with no CR bytes; `git diff --check` passed. The worktree remains clean at `b253dc5`. No live model/provider calls, guest access, commits, pushes or merges occurred.

**NO-GO — R1, R2 and R3.**
