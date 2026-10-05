# Astra review of revision 3, round 2

Reviewed `v11/review-r3b` at `b22199e4cb2e2dcc9e3fc084804b53bd9ce3af50`
on 2026-10-05. Diff: `1ebbe31..HEAD`. Review only; no implementation changes.

## Verdict

**GO for the fresh compatibility run and the fresh, 126-row extension root.**
The real study, gates, authorization validation, six-lane execution, export and
scoring all worked with the offline transport. The original M1 and M2 unsafe
classifications are fixed. The original M3 sequence now preserves both the pause
and the refusal count. Excluded refusals no longer enter summary counts.

This is not approval of unattended replacement-root recovery. If a run leaves an
unarchived start, keep it held and investigate before authorizing a successor.
R1 below reproduces the disclosed pause-write failure across sequential roots.
The current root stops safely; a replacement can lose the pause. Running only
one root at a time does not itself close that recovery gap.

Open counts: **0 blockers, 1 residual major, 2 minor, 1 operational note**.
R1 is the remaining crash case of M3, already disclosed by the fixer. The two
minor findings are newly reported here; R2 also reproduces before this diff.
None requires blocking the two planned fresh starts under the hold-on-failure
operating rule above. No live environment or actual authorization file was
examined, and this review does not authorize either live run.

## Finding status

| Earlier finding | Status | Confirmation evidence |
| --- | --- | --- |
| B1, revision 2 compatibility rejected | Resolved by decision | Spec 9 now requires fresh revision 3 compatibility. A fresh real-bundle root qualified all six lanes with the fake transport. Old evidence is not reused. |
| M1, stale exemption after shutdown errors | Fixed | Independent real-adapter probes inject `streamDisconnected` or `willRetry: true` during `close()`. Both return `infrastructure_incomplete`, no overload exemption, and a provider-error failure. The regression cases also check retained holds and ineligible exports. |
| M2, terminal error hidden by stop/completion | Fixed | Both error codes, each followed by a hard stop or `completed`, produce the explicit non-retryable-error failure. The four archive/resume/export regressions pass. |
| M3, successor resets pause/window | Partly fixed | With the original arm closed, successive roots select xhigh, low, then remaining xhigh extension rows. Starts occur at 0, 600 and 1,200 seconds; counts are 1, 2 and 3. A fourth root starts nothing. The unpersisted-pause crash remains R1. |
| M4, impossible revision 2 study transition | Resolved by decision | A new production study verifies with 1,350 assignments and 137 fixtures. Its extension plan selects exactly 84 xhigh and 42 low rows. It has no revision 2 prior root. |
| m1, excluded refusals inflate summary | Fixed | Export of one refusal and otherwise unrun rows retains one archive and one exclusion, but has `summary.trial_count == 0` and empty summary cells. The mixed valid/refusal regression retains only the valid row in every grouping. |

The M1, M2, M3 and m1 regression reproductions are in
`tests/v11/test_live_astra_r3.py`; the complete offline suite reruns them.
Independent probes used the production bundle/world for error classification
and the complete live-plan rehearsal. Small pause-lineage probes used the fake
study verifier and explicitly embedded `closed_arms: ["calibration"]`, so they
did not rely on the old test's now-forbidden third `calibration` root.

## Open findings

### R1. Major, residual M3: a pause-write failure does not hold a successor root

Locations: `src/swarm_auth_bench/peer_reporting_v11/phase.py:1002`,
`phase.py:1011`; `src/swarm_auth_bench/peer_reporting_v11/live.py:1027`,
`live.py:2093`. Spec 10, study-wide provider pauses and holding on unreconciled
starts/storage failures.

Reproduction, using one coordinator at a time:

1. Build root A selecting `calibration_extension_xhigh`. Its first fake session
   returns `overload_steps()`. Patch only `live.record_study_pause` to raise
   `OSError` before writing the record.
2. A holds with one consumed, unarchived start and no study pause. Its durable
   `usage_settled` journal already records `settlement_reason:
   "provider_unavailable"` and a 75,000-token reservation bound.
3. Resume A. It creates no runtime and holds on `retained_unreconciled_start`.
4. Build B in the same study, selecting `calibration_extension_low`, with A as
   its prior root. Supply B's test-only authorization and the same `PauseClock`.
5. B starts at **0 seconds** after A's refusal, with A's start correctly retained
   in the consumed ledger. B's own refusal records **window_count 1**, not 2.

This is more than losing an unrecorded observation: the refusal's settlement
reason survived, but successor admission does not reconcile it with the missing
pause. No duplicate assignment or simultaneous root is needed. The new study
record correctly protects crashes *after* it is written, including an
unarchived attempt; the gap is before that write.

Suggested fix: before successor admission, reconcile durable refusal evidence
with study pause records, or retain a study-wide hold for unresolved predecessor
starts until their pause/usage state is reconciled. Preserve consumption. Test
the crash on both sides of the pause write with a different selected-arm root.
Until then, do not use a new root to work around this hold. This limits recovery,
not the safety of initially starting the fresh root, which stops on the failure.

### R2. Minor: a clean overload first reported at shutdown is still failed

Locations: `src/swarm_auth_bench/peer_reporting_v11/live_runtime.py:761`,
`live_runtime.py:767`, `live_runtime.py:848`. Spec 10, overload eligibility
decided after shutdown and the last event drain.

Deliver the correct packet, then return a `failed` turn whose error is
`{"codexErrorInfo": "serverOverloaded", "message": "capacity refused"}`.
Deliver its only scoped `error` notification, with `willRetry: false`, from the
fake runtime's `close()` callback:

```python
runtime._sessions[runtime.thread_id].queue.put_nowait({
    "method": "error",
    "params": {
        "threadId": runtime.thread_id, "turnId": runtime.turn_id,
        "error": {"codexErrorInfo": "serverOverloaded", "message": "capacity refused"},
        "willRetry": False,
    },
})
```

Observed with the production adapter/world: no requests/output, clean runtime
shutdown, reconciled world, one overload notification, but
`termination_kind == "infrastructure_incomplete"` and `provider_overload is None`.
The first pass recorded `INVALID_TURN_RESULT` before the notification arrived;
the final pass runs only for an already non-null overload candidate. The same
result occurs with a null turn error. The ordinary notification-before-completion
control remains `provider_unavailable`; a duplicate late overload preserves the
exemption and appears in its evidence.

This synthetic ordering fails closed and can unnecessarily hold a run. It does
not admit invalid behavioral data. A separate process using `1ebbe31` reproduces
the same early rejection, so it is an incomplete implementation of the stated
final-drain rule, not a regression introduced by these fixes.

Suggested fix: defer failed-turn classification until final reconciliation;
evaluate the overload predicate unconditionally then, preserving unrelated
failures. Add a first-notification-at-final-drain case alongside the existing
late-contradiction tests.

### R3. Minor: the study pause verifier ignores its recorded root path

Location: `src/swarm_auth_bench/peer_reporting_v11/live.py:1031`. Spec 10, study
instance/root identity and study-level pause provenance.

After recording a valid pause, change only the study record's `root_path` to
`roots/not-the-producing-root` and re-seal it. `verify_live_root` accepts it.
Changing its study hash, plan hash, phase or lane instead is rejected. The
verifier compares the plan, phase and lane with the start claim, but never
compares the two `root_path` values or checks that path against the registration.

This is a provenance-validation omission, not a demonstrated duplicate-start or
pause-bypass path. Plan identity and admission protections still apply, and
ordinary writes supply the correct path. Suggested fix: require the pause's
root path to equal the claimed and registered path. Add a re-sealed mismatch
test. This need not delay the fresh starts.

### N1. Concurrent roots remain outside the safe operating plan

Locations: `src/swarm_auth_bench/peer_reporting_v11/lanes.py:454`,
`lanes.py:459`, `lanes.py:466`. Spec 10, study-wide refusal window.

The load/count/persist sequence has no study-wide transaction. Two independent
coordinators can read the same preceding events and both persist the same next
count. This is a code-inspection finding; I did not launch simultaneous roots.
The user explicitly excludes that operation, so it is nonblocking here. A
single coordinator serializes archive and pause recording without an `await`;
six concurrent lanes inside that coordinator are supported. If parallel roots
are ever permitted, use a study-level admission lock or atomic counter update
and test the interleaving. A recovered `willRetry: true` error is also not an
open defect: spec 10's terminal-error rule explicitly concerns non-retryable
errors, and recovery probes remain valid.

## Live-plan confirmation

The intended sequence is executable under the current code:

1. Build a new compatibility root under revision 3 with approved frozen caps.
   Validate/retain the user's authorization against its exact plan hash, six
   calls, caps hash and resolved root path. Run compatibility and verify **all
   six** model/effort lanes. A refusal is not a passing qualification.
2. Build and verify a new revision 3 study with the frozen calibration caps.
   Keep revision 2 evidence historical and separate. Do not pass its roots as
   `--prior-root`, and do not select `calibration`.
3. Build `STUDY/roots/extension-r3` with `--phase calibration`,
   `--arm calibration_extension_xhigh --arm calibration_extension_low`, the
   same caps, and the fresh `--compatibility` root. The expected call counts are
   28 for each xhigh lane and 14 for each low lane, totaling 126. Calibration
   requires compatibility for all six lanes; it does not require a smoke root
   or collection review plan. The new study's initial consumed ledger is empty.
4. Validate/retain a separate authorization of this exact calibration plan,
   caps and 126-call maximum, with the cutoff at least trial-wall-plus-drain
   before the forced deadline. Run the `calibration` command with `--study`,
   `--caps`, `--authorization` and the fresh `--compatibility` root. Keep any
   incomplete-start or storage-failure hold for investigation as specified in
   R1. Ordinary STOP recovery uses the same root; no consumed assignment reruns.
5. Verify the root with its study and run `export-review ROOT --study STUDY
   --output EXPORT` with default scoring enabled. Inspect archived counts,
   eligibility, exclusions and summary counts. Resource proposals must continue
   to ignore bounded/refusal charges. Subsequent smoke/collection still need
   their own caps decisions, gates and authorizations.

These are subcommands of `python -m swarm_auth_bench.peer_reporting_v11`.
The rehearsal used the production builders, default full study verifier,
fixtures, world, coordinator, export adapter and scorer. Only process launch,
provider wire, environment attestation and version probe were faked. Its caps
were explicitly test-only frozen caps: 60,000-token stop, 75,000 reservation,
180-second trial wall, 10-second drain, concurrency six, and 36,000-second phase
walls. Actual approved caps were not supplied to this review.

Observed results:

| Stage | Result |
| --- | --- |
| Compatibility | Complete; 6 starts; all 6 lanes qualified. |
| Study | Valid; no errors; 1,350 assignments and 137 fixtures. |
| Extension | 126 planned, started and valid outcomes; 126 study start claims; peak active attempts 6; no holds. |
| Scored export | 126 archived rows; 126 eligible scores; summary count 126; exclusion count 0; no lane errors. |
| Exhausted successor selection | Rejected because all selected extension assignments are consumed. |

The closed-arm guard rejects an omitted selection, explicit `calibration`, and
mixed original/extension selection against the real manifest. Removing
`protocol`, removing `closed_arms`, or clearing that list and re-sealing the
manifest fails the production study verifier before planning. Most small fake
manifests omit `protocol` and deliberately inject `fake_study_verifier`; their
ability to select the old arm is not a production CLI bypass. No supported CLI
bypass was found. Wrong authorization plan hashes, caps hashes and call counts
were rejected, as was a compatibility authorization for another root path.

## Additional checks

- Scope filtering remained correct in independent real-adapter probes: errors
  naming another thread or turn do not poison a clean completion or overload.
  Missing scope is not treated as an attributed trial error. Duplicate terminal
  notifications yield one terminal-error failure; duplicate retry notifications
  followed by recovery remain a valid natural end. Neither duplicate overload
  notifications nor refresh creates a second refusal attempt.
- Study pause writes use the existing exclusive, synced JSON creation helper.
  The archive/journal recovery tests cover a persisted pause without a completed
  archive and an archived pause without its journal entry. Claimed starts stay
  consumed; a fourth root held before any start can be abandoned without removing
  the three preceding pauses. Registered root paths remain enforced separately
  from the R3 metadata omission.
- The summary filter applies to every `excluded_from_analysis` row, including
  amendment exclusions. It retains row-level evidence and scores plus the new
  `analysis_exclusion_count`; it changes neither charging nor known-outcome rate
  denominators.

Validation:

- `.venv/Scripts/python.exe -m pytest tests -q -m "not live and not integration"`:
  **1,877 passed, 1 skipped in 1,771.32 seconds (29:31)**. The private P1
  regression is skipped because `PEER_V1_CONTROLLER_DIR` is unset.
- `.venv/Scripts/ruff.exe check src tests`: **all checks passed**.
- `git diff --check 1ebbe31..HEAD`: clean.
- The independent 21-case adapter probe, four-root pause probe, crash-successor
  probe, real-manifest guard checks and full 126-row rehearsal produced the
  results reported above. They performed no provider calls.

P1 is byte-for-byte unchanged in this range: the Git tree object for
`src/swarm_auth_bench/peer_reporting` is
`1bb77c5090236ca5360f0d82d3b2e2b6905c964a` at both endpoints. No shared runtime
module changed. Separate Python processes loading `1ebbe31` and the reviewed
HEAD returned identical complete `_binding_fields(load_bundle(), models)`
dictionaries, including every catalog field, client `0.158.0`, schema and
descriptor hash. Representative unchanged identifiers:

| Identifier | Value |
| --- | --- |
| Tool manifest | `4233cabb4a6d46a0e6065688bfa94a22381f89bf39108007a0331d13ba685f0d` |
| Wire specifications | `3f216d9e721021fd7145b8315c4db62b830ed7371d6e9ec95bf88f6a351e3664` |
| `LIVE_VERSION` | `peer-reporting-v11-live-phases-v1` |
| `ADAPTER_VERSION` | `peer-reporting-v11-live-runtime-v1` |

Created `.venv` with `uv venv .venv` and installed `.[dev]` through its Python.
Python ran with `PYTHONUTF8=1`. Scratch scripts, public baseline source extracted
from this worktree's Git history, fake runtime homes, roots and result JSON are
under the system temp directory, in `astra-r3b-review-20261005`. No model/provider
calls, live/integration tests, VM operations, private controller reads or copies
from the private source repository were performed. Only this report is committed.
