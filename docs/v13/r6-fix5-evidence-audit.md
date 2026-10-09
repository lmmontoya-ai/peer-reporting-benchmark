# R6-fix5 evidence gate audit

The sealed study's assignment table and fixtures are the primary plan inputs. A
content seal detects changes relative to a retained reference; it does not make
an independently resealed plan, export, selection, or decision authoritative.

Earlier-level plan, prepared-root and export formats are unchanged. The explicit
L0/S archive regression compares their bytes with and without the added
predicates, freezing nonce and journal time. P exports retain their existing
study locator and review-selection fields even when every row is unscored.
Historical lane plans keep their shape: fixture-only fixed labels are checked
against the primary sealed study fixtures.
Runtime assignment checks use execution code only. Scoring and outcome-selected
review stay offline.

| Consumer or gate | Primary evidence checked |
| --- | --- |
| `build_phase_plan`, `build_assignment_plan` | Sealed study assignments, fixed labels and instructions, fixture content, study caps; phase builds also regenerate/verify the study and check actual prerequisite roots. |
| `prepare_live_root` | Every canonical study assignment and fixture; lane identity, count, index and caps derived from those entries. Core preparation revalidates the final pilot decision from its exports before any root or registry write. |
| `verify_live_root`, `run_live_phase` | Canonical study assignments, fixtures and study caps; retained lane plans, journals, attempts, budget history and study start claims. Run also requires user authorization naming the exact sealed plan and current prerequisite evidence. |
| `compatibility_evidence`, `smoke_evidence`, `check_phase_gates` | Actual archived observer results and tool/caps/catalog bindings. Smoke also checks its study registration, exact smoke assignment IDs, start claims, cleanups and approved failed-attempt amendments. |
| `consumed_attempts_in_root`, `prior_root_ledger`, `verify_consumed_ledger`; preparation's prior-root ingestion | Canonical study assignments before ingestion; durable journal starts and lane index checkpoints, study registration/path and start claims, supersession records and retained ledger-repair evidence. |
| `abandon_root` | Registered path and matching plan when present; actual lane journals and absence of all starts. A pending root whose plan was never written still requires journal inspection when its directory exists. |
| `record_amendment`, `amended_attempts` | Explicit approval and study identity; verified smoke attempts, failed transport/usage status and cleared cleanup debt. An amendment cannot make a valid or unstarted attempt a failed exclusion. |
| `reconcile_cleanup` | Verified root/start evidence and an actual environment check. Tests inject the environment check; this work never contacted a guest. |
| `repair_ledger`, `verify_root_ledger_repairs` | Canonical study bindings; approved committed corruption evidence, identity marker, unique single-bit reconstruction, durable journal attribution and exact repair/declaration bytes. |
| Resource proposal and caps freezing | Verified registered roots and archived usage/wall measurements; freezing requires the user's explicit approval of the exact proposed caps. |
| `export_live_review` | Canonical root/study assignment binding before writing; registered root path, study starts, consumed ledger, repair records and amendments; every normalized archived attempt and its transport evidence. |
| `registered_pressure_partitions`, `pressure_export_partitions` | Live study registry and every registered root's hash-bound lane plans, with every entry checked against the full canonical study assignment and primary sealed fixtures when historical labels are omitted. Excluded/unscored rows remain part of the population. |
| P packet production, `build_pilot_decision`, `validate_core_decision` | One shared verifier reopens the registered source root, re-derives every row with the scorer, compares the entire planned population, every row and archived attempt, declared exclusions and attempt hashes, then recomputes the review selection. Filtering happens afterwards. |
| Earlier-level review-plan authoring/verification and packet production | Full deterministic plan recomputation from the sealed study and protocol seed at the CLI and packet consumer. Live retention checks the study/seed and exact authorized plan hash; selection contents do not determine live trial execution. |
| Core decision execution binding | The offline-recomputed decision retained in the exact authorized plan, its study binding and eligible lanes; lane identity and core status independently come from canonical study assignments. Runtime does not rescore or import post-hoc modules. |
| Human review upload, resolution and endpoints | Exact packet/output identities and private bindings to the original attempt; schema and human/adjudication records; record-derived authorization truth. Any supplied structured score is checked by rescoring the attempt, including the existing explicit replay-score view. |
| Offline replay and summaries | Retained plan/fixture hashes and instructions; replay provenance is explicit and default live scoring cannot count it as a primary model observation. Replay and summary outputs are not live admission evidence. |

Whole arms in distinct roots can still produce distinct exports. A partial or
empty index from a larger source root is refused, even if the remaining arms are
internally complete. Legitimate declared exclusions, quarantined/unrun rows and
scoring errors remain representable when they match the primary root.

Human approvals and semantic labels remain explicit inputs. Content hashes do not
establish human authorship or reviewer independence; the existing upload validator
states that limitation.

## Changes and regressions

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

## Final offline validation

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

## Full re-derivation cost

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
