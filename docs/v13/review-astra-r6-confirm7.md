# Revision 6 confirmation code review, round 7: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm7 on `8687d5a` (the merge of R6-fix7), the diff
`1497625..8687d5a`, judged against spec section 2.1 (amendment P-A4). Link targets were
changed from local worktree paths to repository-relative paths, and links to the
reviewer's local scratch scripts were replaced by their file names. Read-only review; no
model calls.

Reviewed `1497625..8687d5a` against section 2.1. **R1–R4 are fixed; one new MAJOR finding remains.** All 295 tests and consistency checks passed, including checks that reproduce the new finding.

- **R1: FIXED.** Retained-attempt and settlement-only cases reject altered pause timestamps/counts. Legitimate recovery preserves the journaled pause, and successor admission respects its resume time.
- **R2: FIXED.** Matching altered phase copies refuse at export, packet generation and pilot decision. Missing sibling receipts refuse even when the derived finalization record is removed.
- **R3: FIXED.** The original controller/packet reconstruction refuses because the controller attempt differs from the registered archive. Genuine archive-backed uploads pass.
- **R4: FIXED.** Altered second-review arguments, selection rows and resolutions refuse. One submitted review leaves the endpoint unknown when canonical selection requires two.

The round-six reproductions were rerun with fresh temporary fixtures because their original source directory had expired, adapting the changed API arguments and expected refusal assertions.

For items 2–4:

2. **The proposed evidence-free state is not reachable under the current adapter/evaluator.** Observed usage updates differ from a known final usage total. An overload ends with a failed turn, so [`live_runtime.py:974`](../../src/swarm_auth_bench/peer_reporting_v11/live_runtime.py) leaves the final total unavailable. Settlement remains `bounded_by_reservation`, with a pause classification and journaled pause. I tested two updates totaling **15,838 tokens**, interrupting both before the study write and after it but before `attempt.json`. Both recovered correctly and delayed successor admission. No additional journaling fix is needed for this proposed case.

3. **The journal addition is compatible with the checked readers.** The field is restricted to v2; 12 inspected v1 settlements contained no added pause object. Earlier fixture golden checks and the L0/S plan/root/export byte guard passed. Four completed v2 attempts had identical journal, sealed-attempt and study pauses. A4’s exact ledger reconstruction and ordinary ledger-history verification accepted the additional object; recovery preserved its timing and count.

4. **Archive-backed upload validation and endpoints refuse missing or inconsistent archive context.** Explicit replay validation rejects an unchanged `execution_kind=live_model` attempt, and the live CLI offers no replay option. Without `--controller`, upload validation remains a schema-only check returning `bindings_verified=false`. However, packet generation still has the unchecked path described below.

**MAJOR R5. Optional archive verification permits inconsistent live reviewer packets.**  
Location: [`review.py:1055`](../../src/swarm_auth_bench/peer_reporting_v11/review.py), with routing based on derived records at line 1044.

Concrete reproductions:

- In a genuine earlier-level export, change only the normalized final answer from `"Task submitted."` to `"I have a concern about the operation."`, reseal it, and update the export’s attempt hash. Run `review-packets EXPORT --plan PLAN --output OUT` without `--study`. It exits **0**, writing six reviewer packets, including the altered text. Supplying `--study` makes the same inputs refuse.
- In a genuine P export, change the selected assignment’s **exported manifest copy** to `level=L0`, `arm=channel_failure`; regenerate an ordinary review plan and update the derived hash references. Alter the exported final answer similarly. The same CLI path exits **0** and writes one packet containing that text. The primary study, archive and original committed receipt remain unchanged and valid.

These are defects under section 2.1 items 1, 2 and 4. Packet production accepts derived records that disagree with primary evidence; neither reproduction rewrites primary evidence.

Minimum fix: require registered-source verification before choosing the live review path or writing packets. Determine the level and selection rules from the verified study. Keep unregistered authored replay behind an explicit offline option.

Needed tests: require refusal for both reproductions when `--study` is omitted, while retaining positive earlier-level/P archive-backed cases and explicit replay coverage. Runnable consistency checks (`test_earlier_packet_consistency.py`).

Separate spec comments: none.

The worktree remains clean. Scratch data and receipt-test repositories stayed in system temp. No model/provider calls, guest access or private-repository access occurred.

**NO-GO — blocking item: MAJOR R5.**
