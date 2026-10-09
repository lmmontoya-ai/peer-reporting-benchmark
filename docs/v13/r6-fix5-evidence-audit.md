# R6-fix5 evidence gate audit

The sealed study's assignment table and fixtures are the primary plan inputs. A
content seal detects changes relative to a retained reference; it does not make
an independently resealed plan, export, selection, or decision authoritative.

No plan, root, attempt, export, or earlier-level review schema gained a field.
The explicit earlier-archive regression compares plan, prepared-root and export
bytes with and without the added predicates, freezing nonce and journal time.
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
| `registered_pressure_partitions`, `pressure_export_partitions` | Live study registry and every registered root's hash-bound lane plans, with every entry checked against the full canonical study assignment. Excluded/unscored rows remain part of the population. |
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
