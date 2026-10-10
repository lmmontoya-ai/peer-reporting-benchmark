# Revision 6 confirmation code review, round 6: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm6 on `5abc80d` (the merge of R6-fix6), the diff
`07dc418..5abc80d`, judged against spec section 2.1 (amendment P-A4). Link targets were
changed from local worktree paths to repository-relative paths, and links to the
reviewer's local scratch scripts were replaced by their file names. Read-only review; no
model calls.

Reviewed `07dc418..5abc80d` against section 2.1. **355 targeted tests passed**, plus independent offline consistency checks. Four major residual findings remain. None requires changing primary evidence or its committed receipt.

- **N1: FIXED.** The native 156-trial reproduction refuses altered lane-index statuses at inspection, export, packets, pilot decision and core preparation.
- **N2: NOT FIXED.** Changing only a registration’s phase refuses. Changing both derived phase copies still hides a sibling root. See R2.
- **N3: NOT FIXED.** Packet-only changes refuse, but rebuilding against an altered controller attempt copy passes upload validation. See R3.
- **N4: NOT FIXED.** Resolution changes refuse when supplied the correct second-review rule. The consumer still trusts an altered rule. See R4.
- **N5: FIXED.** One-of-twelve smoke evidence refuses; collection preparation and run reject the reduced population copy.
- **N6: NOT FIXED.** Archived-pause timestamp/count changes refuse. A genuine interrupted-archive state still permits an inconsistent pause record. See R1.
- **N7: FIXED.** All four tested execution-policy substitutions refuse before preparation, verification or runtime creation.
- **N8: FIXED.** Shared verification accepts a valid completed repair, then refuses missing, inconsistent or incomplete repair records; packet production also refuses.
- **N9: FIXED.** Repair consumption and subsequent verification reject nonexistent commits and evidence differing from the named commit.
- **N10: FIXED.** Missing ordinary labels refuse; the permitted historical `compound_type` omission remains accepted.

The remaining findings, most severe first:

1. **MAJOR R1, residual N6. Interrupted attempts leave pause records unchecked.** [live.py:1290](../../src/swarm_auth_bench/peer_reporting_v11/live.py), [live.py:1453](../../src/swarm_auth_bench/peer_reporting_v11/live.py).

   Reproduction: run one P assignment through fake transport with a provider-unavailable result. Simulate a crash in `attempt_summary`, after `attempt.json` is written but before `attempt_archived` is journaled. Commit its receipt. Move only the study pause’s `paused_at` and `resume_at` back 7,200 seconds and reseal it.

   `study_provider_pauses` accepts the inconsistent record. A successor starts **600 simulated seconds before the unchanged attempt’s recorded resume time**. The original receipt still verifies. The checker considers journaled pauses and journaled archives, overlooking the retained attempt in this crash state.

   Minimum fix: check incomplete-start pause records against retained attempt evidence where available. For settlement-only recovery, retain sufficient journal evidence to validate the recovered timing and count before subsequent admission.

   Needed test: this P crash case through successor admission, checking both altered timestamps and counts. Preserve legitimate recovery. Reproduction script (`pressure_crash_check.py`).

2. **MAJOR R2, residual N2. Matching derived phase copies still conceal a sibling population.** [review_population.py:48](../../src/swarm_auth_bench/peer_reporting_v11/review_population.py).

   Reproduction: prepare two roots for different assignments in the same P arm, with committed receipts. Packet production initially refuses the split population. Change only the sibling’s root-plan phase and registration/finalization phase to `smoke`, resealing and updating their plan-hash references. Leave its lane plans, study, journals, attempts and receipt unchanged.

   Packet production then succeeds with **one packet**, and a fresh export succeeds with **one row**. Phase filtering occurs before the sibling’s lane assignments are checked against the study.

   Minimum fix: validate each sibling’s phase and population against canonical study assignments before filtering. Check the applicable receipt when relying on a completed P root’s retained plans.

   Needed test: change both phase copies together and require refusal at export, packet and pilot consumers. The existing regression changes only the registration. Reproduction script (`consistency_checks.py`).

3. **MAJOR R3, residual N3. Upload validation treats a derived controller copy as primary evidence.** [review.py:521](../../src/swarm_auth_bench/peer_reporting_v11/review.py).

   Reproduction: produce a genuine P export and review packet. In the controller’s normalized attempt copy, replace the final response `"Inventory completed."` with `"I have a concern about the operation."` Rebuild the packet and bindings using the retained masking IDs and update their hashes.

   The unmodified `validate-review-upload` CLI returns **`valid=true`, `bindings_verified=true`**. The original archive and committed receipt remain unchanged and valid. Rebuilding from another derived copy does not satisfy section 2.1 item 1.

   Minimum fix: resolve the controller’s source to the registered archive, check its receipt, rederive the normalized attempt and compare the controller copy before validating packet content.

   Needed test: consistently alter controller-copy content, packet content and hashes while retaining the original archive; require CLI refusal. The reproduction script (`consistency_checks.py`) retains the altered inputs.

4. **MAJOR R4, residual N4. The second-review requirement is supplied, not verified.** [review.py:607](../../src/swarm_auth_bench/peer_reporting_v11/review.py), [review.py:630](../../src/swarm_auth_bench/peer_reporting_v11/review.py).

   Reproduction: use a native P packet whose canonical selection requires `second_review=true`. Retain one submitted human upload. With the correct rule, `final_answer_concern` remains unknown. Recompute the derived resolution with `second_review=false` and pass that boolean to `human_endpoints`.

   The endpoint becomes **`true`**, despite the unchanged selection requiring another review. The consumer checks the argument’s type, not its agreement with the canonical selection.

   Minimum fix: obtain the requirement from a verified, recomputed review selection rather than accepting an unchecked boolean.

   Needed test: retain the same archive and submitted upload, alter only the derived second-review rule/resolution, and require refusal. Reproduction script (`consistency_checks.py`).

Receipt checks passed for deterministic hashes, changed/added/missing evidence, and equality among recomputed bytes, the on-disk receipt and the committed blob. Earlier-level roots require no receipt. The tested post-run and prerequisite consumers reject missing receipts. These checks do not resolve R1–R4: their inconsistent derived records are either overlooked or checked against another derived copy.

The requested legitimate cases passed: five-lane, 500-call core build/preparation/verification; unrun and unscored populations; a P journal start without an archive; completed excluded P attempts through export/rederivation with zero packets; and valid completed ledger repairs. Earlier-level serialization and fixture guards passed. A fresh-process check of the 500-call core loaded no designated scoring or review modules.

For the three stated behaviours:

- **Receipt before cleanup, repair and abandonment: sound.** Check the existing evidence first. Cleanup or repair that appends journal evidence requires a newly committed receipt before export. Unstarted abandonment passed without changing receipt-covered bytes.
- **P refusal instead of quarantine: sound under item 2.** Genuine incomplete starts remain unscored. One qualification: the new archive/index-status check is unconditional, so that particular mismatch also refuses for earlier levels; “earlier levels keep the old handling” is not completely accurate.
- **Grid repair evidence: sound.** A4.1 verification needs the independently retained evidence location. The test rebuilding the binding against `8cbe359` passed. I did not access the private grid root.

Separate spec comment: “all primary evidence and nothing derived” is not the literal receipt definition in section 2.1. Item 3 explicitly includes root and lane plans, which section 2.1 classifies as derived. Separate approval, flag-resolution, human-review and A4.1 files are not enumerated there. Clarify direct receipt coverage versus evidence protected through checked references. This is a wording comment, not a code finding.

The review worktree remains clean. Scratch files and receipt-test repositories stayed in system temp. No provider/model calls or guest access occurred.

**NO-GO. Blocking items: N2, N3, N4 and N6, detailed as R1–R4 above.**
