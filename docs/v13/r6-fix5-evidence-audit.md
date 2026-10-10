# Evidence gate audit through R6-fix7 (P-A4)

[Specification section 2.1](spec.md#21-evidence-and-trust-boundary-amendment-p-a4)
is normative. The sealed study, lane journals and archived attempts, approval
texts and flag resolutions, A4.1 evidence, and submitted human reviews and
adjudications are primary evidence. Every other retained record is derived.
A resealed derived record must still agree with its primary inputs. A mismatch
refuses the operation; it cannot remove an observation from a denominator or
turn it into an excluded, quarantined or unscored row.

The receipt is the only added retained root-evidence record. It hashes file
bytes, including the study manifest, root plan, lane plans, journals and every
file in archived attempt directories. Keys are relative to the root. A v2
compatibility root precedes the study and therefore has no study-manifest file
to hash. The canonical sealed JSON bytes must equal both the receipt on disk
and its exact committed blob at local Git HEAD. No receipt path is stored in a
plan or read from an environment variable.

| Consumer or gate | Derived records read | Primary evidence checked before use |
| --- | --- | --- |
| `build_phase_plan`, `build_assignment_plan` | Assignment copies, lane plans, gate evidence, consumed ledger, core decision | Canonical sealed-study assignments, labels, fixtures and instructions; protocol execution policy; actual compatibility/smoke attempts; prior journals; offline-recomputed core decision. Prerequisite v2/P archives require committed receipts. |
| `prepare_live_root` | Root/lane plans, smoke population, gate bindings, consumed ledger, core decision | Study assignments and caps; fixed phase policy; canonical smoke population; current prior-root starts and repair evidence; recomputed pilot decision before any root write. v2 compatibility prerequisites and P prior roots require receipts. |
| `verify_live_root`, `run_live_phase` | Plans, indexes, budgets, start claims, registrations, supersession, consumed ledger, retained review plan | Study assignments/fixtures/instructions/caps; journal hash chains, attempt checkpoints, exact usage history and user authorization. Later-root verification and run recheck receipts of prior/prerequisite archives. |
| `compatibility_evidence`, `check_phase_gates` | Qualification declarations and plan tool/catalog/caps copies | Actual verified archived qualification results and current protocol tool/catalog/caps bindings; a v2 compatibility root's committed receipt before reading its archives. |
| `smoke_evidence` | Smoke population copy, registration, amendments | Population read directly from the sealed study at each call; all corresponding starts and archived outcomes, cleanup evidence and user-approved failed-attempt amendments. The collection plan's population copy must equal the study's. |
| Study pause loading and admission (`study_provider_pauses`, `check_study_pauses`, `reconcile_study_pauses`, admission reload/window counting) | Study pause records, claiming/root identifiers, timestamps and window counts | Owning root's hash-chained journal, archive hash and retained pause in the attempt; the same `check_study_pauses` comparison used in owning-root verification, plus study starts and registered ownership. Retained attempts are checked even without an archive event. V2 settlements retain the exact pause in the journal for recovery without an attempt. |
| `consumed_attempts_in_root`, `prior_root_ledger`, `verify_consumed_ledger`; preparation's prior-root ingestion | Lane indexes, consumed ledger, registrations, supersession and repair records | Canonical assignments; journaled starts and index checkpoints; study start claims and owning paths; committed corruption evidence and exact repair declarations. P/v2 prior archives require receipts before their journals are opened. |
| `abandon_root` | Registration and abandonment data | Registered plan/path and absence of journaled starts; P receipts checked before journal inspection, with an explicit directory accepted by API and CLI. Pending roots without a written plan retain the existing journal-inspection crash check. |
| `record_amendment`, `amended_attempts` | Amendment identity and excluded-attempt declarations | User approval text and study identity; verified failed smoke attempts, transport/usage results and cleanup evidence. Valid and unstarted trials cannot be amended into failed exclusions. |
| `reconcile_cleanup` | Cleanup and start declarations | Current P/v2 root receipt before archive verification; verified root/start evidence and the actual environment-check result. Tests inject that check; this package's validation does not contact a guest. |
| `repair_ledger`, `verify_root_ledger_repairs` and their consumers | Binding, repair records, completion and journal declarations | Explicitly supplied independently retained evidence locations; binding rebuilt with the existing builder against its named local Git commit; unique single-bit reconstruction, identity marker and exact journal prefix. The repair checks its current P root's receipt before reading its journal. All consumers recheck the binding commit, including repair resumption and current/prior-root verification. |
| Resource proposal and caps freezing | Root plans, registrations, repair/usage summaries and proposed caps | Verified actual archived usage/wall measurements and journal evidence. P/v2 input archives require receipts. Freezing uses the user's approval of the exact proposal. |
| `inspect_live_root` within export/re-derivation | Lane indexes and attempt summaries | Journaled archive presence and retained archived attempts. An index that hides a journaled archive refuses outside the per-row quarantine handler. A start without an archive retains the existing interrupted/recovery behavior. This lane-index check also refuses inconsistent earlier-level indexes; earlier levels do not keep the old handling for this mismatch. |
| `export_live_review` | Plans, registrations, consumed ledger, indexes, amendments, repair records and declarations | Canonical study binding; study starts; journal and attempt evidence; current repairs rebuilt against their committed incident evidence; each P root's committed receipt before archive access. |
| `registered_pressure_partitions`, `pressure_export_partitions` | Registrations, root/lane plans and exported population | Every root's entries are checked against sealed-study assignments before phase filtering; the canonical split supplies the phase, which both plan and registration must equal. P siblings with retained journal/attempt evidence require committed receipts independently of registration state; finalized P plans also require receipts. Excluded/unscored assignments remain in the planned population. |
| P packet production, `build_pilot_decision`, `validate_core_decision` | Export population, rows, attempts, scores, exclusions, repair declarations, selection and decision | Shared verifier checks the source receipt, current repairs and declarations, reopens the source archive, rederives every row/attempt/score, and recomputes selection before filtering. Pilot mechanical counts and selection are recomputed; user flag resolutions and ceiling choices remain primary inputs. |
| Earlier-level review-plan authoring/verification and packet production | Review plan and export binding | Full deterministic plan recomputation from the study and protocol seed; exact retained study/authorized plan hash. Earlier-level archives require no P-A4 receipt. |
| Core decision execution binding | Retained decision and eligible-lane declarations | Exact authorized plan's offline-recomputed decision; canonical assignment arms and lanes. This execution check reads no score exports and imports no post-hoc modules. |
| Human upload validation | Controller attempt, selection row, packet and private bindings | Shared `verified_review_context` resolves the export to its registered archive and checks its receipt, then checks the controller copy and recomputed selection. The packet builder reconstructs evidence-bearing content from that verified attempt with retained masking IDs: output text/payloads, context, notices/reactions, instructions, delivered and retrievable records, and record checks. Consistently changed packet/controller hashes do not authorize changed content. |
| Human endpoints | Resolution and any supplied structured score | Existing resolver recomputes resolution from submitted initial reviews, adjudication, authorization truth and the second-review rule from the verified, recomputed selection; complete resolution comparison. Live endpoints require the same verified context as uploads. Supplied structured scores are checked by rescoring the primary attempt. |
| Receipt writing and checking | Receipt | Deterministic live-file hashes; exact on-disk bytes and the matching committed Git blob at HEAD. Changed, added or missing attempt/journal files refuse. |

Historical lane omission fallback is limited to `compound_type`; non-S/P
entries also omit `difficulty`, `block`, `prevalence_k`, `post_condition`,
`visibility` and `pressure`, and S entries omit `visibility` and `pressure`.
Every other serialized assignment label must be present and equal. No earlier
plan, prepared-root or export field was added or removed.

The current root's execution verifier remains usable during execution and crash
recovery, before a receipt can exist. It performs the internal consistency
checks of section 2.1 item 3. The post-run gates listed above require the receipt;
a later root also requires receipts of the prerequisite/prior roots it reads.
Pure fixture builders, authored offline replay, scorers and summaries retain
their existing formats. They consume primary supplied inputs or produce derived
outputs; none independently admits a live trial or authorizes a score export or
pilot/core decision. The gates that use their outputs recompute them.

Earlier-level serialization and the execution/post-hoc import boundary are
unchanged. Publication of a receipt commit remains outside these offline checks,
as section 2.1 item 3 states.

## R6-fix7 change and test map

Implementation and regression tests are committed as `406cfbd`; `0bf062b`
updates the authored-replay CLI fixture to assert the missing-archive refusal
and to use the explicit replay API for its positive case. `5a4329f` binds
sibling receipt scope to retained primary journal/attempt evidence as well as
finalization, so removing a derived finalization record cannot suppress the
receipt check for a completed archive.

| Finding | Code change | Tests in `test_r6_remaining_consistency.py` |
| --- | --- | --- |
| R1: interrupted pause records | `live.study_provider_pauses` checks retained `attempt.json` against its journaled start even without `attempt_archived`, then compares every pause copy with that evidence. V2 `usage_settled` retains the exact pause for settlement-only recovery. | `test_interrupted_pause_primary_evidence_at_successor_admission`: retained-attempt and settlement-only states, altered timestamps and counts, legitimate recovery, successor admission and unchanged receipt-covered evidence. |
| R2: sibling phase | `review_population.registered_pressure_partitions` checks all lane entries against the study before filtering, derives the phase from their canonical split, checks both phase copies, and checks applicable sibling receipts whenever primary journal/attempt evidence exists or the plan is finalized. | `test_sibling_matching_phase_copies_refuse`: matching root-plan and registration/finalization changes refuse at export, packets and pilot; `test_sibling_receipt_checked_before_population_filtering` refuses a missing receipt for prepared and completed siblings, including a completed sibling with its derived finalization record removed; primary attempt/journal bytes remain unchanged. |
| R3: controller attempt copy | `review.verified_review_context` reuses `verify_pressure_export_evidence`, compares the controller attempt with the verified registered archive, then checks bindings and rebuilds the packet. Upload CLI propagates receipt and repair evidence. | `test_controller_attempt_packet_and_hashes_compared_with_archive_cli`: consistently rebuilt controller, packet and hashes refuse in an unmodified CLI process; original inputs pass. `test_review_endpoints.py::test_retained_blinding_warning_preserves_primary_packet_validation` covers a legitimate model-name warning. |
| R4: second-review rule | Uploads and endpoints share `verified_review_context`. The selection row and rule are recomputed; any supplied rule or resolution must agree. | `test_second_review_rule_recomputed_from_archive_selection`: changed argument, controller selection row and recomputed resolution refuse with unchanged archive and submitted review. |

The original reviewer scripts were copied unchanged into system temp and run
against a separate system-temp `1497625` snapshot. They reproduced all four
findings offline: a start 600 simulated seconds early with an unchanged receipt,
a sibling hidden by matching phase copies, an altered controller accepted with
`bindings_verified=true`, and a required second review bypassed by a supplied
rule. The scripts and the original evidence were not modified.

V2 provider-pause settlements add one `provider_pause` object to the existing
`usage_settled` journal event, before any study-pause write or archive write.
It records the exact timing, window count and admission rule subsequently used
by the attempt and study record. Settlement-only recovery copies that journal
evidence rather than creating a new time or count. No new derived file is added.
V1 journal serialization, all earlier plans, prepared roots and exports keep
their bytes. Historical v1 settlement-only recovery has no retained timing/count
evidence to check against and retains its existing recovery path; this journal addition is scoped
to tool set v2 as requested.

Earlier-level live packet consumers use the same registered-export verifier
when given their study directory. The private controller retains that source
locator; earlier exports are unchanged. Authored offline replay examples have
no registered archive. Their unit callers explicitly opt into replay validation
and endpoints; the live upload CLI does not offer that option, and live endpoint
calls without a registered controller refuse.
`test_review_packets.py::test_review_packets_command_and_unregistered_upload_consistency`
checks that the live CLI reports the absent archive and that explicitly opted-in
authored replay validation still succeeds. The native earlier-level integration
chain checks registered-archive validation and endpoint rule consistency.

## R6-fix7 final offline validation

The full suite ran on final code/test commit `5a4329f` as four simultaneous
`uv run --offline python -m pytest -q <files> -p no:cacheprovider` processes.
All 75 test files were assigned exactly once, with no overlap or omissions.
The groups contain 18, 18, 21 and 18 files. Each process used its own
system-temp `--basetemp` outside Git repositories; `PEER_V1_CONTROLLER_DIR`
was unset. All four exited zero. Only this audit document changed after
the run began.

| Worker | Files | Collected | Result | Pytest duration |
| --- | ---: | ---: | --- | ---: |
| 1 | 18 | 774 | 773 passed, 1 skipped | 1,968.80 seconds (32:48.80) |
| 2 | 18 | 817 | 816 passed, 1 xfailed | 1,780.40 seconds (29:40.40) |
| 3 | 21 | 1,427 | 1,427 passed | 1,929.63 seconds (32:09.63) |
| 4 | 18 | 1,459 | 1,459 passed | 1,850.19 seconds (30:50.19) |

Total: **4,475 passed, 1 skipped, 1 expected xfail; 4,477 collected**.
Four-worker wall time was **1,972.36 seconds (32:52.36)**. All 17 new
regression cases passed. The skip is
`test_score.py::test_p1_collection_regression`, whose private controller
directory is unset. The existing strict Win32 xfail is
`test_windows_ledger_storage.py::test_atomic_ledger_update_while_reader_is_open`.
It covers Windows readers denying `os.replace`; it was not introduced here.

The L0/S plan/root/export byte-identity guard and all three earlier-fixture
golden checks passed. Both execution boundaries passed:
`test_live_w09.py::test_no_execution_module_imports_a_post_hoc_module` and
`test_pilot_decision.py::test_runtime_binding_does_not_load_post_hoc_code_or_read_exports`.
The changed Python files pass Ruff. `git diff --check 1497625` and byte scans
found no CR bytes, mojibake or non-ASCII text in the 14 changed files.
No provider/model calls, guest access, push or merge occurred.

The exact file groups, per-worker logs and JUnit XML remain under
`C:/Users/luism/AppData/Local/Temp/r6-fix7-verified-suite-bek6iw17/`.
The manifest records the tested commit and all four process launchers.

## R6-fix6 change and test map

The implementation is committed as `491b6a0` and `a079cb8`. The latter closes
receipt handling in abandonment, repair and cleanup, supports receipt refresh,
and updates an earlier serialization-only test double. The finding regressions are in
`tests/v11/test_r6_evidence_consistency.py`; receipt byte and Git cases are in
`tests/v11/test_receipts.py`. The table names the exact test functions, with
parameterized cases counted by pytest.

| Finding | Code change | Regression test |
| --- | --- | --- |
| N1: lane index | `live_review.inspect_live_root` refuses an index that hides a journaled archive, before quarantine handling. | `test_native_156_trial_index_mismatch_refuses_through_600_call_core_preparation`: unchanged primary bytes, four-flag P2 baseline, two altered indexes, inspection/export/packets/pilot/core preparation all refuse. |
| N2: registration phase | `review_population.registered_pressure_partitions` checks each registration against its root plan before filtering. | `test_sibling_registration_phase_checked_before_filtering`: sibling phase mismatch at export, packets and pilot. |
| N3: packet content | `review.validate_review_upload` rebuilds the packet with `build_review_bundle` and its retained masking map. | `test_packet_content_recomputed_after_consistent_hash_changes`: text, payload, context, instructions and record evidence, with consistent changed hashes. |
| N4: resolution | `review.human_endpoints` recomputes the complete resolver result with an explicitly supplied second-review rule. | `test_endpoint_resolution_recomputed_from_reviews_and_bound_rule`: disagreement, missing second review and absent adjudication; legitimate endpoints remain valid. |
| N5: smoke population | `live.smoke_evidence` reads the sealed study; shared plan binding checks its population copy. | `test_one_of_twelve_smoke_population_refuses_collection_preparation_and_run`: one successful smoke attempt cannot admit collection. |
| N6: pauses | Study pause loading compares every record with its owner's journal/archive through `check_study_pauses`, before admission and window counting. | `test_successor_pause_admission_checks_owning_journal`: altered timestamps and window counts refuse before successor transport creation. |
| N7: execution policy | `live.check_execution_policy` binds all fixed lane policy fields to the protocol and phase. | `test_lane_execution_policy_refuses_before_prepare_verify_or_run`: four fields; compatibility's legitimate continuation remains covered by existing qualification chains. |
| N8: repairs after export | `review.verify_pressure_export_evidence` reruns `verify_root_ledger_repairs` and compares exported repair/deviation declarations. | `test_shared_export_rederivation_rechecks_current_root_repairs_after_export`: missing current-root receipt refuses repair, valid completed repair passes, then missing, inconsistent or incomplete records refuse shared verification and packet production. |
| N9: binding commit | `ledger_repair.check_binding_evidence` rebuilds every consumed binding from explicit evidence directories and the named local Git commit. | `test_repair_consumer_rechecks_named_local_commit`, `test_root_verification_rechecks_binding_against_committed_external_evidence`; existing repeated-repair and repair-resumption tests. |
| N10: historical labels | `live.check_assignment_binding` lists historical omissions; all other labels must be present and equal. | `test_historical_entry_requires_ordinary_labels`: three missing ordinary labels and a valid omitted `compound_type`; existing explicit-label mismatch cases. |

| Receipt requirement | Code change | Tests |
| --- | --- | --- |
| Execution-side module; deterministic hashes-only record | `receipts.receipt_bytes`, with no post-hoc imports, hashes the live manifest, root/lane plans, journals and every archived attempt file under relative paths. | `test_valid_committed_receipt_is_deterministic_and_holds_hashes_only`; native pressure chain checks the study-bound record. |
| One offline command, named by plan seal | `root-receipt ROOT --receipt-directory DIR [--study STUDY]` writes `<plan seal>.json`. | `test_cli_writes_an_offline_receipt_without_committing`. |
| Exact recomputed, disk and HEAD bytes | `receipts.check_receipt` uses the shared `git_evidence.committed_files` helper; no receipt parsing. | `test_receipt_requires_exact_disk_and_head_content`: missing, staged/uncommitted, changed disk and different committed bytes. |
| Refresh after an authorized primary change | The same writing command replaces derived receipt bytes at the same plan-hash path; the check still requires a new exact HEAD commit. | `test_receipt_refresh_requires_a_new_commit`: writing new hashes alone refuses; committing the refreshed record passes. |
| Changed, added or missing primary file refuses | Receipt discovery includes extra lane journals/plans and every file in attempt directories. | `test_primary_file_changes_after_commit_refuse`: changed, added and missing attempt or journal, each after a real commit. |
| P and v2 compatibility scope; earlier levels unchanged | `receipts.requires_receipt` checks canonical lane levels or v2 compatibility tool set. | `test_earlier_level_root_needs_no_receipt`; L0/S byte-identity guard; earlier-fixture goldens. |
| Export and shared packet/pilot gates | Explicit directory passed to `export_live_review` and `verify_pressure_export_evidence`, before journals/archives are read. | `test_native_post_run_consumers_require_committed_receipts`: export, packets and pilot cases; complete offline pressure chain. |
| Later-root build, preparation and verification | `consumed_attempts_in_root` covers prior ingestion; `compatibility_evidence` and `check_prerequisite_receipts` cover v2 prerequisites. | `test_native_post_run_consumers_require_committed_receipts`: build/prepare/verify with a P prior; `test_v2_compatibility_prerequisite_receipt_required_at_later_root_gates`: three prerequisite gates. |
| Other post-run root readers | `abandon_root`, `repair_ledger` and `reconcile_cleanup` check the current root before journal/archive use. | Native missing-receipt abandonment/cleanup cases stop before journal inspection or verification; N8's real repair case checks missing and valid receipts; `test_unstarted_pressure_abandonment_cli_accepts_committed_receipt` covers API/CLI propagation without changing primary bytes. |
| Explicit argument, no environment lookup | One `receipt_directory` argument is propagated through entry points; CLI exposes `--receipt-directory`. | CLI receipt and core cases, missing-directory refusals, native chain. |
| Real Git helper for existing P chains | `tests/v11/receipt_helpers.commit_receipt` commits exact bytes in an isolated system-temp Git repository. | `test_revision6_entire_offline_chain`, native CLI pilot/core fixtures and v2 integration qualification. |

An authorized repair or cleanup can append journal evidence. Its refreshed receipt
must be committed before a later reader uses those changed bytes. Writing the
refreshed record does not satisfy the gate by itself.

## R6-fix6 final offline validation

The full suite ran on final execution code commit `a079cb8` as four simultaneous
`uv run --offline python -m pytest -q <files> -p no:cacheprovider` processes.
The groups cover all 74 test files exactly once, with no overlap or omissions.
Each process used its own system-temp `--basetemp` outside Git repositories;
`PEER_V1_CONTROLLER_DIR` was unset. All four exited zero. Only this audit document
changed after the run began.

| Worker | Files | Collected | Result | Pytest duration |
| --- | ---: | ---: | --- | ---: |
| 1 | 18 | 1,349 | 1,348 passed, 1 skipped | 1,008.36 seconds (16:48) |
| 2 | 18 | 1,111 | 1,111 passed | 2,004.77 seconds (33:24) |
| 3 | 19 | 886 | 885 passed, 1 xfailed | 1,651.50 seconds (27:31) |
| 4 | 19 | 1,114 | 1,114 passed | 1,846.94 seconds (30:46) |

Total: **4,458 passed, 1 skipped, 1 expected xfail; 4,460 collected**.
Four-worker wall time was **2,006.53 seconds (33:26.53)**. All 54 new cases passed.
The skip is `test_score.py::test_p1_collection_regression`, whose private
controller directory is unset. The existing strict Win32 xfail is
`test_windows_ledger_storage.py::test_atomic_ledger_update_while_reader_is_open`.
It covers Windows readers denying `os.replace`; it was not introduced here.

The L0/S plan/root/export byte-identity guard and all earlier-fixture golden
checks passed. Both execution boundaries passed:
`test_live_w09.py::test_no_execution_module_imports_a_post_hoc_module` and
`test_pilot_decision.py::test_runtime_binding_does_not_load_post_hoc_code_or_read_exports`.
The changed Python files pass Ruff. `git diff --check` and byte scans found no
CR bytes, mojibake markers or newly added non-ASCII text in the 27 changed files.
Existing U+00E9 literals in `test_ledger_repair.py` remain unchanged.
No provider/model calls, guest access, push or merge occurred.

## R6-fix6 receipt-check timing

`test_revision6_entire_offline_chain` times one complete `check_receipt` call
after committing each native fake-transport root's receipt. The filesystem is
warm from verification and receipt writing. The check includes discovering and
hashing the live files and comparing disk and Git HEAD content. It does not
normalize or score trials. Other suite workers were active during these checks.

| Native root | Archived trials | Receipt check |
| --- | ---: | ---: |
| Gate/pilot | 156 | 1.450 seconds |
| Core | 600 | 4.539 seconds |

These are individual measurements from this offline suite run, not performance
limits or measurements of real provider transcripts.

## R6-fix5 historical changes and validation

A changes `src/swarm_auth_bench/peer_reporting_v11/live.py`. One shared canonical
assignment checker runs at build, prepare, root verification and run start;
preparation checks it before root or registry writes. Core status and eligible
lane identity come from canonical assignments. Lane identities, counts, indexes,
caps, fixture content and instruction hashes also remain bound. Runtime checks
use no post-hoc modules.

`tests/v11/test_study_assignment_binding.py` adds 28 cases: arm relabeling,
model/effort substitution, other fixed labels and derived metadata, sealed-root
verification/run refusal, missing primary bindings, the legitimate five-lane
500-call core, and L0/S plan/root/export byte identity. Existing bound-plan callers
were updated in `test_ledger_repair.py`, `test_live_w09.py`,
`test_offline_social_chain.py` and `test_social_protocol.py`.

B changes `review.py`, `pilot_decision.py` and `live_review.py` in the same package.
Packet production and pilot-decision building share
`verify_pressure_export_evidence`. It resolves the actual source through the live
study registry and reopens it with `inspect_live_root` and the scorer. It checks
the complete population, every row, normalized attempt and attempt hash, declared
exclusions and recomputed review selection before filtering. The export's root
snapshot cannot supply the source authority. All-unscored P exports retain the
source locator and an empty selection; earlier-level serialization is unchanged.

`tests/v11/test_export_evidence_binding.py` adds 19 cases. Both four-export attacks
and both pilot attacks are refused, along with score, eligibility, attempt,
status, selection and JSON-type substitutions. The native positive case covers
an all-unrun P root, zero packets and a coexisting historical H2 plan that omits a
non-null fixture-only label. The four existing `test_cli_core.py` cases now use
actual registered archives made with fake transport, including a 156-trial pilot,
so the unmodified CLI subprocess validates primary evidence. Supporting fixtures
were updated in `review_root_helpers.py`, `test_pilot_decision.py`,
`test_review_pressure.py` and `test_score_social.py`.

The sweep changes `review_population.py` and `ledger_repair.py`, plus the shared
root, export and endpoint gates in `live.py`, `live_review.py` and `review.py`.
`tests/v11/test_evidence_gate_sweep.py` adds 12 cases for downstream root gates,
standalone consumption/ledger checks, repair before writes, P exports using an
earlier review plan, empty/whole-arm partial exports and fabricated endpoint
scores. `test_review_population.py` adds five fixed-label cases. Existing refusal
assertions in `test_live_r2.py` and `test_resources.py` were updated because the
primary-binding refusal now occurs earlier. The table above lists every consumer
group and its primary evidence. There are 64 new parameterized test cases in total.

### Historical offline validation (R6-fix5)

The complete suite ran on code commit `0322d98` as four simultaneous
`uv run --offline python -m pytest -q` processes over disjoint sets of all 72 test
files. Every process ran from the worktree root with its own system-temp
`--basetemp`; `PEER_V1_CONTROLLER_DIR` was unset. All four exited zero.

| Worker | Files | Collected | Result | Pytest duration |
| --- | ---: | ---: | --- | ---: |
| 1 | 17 | 887 | 886 passed, 1 xfailed | 1:03:34 |
| 2 | 18 | 1,253 | 1,252 passed, 1 skipped | 0:34:14 |
| 3 | 18 | 1,127 | 1,127 passed | 0:22:14 |
| 4 | 19 | 1,139 | 1,139 passed | 0:29:18 |

Total: **4,404 passed, 1 skipped, 1 expected xfail; 4,406 collected**.
Four-worker wall time was **3,818.39 seconds (1:03:38)**. The skip is
`test_score.py::test_p1_collection_regression` because its private controller
directory is unset. The existing strict Win32 xfail is
`test_windows_ledger_storage.py::test_atomic_ledger_update_while_reader_is_open`:
Windows readers deny `os.replace` and cause simulated execution-check holds.
Neither condition was introduced by these fixes.

The L0/S plan/root/export byte-identity guard passed. All three earlier-fixture
golden byte checks, frozen v1 tool descriptors/wire hashes, committed fixture-audit
regeneration, both execution/post-hoc boundary guards and all four CLI core cases
passed. No spec or review document was edited. Every commit was checked with
`git diff --check` and a byte scan for carriage returns; changed files contain zero
CR bytes. No real provider/model calls, guest access, push or merge occurred.

### Historical full re-derivation cost (R6-fix5)

The final code's shared export verifier was timed in one fresh Python process,
using the native fake-transport archives from this full run and a warm filesystem.
These are complete registered roots and exports.
The timed operation includes root and durable-evidence checks, normalization,
scoring, all row/attempt comparisons, exclusions and selection recomputation.

| Native archive | Rows | Scored rows | Complete shared check |
| --- | ---: | ---: | ---: |
| Pilot | 156 | 156 | 12.76 seconds |
| Core | 600 | 600 | 51.89 seconds |

Full re-derivation is retained for these offline gates. No weaker fallback was
needed at the measured cost. Timings use fake model transport; real collected
provider transcripts were not benchmarked. The source work is complete; the
private-controller skip and existing Windows xfail remain as described above.
