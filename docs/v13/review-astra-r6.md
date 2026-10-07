# Revision 6 code review: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-review ([plan.md](plan.md)) on `19cb4a1`, the diff
`07515a9..19cb4a1`. Only the link targets were changed, from local worktree paths to
repository-relative paths. Read-only review; no model calls.
[construct-audit-response.md](construct-audit-response.md) records how each finding is
applied.

Three **MAJOR** findings prevent approval.

1. **MAJOR — Core plans do not enforce the pilot decision or lane exclusions.** [live.py:2205](../../src/swarm_auth_bench/peer_reporting_v11/live.py)  
   After six fake v2 compatibility probes, the builder accepted all **600 core calls across six lanes**, without gate results, pilot results, or a pilot decision record. It also has no eligible-lane input: a permitted 34/36 gate result containing one 4/6 lane cannot produce the required five-lane core through this workflow. This violates section 10.  
   **Minimum fix:** require and bind the pilot decision, apply its lane exclusions, and reject stopped or unresolved decisions when building and running core plans. Until supported, refuse core arms. Add tests for missing decisions and failed-lane exclusion.

2. **MAJOR — Commentary incorrectly counts as final disclosure.** [score.py:379](../../src/swarm_auth_bench/peer_reporting_v11/score.py)  
   P scoring receives every `final_response` output, including `phase="commentary"`. In a fake-runtime reproduction, commentary containing “Concern about \<focal-id\>” followed by a final “Task complete.” produced `owner_informed=true` and `disclosure_category=final_message_only`, without a report. With a focal report, it also incorrectly indicated that the concern was restated at closure. The runtime correctly excludes that commentary from closure notices.  
   **Minimum fix:** filter P final-text inputs consistently with closure notices, retaining finish summaries and preserving earlier-level scoring. Test concern-bearing commentary followed by an unrelated final answer; the existing “Working.” commentary test misses this.

3. **MAJOR — The P review selection cannot enter the review-packet workflow.** [review_plan.py:361](../../src/swarm_auth_bench/peer_reporting_v11/review_plan.py), [review.py:748](../../src/swarm_auth_bench/peer_reporting_v11/review.py)  
   The exported P selection has a new kind and lacks the bindings required by `write_review_packets`. Passing it to that workflow raises `KeyError: 'study_manifest_hash'`; the existing frozen-plan validator would reject its kind afterward. Section 7.4 selection therefore stops before packet production.  
   **Minimum fix:** provide a P packet route that verifies the selection against the sealed export, study and scores. Add an export-to-`review-packets` integration test. The current offline chain calls `build_review_bundle` directly for three selected attempts, bypassing this failure.

4. **MINOR — Reviewer packets omit notices and reactions.** [live_review.py:215](../../src/swarm_auth_bench/peer_reporting_v11/live_review.py), [review.py:227](../../src/swarm_auth_bench/peer_reporting_v11/review.py)  
   New room messages survive in `pressure_events`, but packets consume only `observed_peer_messages`. A reproduced packet omitted the pressure reaction present in its attempt, hiding context needed to interpret subsequent responses.  
   **Minimum fix:** include delivered notices and reactions as ordered, masked context, without controller condition labels. Test their presence in the finished packet.

5. **MINOR — Trigger logs lack the required focal-coverage snapshot.** [world.py:713](../../src/swarm_auth_bench/peer_reporting_v11/world.py)  
   Section 5.5 requires each trigger to record whether a stored report already covered the focal operation when reactions returned. Neither trigger event records this. Scoring reconstructs only the first-reaction value, which does not satisfy per-trigger logging.  
   **Minimum fix:** record and audit that snapshot for both triggers. Test differing coverage states at the first and second triggers.

6. **MINOR — Archive auditing does not bind inline receipts to emitted room events.** [world.py:218](../../src/swarm_auth_bench/peer_reporting_v11/world.py)  
   In a temporary archive, replacing a visible report receipt with the schema-valid private shape—removing its notice reference and `room_events`—and recomputing hashes still passed `audit_state`, while the notice and reactions remained logged. The audit therefore cannot establish that the required inline stimuli appeared in the receipt.  
   **Minimum fix:** validate visibility-specific receipts and their exact event references, contents and ordering against the corresponding trigger events. Add a rehashed-mutation regression.

**P-A1: PASS.** One visibility-neutral v2 tool set is selected per all-P root; mixed-level roots are refused. The frozen v1 manifest, descriptors and wire hashes are preserved.

**P-A2: PASS.** The specified P compatibility fixture, visible instructions and unchanged qualifier are wired through v2. Evidence must match the selected tool bindings; v1 evidence cannot qualify P calibration roots. The v1 route remains intact.

Focused offline validation: **1,430 passed, 1 skipped, 2 deselected**. The full suite was not completed. The generated audit was excluded; its size is **6,899,257 bytes**. The worktree remains clean at `19cb4a1`; no provider calls or guest-VM operations were made.

**NO-GO — blocking items: core-plan pilot/lane enforcement, P final-text scoring, and P review-packet integration.**
