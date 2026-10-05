# Astra review of specification revision 3

Reviewed `v11/review-r3` at `ee25703a75b87036ebbe014725b2daa9f230ed84` on
2026-10-05. Scope: `git diff 6c21468^..HEAD`, whose base is
`e0cf4e79c591e454e32b6c65283a03dce14778eb`. This report changes no implementation.

## Verdict

**Do not start the calibration extension yet.** Revision 3 rejects the retained
revision 2 compatibility evidence, despite unchanged compatibility identifiers
(B1). The documented transition from the held calibration study is also not
implemented (M4). After those startup problems are resolved, error reconciliation
and pause inheritance still need fixes before unattended execution (M1-M3).

Counts: **1 blocker, 4 major, 1 minor, 0 notes**.

Locations below are relative to `src/swarm_auth_bench/peer_reporting_v11/`, unless
an explicit repository path is given. Findings distinguish observed results from
suggested changes. The historical guest results in the review brief were not
inspected or rerun.

## Blocker

### B1. Revision 3 invalidates all retained revision 2 compatibility lanes

Locations: `__init__.py:4`, `ladder.py:358`, `ladder.py:602`, `live.py:1636`,
`live.py:1716`; the contrary reuse claim is in
`docs/v11/implementation-notes.md:679`.

Bumping `SPECIFICATION_REVISION` changes the provenance of every regenerated
fixture. `verify_fixture` compares the stored provenance with a fixture regenerated
using the current revision. `verify_live_root` applies that check to compatibility
fixtures before accepting any lane. Consequently, compatibility evidence generated
under revision 2 fails with `provenance differs from the specified fixture`.
`compatibility_evidence` catches the failure and returns no qualified lanes.

The manifest, wire specs, catalogs, client, `LIVE_VERSION`, and `ADAPTER_VERSION`
really are unchanged. Those comparisons are insufficient to establish retained
compatibility reuse because fixture verification rejects the root first.

Reproduction, using only public repository code and the offline transport:

1. Extract `git archive 6c21468^ src` into a system-temp directory. In a separate
   Python process, put its `src` first on `PYTHONPATH`.
2. Load the real `load_bundle()`, build and prepare a compatibility root, and run
   all six lanes with `Harness`, `qualifier_steps`, and `run_phase` from
   `tests/v11/live_fakes.py`. Pass the real bundle to `run_phase`. Supply the
   usual test-only authorization naming this compatibility root's resolved path.
3. The old process returns `complete`, with all six lanes qualified. Its fixture
   has `provenance.specification_revision == 2`.
4. In a fresh process using current code, call
   `verify_live_root(root, bundle=load_bundle())`. It raises:

   ```text
   EvidenceError: fixture f41efc2203f342105914b018494b7da1bd749804637108c05d6d56018fdf66e7
   failed verification: ['provenance differs from the specified fixture']
   ```

5. `compatibility_evidence([root], bundle=load_bundle())` returns an empty evidence
   mapping and the same verification failure. No current calibration gate can
   obtain any of the six qualifications from that root.

Spec: section 10, compatibility qualification and binding; revision 3's stated
reuse of the six qualified lanes in the implementation notes.

Suggested fix: validate retained fixtures against their recorded, supported
revision while keeping their original bytes, seals, and provenance intact.
Explicitly establish that the revision 2 compatibility stimulus and tool contract
remain acceptable. Add an old-code/new-code fixture and compatibility-root
regression. Do not repair archived evidence by rewriting its provenance or seals.
If the chosen policy instead requires new qualification calls, declare that
change before execution; the current claim that the existing calls remain usable
is false.

## Major

### M1. Shutdown-time errors leave a stale overload exemption in force

Locations: `live_runtime.py:515`, `live_runtime.py:752`, `live_runtime.py:779`,
`live_runtime.py:839`.

The adapter evaluates `provider_overload` before `runtime.close()`. It subsequently
drains more events, including scoped `error` notifications. Those notifications
append to `controller.provider_errors` without adding an infrastructure failure.
The final check tests failures, output, requests, and cleanup, but never recomputes
the error-code/retry predicate. Its copied `provider_overload.error_notifications`
therefore omits contradictory events that are present in the archived raw log.

Reproduction:

1. Build a real revision 3 study and qualify a current compatibility root with the
   fake transport. Prepare a serial calibration root selecting
   `calibration_extension_low`.
2. Run its first assignment with `overload_steps()`, which delivers the packet,
   emits non-retryable `serverOverloaded`, and completes the turn as `failed`.
3. Give `Harness` an `observe` callback. At `kind == "close"`, while the runtime
   still has its session, append this notification with `queue.put_nowait`:

   ```python
   {
       "method": "error",
       "params": {
           "threadId": runtime.thread_id,
           "turnId": runtime.turn_id,
           "error": {"message": "another transport failure",
                     "codexErrorInfo": "streamDisconnected"},
           "willRetry": False,
       },
   }
   ```

4. Use `PauseClock`, and create `root/STOP` on its first pause poll to bound the
   reproduction. Observed: one consumed start; `classification` and
   `termination_kind` both `provider_unavailable`; no infrastructure failure;
   75,000 tokens settled as `bounded_by_reservation`; `holds == ["soft_stop"]`.
   Both error notifications appear in `raw_events`, but the overload evidence
   lists only the first.
5. Repeating with a late `serverOverloaded` whose `willRetry` is `True` gives the
   same exemption, despite the rule that the last error must announce no retry.
6. Remove `STOP` and resume the mixed-error root with ordinary `report_steps`.
   After the restored pause expires, a second assignment starts. No retained
   execution-failure hold appears. A stop at that session bounds the reproduction.

Control: deliver the same mixed-code error before the initial drain instead.
The real adapter records `turn result identity, status, or error invalid`, usage
stays unresolved, and the run holds for an execution-check failure.

Spec: section 10, provider overload and transport failures. The exception requires
an otherwise clean turn. Timing within the close cannot turn mixed errors into a
capacity-only refusal.

Suggested fix: derive the final classification from all reconciled events after
shutdown and the last drain. Recompute both eligibility for the exception and its
recorded evidence. Test mixed codes and retry announcements at both drain
boundaries, and test subsequent admission and export.

### M2. A non-retryable error disappears behind interruption or successful completion

Locations: `live_runtime.py:515`, `live_runtime.py:608`, `live_runtime.py:755`;
`live.py:367` and `live.py:474` consume the resulting classification.

Outside the overload exception, the fallback checks the `TurnResult` status and
error, but never rejects the recorded error notifications themselves. An explicit
non-retryable error can therefore disappear if a hard stop wins the completion
race, or if transport evidence contains a contradictory successful completion.
This is separate from M1: these errors arrive before the first drain, and the
initial overload predicate already returns `None`.

Reproduction with the real bundle, world, and sealed calibration root:

1. Feed `FakeTransport` the following steps, substituting a callback that touches
   the root's `HARD_STOP` file for `request_hard_stop`:

   ```python
   [
       ("usage", 1000),
       ("raw", "error", {
           "error": {"codexErrorInfo": "serverOverloaded",
                     "message": "capacity refused"},
           "willRetry": False,
       }),
       ("sleep", 0.02),
       ("stall", request_hard_stop),
   ]
   ```

2. The actual monitor interrupts the turn. The attempt archives as
   `stop_truncation`, with `infrastructure_failures == []` and a clean-close
   reservation bound of 75,000 tokens. Its only failed transport check is
   `valid_close`. The run's only hold is `hard_stop`.
3. Remove `HARD_STOP` and resume. A second assignment starts; only the new
   deliberately requested soft stop holds that run. The original provider error
   creates no retained execution-failure hold.
4. A direct real-adapter reproduction replacing the code with
   `streamDisconnected` also produces `stop_truncation` and no failure.
5. A separate full-root reproduction replaces the final two steps with
   `("end", "completed", None)`. Despite the non-retryable error, the attempt
   becomes `natural_end`, passes transport validity, and settles 1,000 tokens.
   Default scoring/export reports `eligible: true`, `resolves_assignment: true`.
   The tested row was a twin, and `false_alarm` was scored `false`, not null.

This is a gap in the claimed "stop never masks a real failure" protection. The
successful-completion variant also converts contradictory transport evidence
into a resolved behavioral observation. The tests that pair an error with a
`failed` completion do not exercise either path.

Spec: section 10, stops, provider overload, and holding on transport/protocol
failure. A transient error that retries and recovers needs an explicit
policy; neither reproduction announces a retry.

Suggested fix: reconcile terminal error notifications with the final status and
stop evidence. A non-retryable error outside the fully validated overload
exception must create an execution failure even when the final status is
`interrupted` or `completed`. Preserve that failure at archive, resume, and export.
Add ordering tests for error-before-stop and contradictory terminal statuses.

### M3. A successor root loses the active pause and the 60-minute count

Locations: `live.py:1990`, `phase.py:1334`, `live.py:1295`.

`run_live_phase` creates a fresh `ProviderPause`. `run_lanes` restores only the
current root's lane journals. The mandatory prior-root ledger retains consumed
attempt IDs but no provider pause history. Selecting a different calibration arm
and superseding the old root thus resets both the active pause and its window
count, even though the old roots remain registered and are explicitly supplied.

Reproduction with one unchanged real study, serial dispatch, and one shared
`PauseClock`:

1. Prepare root A selecting `calibration_extension_xhigh`. Its first assignment
   uses `overload_steps()`. On the first pause poll, create A's `STOP`; the clock
   advances 60 seconds and the run returns held only by `soft_stop`.
2. Prepare root B selecting `calibration_extension_low`, with A as `prior_roots`.
   Run it at the current clock time, using the same overload and stop recipe.
3. Prepare root C selecting `calibration`, with A and B as prior roots. Repeat.
4. Observed times, relative to A's refusal:

   | Root | Session start | Its recorded resume time | Window count | Limit hold |
   | --- | ---: | ---: | ---: | --- |
   | A | 0 s | 600 s | 1 | false |
   | B | 60 s | 660 s | 1 | false |
   | C | 120 s | 720 s | 1 | false |

Each root starts exactly one distinct assignment. B and C correctly carry one
and two consumed starts in their sealed ledgers. Nevertheless, both start before
A's pause expires, and the third refusal within 120 seconds still records count 1.
No `provider_unavailable_limit` hold appears. No copy, ledger edit, or repeated
assignment is involved.

Spec: section 10, the 10-minute admission pause and third refusal within 60
minutes; section 9, calibration arm selection and replacement roots. The rule is
not stated as resetting on a new root revision or arm selection.

Suggested fix: carry validated pause history across the study's root lineage,
using an authoritative study-level record or importing/reconciling the named
predecessors before any dispatch. Include predecessor events in active-pause and
window-limit checks. Test a new selected-arm root during a pause and after two
recent refusals, as well as a same-root restart.

### M4. The documented extension of the held revision 2 study cannot be built

Locations: `live.py:734`, `live.py:1863`, `live.py:1305`, `collection.py:212`;
`docs/v11/implementation-notes.md:672` promises the unsupported transition.

The arm-selection test starts all three arms in a study already built under
revision 3. It does not model the actual starting point: a sealed revision 2
study and its held calibration root. Full study verification now expects 1,350
assignments and 137 fixtures. The old study has 1,224 and 135, so a new root
cannot be built against it. Building a fresh study changes its manifest identity,
and the old calibration root is then rejected as belonging to another study.
No lineage extension or migration operation bridges these cases.

Reproduction:

1. From the extracted pre-revision `src` used in B1, call the real `build_study`
   with `load_protocol()`, `load_all_templates()`, and test frozen caps. Register
   a calibration root against that study using its real rows and fixtures. It
   need not start anything to reproduce the failure.
2. Under current code, call `build_phase_plan("calibration", ..., arms=
   ["calibration_extension_xhigh", "calibration_extension_low"],
   study_directory=old_study, prior_roots=[old_calibration], ...)`.
3. It raises `EvidenceError: study verification failed`, listing mismatches in
   assignments, counts, fixtures, protocol, seal, split counts, and total trials.
4. Build a fresh revision 3 study with the same caps and inputs. Repeat the build
   with that study and the old calibration root as prior evidence. It raises
   `ValueError: prior root ... belongs to another phase or study`.
5. Independently compare the old assignments with the old arms in the new study:
   all 1,224 assignment IDs are identical. A rebuild therefore does not eliminate
   the need to decide how earlier consumed starts carry forward.

This failure is independent of B1: the reproduction supplies newly qualified
revision 3 compatibility evidence and still cannot perform the documented
transition. The existing source/registry checks are correctly refusing mismatched
identity; weakening those checks would reintroduce the earlier consumption bug.

Spec: sections 9 and 10, extension without repeating original calibration,
consumed attempts, study instance, and full study verification. The implementation
notes explicitly say the extension names the held root as `--prior-root` and a
later root can finish its original arm.

Suggested fix: implement and document an explicit study-lineage transition that
preserves original evidence and durable consumption claims while adding the new
arms. Alternatively, obtain an explicit protocol decision to use a distinct,
extension-only study and document its different treatment of historical results
and any future original-arm run. Test the actual revision 2 to revision 3 boundary,
not just replacement roots within a newly built revision 3 study.

## Minor

### m1. Excluded provider refusals still enter the analytical summary's trial counts

Locations: `live_review.py:405`, `score.py:241`, `score.py:256`.

Export correctly flags provider refusals as excluded and ineligible, but passes
every non-null score to `summarize`, including excluded rows. The summary therefore
counts these rows as trials and applicable unknown outcomes. The known-outcome
rate denominator is correct: the refusal is not counted as a negative. The
reported analytical sample size and null counts nevertheless include a row
explicitly excluded from analysis.

Reproduction: export a real calibration root with one archived, clean
`provider_unavailable` attempt and all other rows unrun, passing
`scorer=score_trial, summarize=summarize`. The tested refusal had a 1,000-token
usage notification before overload and correctly settled at the 75,000-token
reservation. The resulting index contains:

```text
row.status = archived
row.excluded_from_analysis = true
analysis_exclusions = [that assignment ID]
score.eligibility.eligible = false
score.eligibility.resolves_assignment = false
summary.trial_count = 1
summary arm cell: trial_count = 1
focal_hit: applicable = 1, null = 1, true = 0, false = 0, rate = null
```

Spec: section 10, behavioral ineligibility and analysis exclusion; revision 3's
export contract. This affects descriptive summary counts, not false-negative
rates or resource charging.

Suggested fix: filter excluded rows before producing analytical summaries, while
retaining their archives and a separate accounting count in the export. Add a
scored-export assertion; the existing overload export test supplies no scorer or
summarizer.

## Checks and evidence

Created `.venv` with `uv venv .venv`, installed `.[dev]` using
`uv pip install --python .venv/Scripts/python.exe -e ".[dev]"`, and used
`PYTHONUTF8=1` for Python. All scratch code, extracted historical source, mock
runtime homes, and evidence roots are under the system temp directory. No model
or provider calls, live/integration tests, or VM operations were performed. No
files were read or copied from the private source repository.

Validation results:

- `.venv/Scripts/python.exe -m pytest tests -q -m "not live and not integration"`:
  **1,860 passed, 1 skipped in 1,744.17 s (29:04)**. The skip is the private P1
  regression, whose `PEER_V1_CONTROLLER_DIR` is unset.
- `.venv/Scripts/ruff.exe check src tests`: all checks passed.
- `git diff main..HEAD -- src/swarm_auth_bench/peer_reporting`: empty.
- Built and verified a real packaged study: 1,350 assignments, 137 fixtures;
  collection split 1,128, calibration split 210, smoke split 12;
  `verify_study` returns `valid: true`, with no errors.
- Both packaged and documented protocol copies contain revision 3's 84-row xhigh
  extension and 42-row low extension. Original-arm assignment IDs are unchanged.
- A separately created revision 2 compatibility root qualified all six lanes under
  the old code, then failed current verification as documented in B1. This was a
  local fake-transport experiment, not an inspection of guest evidence.

I compared `_binding_fields(load_bundle(), models)` in separate processes using
`6c21468^` and `HEAD`. Every field was equal, including every catalog field, client
version, schema version, descriptor hash, and the following identifiers:

| Identifier | Unchanged value |
| --- | --- |
| Tool manifest hash | `4233cabb4a6d46a0e6065688bfa94a22381f89bf39108007a0331d13ba685f0d` |
| Wire tool specs hash | `3f216d9e721021fd7145b8315c4db62b830ed7371d6e9ec95bf88f6a351e3664` |
| Luna catalog SHA-256 | `9a3bd7b0899b2bfcbe3b9f0749241e29f63c67fee2f6275c71d28d37b268f193` |
| Sol catalog SHA-256 | `76f3d97946113579fb919821c1ef738cc75d658069c289f248ac809bbb697b34` |
| Astra catalog SHA-256 | `d44e2c0896abafa01ca5f862c934b392aef238b547e79e649fe53d4f90fbf589` |
| `LIVE_VERSION` | `peer-reporting-v11-live-phases-v1` |
| `ADAPTER_VERSION` | `peer-reporting-v11-live-runtime-v1` |

### What checked out

The following conclusions are bounded by the failures above.

- Normal failed-turn overload handling works. A clean refusal after exact packet
  delivery, before any request or output, is consumed once, fails `valid_close`,
  settles at its reservation without observed usage, and pauses admission. A
  prior 1,000-token usage observation remains distinct from the 75,000-token
  bound. The resource-proposal filter explicitly ignores `provider_unavailable`.
- Errors delivered before the initial drain with a failed turn are
  rejected when codes are mixed, retry is announced, packet delivery is missing,
  or a request/output already occurred. Additional real-adapter probes rejected
  a partial assistant delta, malformed/missing usage, a native tool item during
  shutdown, and a late packet mismatch. Those last two correctly revoke an
  initially observed overload exemption. M1 identifies the error-notification
  branch that does not do so.
- Within the same root, pause restoration, the third-refusal hold, recovery after
  a missing pause journal write, and post-preflight refusal/requeue are covered
  by the offline suite. The pause is sealed before its journal record and is
  recovered from the archive. A crash before an archive remains a consumed,
  incomplete start that holds. The synchronous archive and post-preflight checks
  preserve the admission boundary. Requeued assignments do not gain a second
  consumed start, and dispatch retains the existing effort-specific round barrier.
- Lane time uses `LaneClock` throughout an open run, so ordinary pause waiting
  consumes an already-started lane's wall allowance. Time between runs is excluded.
  The coordinator continues checking stops and deadlines during a pause.
- Overload export retains the archive, sets `eligible: false` and
  `excluded_from_analysis: true`, and records an exclusion reason without
  quarantine. The scorer gives null endpoints and does not resolve the
  assignment; review packets skip excluded rows. Known-outcome rate denominators
  omit the refusal. The descriptive count exception is m1.
- The smoke gate rejects a provider refusal unless an approved amendment accepts
  that failed attempt. The rule requiring some valid smoke evidence for every
  needed lane remains in force, as does cleanup reconciliation before amendment.
- Within a revision 3 study, selected arms are sealed, sorted, limited to
  calibration, checked against lane arms at verification/export, and filtered
  after the full study check. Unknown, duplicate, empty, or exhausted selections
  are refused. Low-effort selection requires the low compatibility lanes.
  Prior-root consumption, registered paths, and study-level start claims remain
  enforced. M3 concerns pause state, not repeated assignment execution.
- The xhigh extension has six rounds of 14 rows. Independently checked each
  round: seven neutral and seven discouraged rows, model counts 4/5/5, and each
  violation immediately followed by its twin with the same model, prompt, level,
  type, and round. Reversing assignment input order reproduces the same schedule
  using the packaged protocol. The six-pair two-prompt offset cycle is used.
- Both new calibration ambiguity fixtures, for `budget-transfer` and
  `firewall-change`, pass the production independent fixture verifier and have no
  focal operation. The full study verifier checks all 137 fixtures and rebuilds
  the manifest and order. Extension fixture selection uses the first calibration
  near-miss type at L2-L4 and reuses the intended violation/twin fixtures.
- The real review plan still verifies with 1,128 first-review rows and 336
  second-review rows; calibration extension rows do not enter its selection.
  Its fixed seed, retained collection binding, and packet-export recomputation
  remain intact.
- The earlier regression tests cover root copies and stale start claims,
  post-close conflicting/undeclared/unattributable requests, contradictory tool
  receipts, export authorization quarantine, and explicit cleanup reconciliation.
  Revision 3 does not alter the P1 implementation. M2 concerns error
  notifications and completion ordering, beyond the earlier stop-failure cases.

The missing release tests are cross-version evidence reuse, a real old-study
transition, terminal error evidence at both drains and around stops, pause
inheritance across selected-arm roots, and scored export of excluded refusals.
Passing tests built entirely under revision 3 cannot establish the first two.
