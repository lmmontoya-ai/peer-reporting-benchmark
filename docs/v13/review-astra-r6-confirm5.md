# Revision 6 confirmation code review, round 5: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm5 on `317f731` (the merge of R6-fix5), the diff
`116facc..317f731`. Only the link targets were changed, from local worktree paths to
repository-relative paths. Read-only review; no model calls. The first launch of this
review was stopped by the provider's content filter after nine minutes; this is the reply
of the second launch, whose brief included the two preliminary findings (N2, N3) of the
first.

A: **FIXED.** Arm relabelling and substitution of all 100 excluded-lane model entries now refuse. The legitimate five-lane, 500-call build, preparation and verification pass.

B: **NOT FIXED in full.** Both original four-export filtering cases and both pilot suppression cases now refuse, but altering the lane indexes reproduces the P2-to-P3 change and permits 600 core calls. See N1.

Reviewed `317f731`. **217 targeted tests passed**, plus independent offline reproductions. Both preliminary findings are confirmed.

1. **MAJOR N1. Lane-index inconsistencies become accepted missing observations.** [live_review.py:276](../../src/swarm_auth_bench/peer_reporting_v11/live_review.py), [live_review.py:381](../../src/swarm_auth_bench/peer_reporting_v11/live_review.py).

   In a native 156-trial fake-transport archive, change only two flagged trials’ lane-index statuses to `incomplete_interrupted` and reseal. Their unchanged journals still record archived attempts. Inspection converts the mismatches into `quarantined_attempt`, with null attempts and scores. The new export passes re-derivation; the pilot changes from **P2 with four flags to P3 with two flags and ten correct answers**. `validate_core_decision` and preparation accept **600 core calls**. Attempt and journal bytes remained unchanged.

   Minimum fix: derive archive presence from journals and attempts, and reject index disagreements at packet and decision gates. Test this native reproduction through core preparation, while preserving genuine unstarted and excluded rows.

2. **MAJOR N2. Registry phase fields conceal sibling roots before validation.** [review_population.py:40](../../src/swarm_auth_bench/peer_reporting_v11/review_population.py).

   Confirmed the preliminary result: changing only sibling registrations’ phases and resealing produces **four accepted singleton packets**. With native roots, the unchanged split-arm population initially refuses; changing the sibling registration permits an export containing three rows and packet production.

   Minimum fix: verify registrations against their actual root plans before filtering by phase. Test phase mismatches across sibling roots at export, packet and pilot consumers.

3. **MAJOR N3. Packet text is not bound to the original output.** [review.py:513](../../src/swarm_auth_bench/peer_reporting_v11/review.py).

   Confirmed the preliminary result. Replace the packet’s final-response text, recompute its hash, and update the controller and binding hash references. Validation returns `bindings_verified=true`; resolved labels produce `final_answer_concern=true` against an unchanged original response of **“Inventory completed.”**

   Minimum fix: reconstruct the packet’s evidence-bearing content from the attempt using its retained masking map. Compare text, payloads, context, instructions and record evidence, not just output IDs. Test altered content with consistently recomputed hashes.

4. **MAJOR N4. Human endpoints trust edited resolution records.** [review.py:602](../../src/swarm_auth_bench/peer_reporting_v11/review.py).

   Two retained human reviews disagree, producing `status=disputed` and an unknown endpoint. Change only the derived resolution to `status=final`, `resolution=adjudicated`, and positive labels, leaving `adjudication=None`. `human_endpoints` returns `final_answer_concern=true`.

   Minimum fix: recompute resolution from the original reviews, adjudication and bound second-review requirement. Test disagreements, missing second reviews and absent adjudications at the endpoint consumer.

5. **MAJOR N5. The smoke gate trusts the plan’s expected population.** [live.py:2235](../../src/swarm_auth_bench/peer_reporting_v11/live.py).

   A fake-runtime study contains twelve canonical smoke assignments. A root with one successful assignment fails the gate against all twelve, but passes when `smoke_assignment_ids` contains that single ID. Collection preparation and run accept this derived list; the collection attempt executes.

   Minimum fix: derive the required smoke population directly from the sealed study at every gate, and bind the plan’s copy to it. Test one-of-twelve smoke evidence through collection preparation and run.

6. **MAJOR N6. Cross-root pause records are not checked against their owning journals.** [live.py:1359](../../src/swarm_auth_bench/peer_reporting_v11/live.py).

   Move a retained study pause’s timestamps two hours earlier and reseal, leaving its owning journal unchanged. Verification of the owning root detects the mismatch. A successor nevertheless builds and runs, starting after **60 simulated seconds instead of the required 600**.

   Minimum fix: verify every loaded study pause against its owning root’s durable evidence before admission and window counting. Test altered timestamps and counts across roots. This admission consumer is missing from the sweep table.

7. **MAJOR N7. A derived lane policy changes behavior after failed preflight.** [phase.py:756](../../src/swarm_auth_bench/peer_reporting_v11/phase.py), [live.py:801](../../src/swarm_auth_bench/peer_reporting_v11/live.py).

   Set a behavioral lane’s `continue_after_preflight_failure=true` and update its plan hash before preparation. The shared checker accepts it. With identical scripted preflight results, the original plan holds with zero attempt starts; the altered plan reports complete and starts the next attempt.

   Minimum fix: bind this field to the canonical phase policy, alongside other fixed execution-policy fields. Test the altered field at preparation, verification and run, retaining compatibility’s legitimate behavior.

8. **MAJOR N8. Shared export re-derivation omits current-root repair verification.** [review.py:807](../../src/swarm_auth_bench/peer_reporting_v11/review.py).

   After creating a native export, add a sealed repair record inconsistent with the unchanged lane journal. A fresh `export_live_review` refuses with `ledger repair records differ from the lane journal`. `verify_pressure_export_evidence` still accepts the existing export, and packet production succeeds.

   Minimum fix: recheck current-root repair evidence and its exported declarations in the shared consumer verifier. Test missing, inconsistent and incomplete repair records after export, plus valid completed repairs.

9. **MAJOR N9. Repair consumers do not verify the binding’s external commit.** [ledger_repair.py:513](../../src/swarm_auth_bench/peer_reporting_v11/ledger_repair.py).

   An offline binding with matching file hashes but a nonexistent forty-character commit is rejected by the binding builder. The real `repair_ledger` consumer nevertheless accepts it, restores the ledger, and subsequent root verification accepts the repair. The consumer checks commit syntax, not the claimed committed evidence.

   Minimum fix: require and verify the independently retained evidence location and commit at consumption, or an independently anchored exact binding. Test a resealed nonexistent commit and evidence differing from the named commit. This is separate from establishing human approval or publication.

10. **MINOR N10. Historical-label fallback accepts missing required fields.** [live.py:783](../../src/swarm_auth_bench/peer_reporting_v11/live.py).

    Delete a non-null L2 `near_miss_type` from a lane entry and update its hash. Preparation and verification succeed through fixture fallback; the verification report changes `wrong_version` to null, and export raises `KeyError('near_miss_type')`.

    Minimum fix: restrict fallback to fields genuinely omitted by the historical format. Require ordinary serialized labels to remain present. Test missing required labels alongside valid omitted `compound_type` and explicit mismatches.

The execution boundary passes: calling the new assignment checker in a fresh process loaded none of the designated post-hoc modules. `pilot.verify_core_binding` remains execution-only.

For item 4, the L0/S plan/root/export byte-identity guard and earlier-fixture golden checks passed. Explicit altered labels are rejected, including `compound_type`; the missing-field problem in N10 remains.

For item 5, the reported **12.76 seconds for 156 rows and 51.89 seconds for 600 rows are acceptable for offline gates**. Keep full re-derivation. Reusing verified data within one operation can reduce repeated work. A cheaper disk cache needs an independent immutable or authenticated binding; another editable seal is insufficient.

Legitimate unscored cases passed. A separate native P exclusion also passed re-derivation and produced zero packets. No provider calls or guest access occurred; tracked files remain unchanged.

Automatic approval review rejected removal of the initially created environment with “blocked by policy.” The ignored `.venv` remains in the review worktree.

**NO-GO. Blocking items: N1–N9.**
