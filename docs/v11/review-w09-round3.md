# W09 review, round 3: confirmation pass after FXR2

Branch `v11/wp9-review3` at `aa01569`, which is `v11/integration` with FXR2-misc, FXR2-live
and the round-2 spec decisions merged. Review date 2026-10-05. I made no model or provider
call, ran no live or integration test and started no VM. Every scenario below ran offline,
either with the real bundle and packaged protocol or with the fakes in
`tests/v11/live_fakes.py`. Scratch scripts live under `.local/` and are not committed.

## Verdict

Compatibility is ready. None of the compatibility-bound identifiers changed in this round.
The tools, wire specs, catalogs, client, `LIVE_VERSION` and `ADAPTER_VERSION` are identical
at `af580cb` (the round-2 state) and at `HEAD`. Against `8988994` only the three tool hashes
differ, and that difference is FX1 (`6235c4e`), which round 2 reviewed. The six-call path
works end to end offline with the packaged protocol and the real bundle, and a compatibility
root built by the round-2 code still verifies and still counts as gate evidence under `HEAD`.
I recommend one cheap change before the real compatibility root is built. Compatibility roots
carry no root nonce, so one authorization can run any number of identical compatibility roots
(R3-m1). Adding the nonce changes no binding. It is not a blocker.

Smoke, calibration and collection are close. All three round-2 majors are fixed, and so are
the four round-2 minors. One new major sits in the code that fixed R2-m2.
`abandon_root` accepts a finalized root that started attempts whenever `--root` points at a
directory without a sealed plan, for example a typo. That abandonment then lets a new root
rerun the consumed assignments and lets a failed smoke attempt through the collection gate
(R3-M1). Abandonment is the documented recovery path for exactly the phase where this
matters, so fix it before smoke. With R3-M1 fixed and the study rebuilt (it needs the nonce
and the `round` field), calibration and smoke are ready. Collection additionally needs the
two known pending items, the resource-proposal tool and the human review plan. None of the
author's open items blocks a phase (see below).

Counts of new findings: 0 blockers, 1 major, 2 minor, 5 notes. Every round-2 finding is
fixed. R2-M3 has a documented residual for deliberate copies, and R2-m4 is fixed as
specified but bounds drift only coarsely.

## Checks re-run

- `pytest tests -q -m "not live and not integration"`: 1,631 passed, 1 skipped (the private
  P1 regression), 452 seconds.
- `ruff check src tests`: all checks passed. `ruff check scripts` passes too.
- P1 integrity: `git diff main..HEAD` over `src/swarm_auth_bench/peer_reporting`,
  `runtime.py`, `events.py`, `long_events.py`, `model_catalog.py` and `isolation.py` is empty.
  All 61 files that differ from `main` are additions. Since round 2 the branch added
  `docs/v11/review-w09-round2.md` and `tests/v11/test_live_r2.py`.
- Privacy: I scanned the 61 added files for drive and home paths, email addresses, key and
  token patterns, and the private script name from round 1. The only hit is the round-1
  report quoting its own N9 note. No machine path, credential, email or private data appears.
- An environment observation, not a finding. When I first put scratch roots under the
  worktree on `D:\`, 4 of 15 repeated compatibility runs held. The cause was Windows
  `PermissionError` on `os.replace` of `budget-ledger.json` and of the world state. Under
  `%TEMP%`, as pytest uses, 0 of 15 held. Every one of those failures held admission and
  left the lane unqualified, so storage faults fail closed. Live phases run on the Linux
  guest, so this does not reach them.

## Round-2 findings

| ID | Status | Evidence |
|---|---|---|
| R2-M1 | Fixed | Four smoke variants on the luna xhigh L1 row: a usage notice without a total, no usage notice at all, a total followed by a notice without one, and a raw-response notice without a response ID. All four end `natural_end`, settle `unresolved` with reason `usage_notification_without_total` or `usage_never_observed`, and hold `unknown_final_usage`. A trial that observed 1,500 tokens and then hit the trial wall settles `bounded_by_reservation` at 75,000, and the run completes. |
| R2-M2 | Fixed | My round-2 scenario with a 3-second trial wall. One lane stalls and touches `root/STOP`. The run holds `soft_stop` after 6 starts. The stalled attempt ends `per_trial_limit`, passes, settles bounded and records no collection stop reason. Resuming completes all 12, and the collection gate accepts the root. A hard stop after usage was observed classifies the attempt `stop_truncation`, holds only `hard_stop` and resumes cleanly. A deadline truncation behaves the same way. A ten-hour pause between runs costs no lane time (`test_a_resume_after_a_long_pause_does_not_exhaust_the_lane_wall`). |
| R2-M3 | Fixed, with a documented residual and a new route (R3-M1) | Copying the study without `live-roots/` now gives the copy's root another plan hash. The original study's collection gate refuses that root ("not registered in the supplied study directory"), and `run` without `--study` refuses. A rebuild seals a new nonce, both instances verify, and old smoke roots fail the new instance's gate. The residual: a copy is a self-consistent ledger of its own. Smoke in the copy reran the 3 consumed assignments, and the copy's own collection gate passed (N3-d). |
| R2-m1 | Fixed | `GROUPING_KEYS[0]` is `arm`, every grouping starts with `arm`, and `summarize` emits no `overall`. `test_three_l1_replays_never_pool_collection_low_effort_or_channel_failure` replays my round-2 case. |
| R2-m2 | Fixed, but see R3-M1 | A crash between registration and the plan write leaves a pending root. Abandoning it lets `smoke-v2` build and run (`test_a_crash_between_registration_and_plan_write_is_abandoned_not_wedged`). A pending root never runs, because `run` needs a finalized registration. |
| R2-m3 | Fixed | `companion_slot_offset: -2` and `pressure_slot_offsets: [5, 1]` are refused with the order messages. Offsets 0, `[0, 5]`, `[3, 3]` and `[1, 5]` with companion 1 are refused as collisions. |
| R2-m4 | Fixed as specified; the bound is coarse | The barrier follows spec 9, and verify-time order is unchanged. On the real collection order at 25% of astra xhigh with 6 slots, luna has started 136 rows (168 without the barrier), sol 126 (126) and astra 84. With 3 slots luna has 136 (149). The simulated makespan is the same with and without the barrier. Spec 12 now compares a stopped collection only on fixture-matched cells. See N3-a. |
| N-a | Fixed | `resource_observations` carry `usage_settlement`, and the lane ledger summary splits settled tokens by label. The resource-proposal tool is still pending. |
| N-b | Fixed by decision | Spec 11 now reviews every stored observer public post in any trial. |
| N-c | Fixed | A lane's token target is the reservation times (planned rows + 1), and `validate_token_headroom` refuses a tighter plan at creation. In the test, three 80,000-token overshoots on a three-row lane still admit every row. |
| N-d | Fixed | The L0 statement now reads "...every operation listed in scope...". The string in `ladder.py` equals the spec 5.4 text, and `scope` lists operations. |

## Compatibility path, offline end to end

I used the packaged protocol (`peer-reporting-v1.1`), the real bundle and the fake transport.

- `build_compatibility_plan` returns 6 lanes in `lane_order()`, one call each, all in round
  0, each with a token target of 150,000 (two reservations). The plan has no
  consumed-attempt ledger and no behavioral observation, and it counts in no behavioral
  denominator. The fixture is token-issuance, smoke split, seed 1100, L1 violation, and it
  verifies.
- `prepare_live_root` refuses a compatibility root with a study directory.
- `run_live_phase` refuses each of these before creating any runtime: no authorization, an
  empty record, an authorization for another plan, an edited sealed authorization, a
  "proposed" approval, caps with an invalid status, and caps that differ from the sealed
  caps. No runtime was created and no `authorizations/` directory was written.
- The authorized run creates 6 runtimes, one per lane, with exactly one manifest probe each
  and a peak of 6 active attempts. It completes with all 6 lanes qualified and settled usage
  of 4,321 per lane. `verify_live_root` reports 6 qualified lanes, no unreconciled start and
  no implementation change, and `compatibility_evidence` returns 6 lanes. A second run starts
  nothing.
- Qualification rules. Skipping `report_incident` or `agent_finish` classifies every probe
  `protocol_incompatibility_or_check_incomplete`. Omitting usage classifies them
  `usage_unavailable` with an `unresolved` settlement. No lane qualifies, and the run holds.
- Identifiers at three commits:

| Identifier | `8988994` | `af580cb` | `HEAD` |
|---|---|---|---|
| `LIVE_VERSION` | `peer-reporting-v11-live-phases-v1` | same | same |
| `ADAPTER_VERSION` | `peer-reporting-v11-live-runtime-v1` | same | same |
| client | `0.158.0` | same | same |
| tool schema version | `peer-reporting-v11-tools-v1` | same | same |
| catalogs (luna, sol, astra) | `9a3bd7b0…`, `76f3d979…`, `d44e2c08…` | same | same |
| qualifier block, criteria, version, fixture spec | `9e2719de…`, `246f98f7…`, `-qualifier-v1`, token-issuance/1100/L1 | same | same |
| `tool_manifest_hash` | `5cf7ce1f…` | `4233cabb…` | `4233cabb…` |
| `tool_descriptors_hash` | `a47dba9a…` | `1dcd101d…` | `1dcd101d…` |
| `wire_tool_specs_hash` | `e02966ea…` | `3f216d9e…` | `3f216d9e…` |

  The tool hashes moved once, in FX1 (`6235c4e`): `$id` stripping and the wire scan in
  `bundle.py`, and the report description in `schemas.py`. `git diff af580cb..HEAD` over
  `bundle.py`, `schemas.py`, `live_runtime.py`, `world.py`, `prompts.py`, `protocol.json`,
  `incidents/`, `runtime.py`, `model_catalog.py` and the P1 package is empty.
- A compatibility root built and run by the `af580cb` code on the fakes verifies under
  `HEAD`, and `compatibility_evidence` still returns all 6 lanes. The new `round` and
  token-headroom checks run only when a lane plan is created. Compatibility evidence from
  before this round would have survived it. No real compatibility root exists yet.

## Author's open items

1. The study and roots must be rebuilt. Confirmed, and it fails closed. A manifest without
   `instance_nonce` fails `verify_study`, and rows without `round` fail
   `validate_assignment_rows`. A fresh build verifies, and calibration and smoke plans build
   from it with the real bundle. This is an offline step before calibration, not a blocker.
2. A hard stop before the first usage notification still holds. Confirmed. The attempt is
   classified `stop_truncation`, but it settles `unresolved` (`usage_never_observed`), and
   the next run holds on the retained reservation with zero calls. In smoke, one amendment
   clears it, and the same root then completed in a single run (11 new runtimes, gate
   passed with the attempt listed as accepted). In collection, a new root naming the prior
   root clears it. Holding is the right call under R2-M1. A deadline truncation should be
   rare, because the authorization puts the cutoff a full trial wall plus drain before the
   deadline. Not a blocker. Operators should use `STOP` and keep `HARD_STOP` for
   emergencies.
3. A failed, unreconciled smoke attempt holds once more after its amendment. I did not
   reproduce it, because that needs a crash mid-attempt. The code agrees with the author:
   `accept_lane_report` always holds on active reservations, and the next run reconciles
   them. The cost is one zero-call run under another authorization. Not a blocker.
4. A deliberate copy of the study with its registry and roots cannot be detected.
   Confirmed, and the gap is a little wider. A copy without `live-roots/` is also
   self-consistent inside itself (N3-d). With "move, never copy" documented, this is not a
   blocker. R3-M1 is the more likely accident.
5. The resource-proposal tool (N11) is still pending. It blocks freezing collection caps
   after calibration, as known. N3-e lists what the barrier changes for it.

## Severity scale

The scale is the same as rounds 1 and 2. A blocker breaks a release rule in a phase that has
not run yet, on a path the project documents. A major finding can distort the sample, the
spend record or a sealed gate. A minor finding is a real defect with limited reach. A note
records a design risk or a documentation gap.

## Major

### R3-M1. `abandon_root` abandons a started root when `--root` has no sealed plan

- Code. `live.py:750-765` in `abandon_root`. When `root/live-plan.json` does not exist, the
  `else` branch at `live.py:761` sets `started = set(_journal_starts(root))` for whatever
  directory was passed. `live.py:766` refuses a finalized root only when no root is given.
  The CLI exposes the same path (`cli.py:81`, `abandon-root STUDY --plan-hash H --root DIR`).
- What is wrong. For a finalized root, abandonment must show that its journals have no
  start. If `--root` points at a directory without a sealed plan, such as a mistyped path,
  a path that does not exist, an empty directory or the wrong root, the glob finds no
  journal, and the code seals an abandonment with `root_journals_checked: true`. That branch
  takes no coordinator or lane lock and skips the supersession check, so a running or
  superseded root can be abandoned the same way.
- Scenario. `smoke-v1` holds after 3 starts, one of them a failed astra L1 attempt
  (`execution_check_failure`). `abandon_root(study, v1_hash, root=base / "smoke-v1-typo")`
  succeeds with `prior_state: "finalized"` and `root_journals_checked: true`. A new smoke
  plan then needs no prior root and has 12 calls. It ran `complete` and reran all 3 consumed
  assignments, including the failed one. A collection plan built with `smoke-v2` as its
  smoke evidence passed the gate with 12 attempt hashes and no accepted failures. The CLI
  with a wrong `--root` path exits 0 with the same record. `verify` of `smoke-v1` reports
  state `abandoned` and raises nothing.
- Spec. 9 ("Each assignment runs once"). 10, consumed attempts. 10, study instance: only "a
  pending root, or a finalized root whose journals show no start, can be abandoned". 10,
  smoke: a failed smoke attempt blocks collection until an amendment.
  `implementation-notes.md:356-358` says a finalized root's locks are taken and that "a
  started or superseded root is never abandoned".
- Fix. For a finalized registration, require `root/live-plan.json` with a seal hash equal
  to `plan_hash`, then take the locks and check starts and supersession as the existing
  branch does. Refuse anything else. A pending root can never have run, so it needs no
  journal check, and the glob fallback can go. As a second line of defense, have
  `verify_live_root` and `export_live_review` fail when an abandoned root's directory shows
  a journaled start. Record the lane journal hashes in the abandonment record, too. Add
  tests with a wrong path, an empty directory and another root's directory, through both the
  API and the CLI.

## Minor

### R3-m1. One authorization runs any number of identical compatibility roots

- Code. `live.py:1040-1042` seals `root_instance_nonce` only when the plan has a
  consumed-attempt ledger, so compatibility roots get none. `build_compatibility_plan`
  (`live.py:498`) is deterministic, and `validate_authorization` (`lanes.py:196`) binds an
  authorization to `live_plan_hash`.
- Scenario. I built `compat-a` and `compat-b` from revision `compat-v1`. Both have the same
  plan hash. One authorization for that plan ran both: 12 model sessions, both roots
  `complete`, both runs journaling the same authorization hash. The authorization record
  says `maximum_live_calls: 6`.
- Spec. 10: every live phase needs a user authorization that names the exact plan and its
  call count.
- Fix. Seal `root_instance_nonce` in every root, compatibility included. It changes no
  value in `_binding_fields`, so it costs nothing in compatibility evidence. Do it before
  building the real compatibility root. Behavioral roots already have the nonce, so their
  authorizations are bound to one root.

### R3-m2. A hard stop exempts a packet-delivery mismatch from the execution check

- Code. `STOP_TRUNCATION_REQUIRED` (`live.py:204-206`) omits `initial_receipt_exact`, and
  `stop_truncated` (`live.py:317`) relies on it. `live_runtime.py:661-663` adds no "initial
  exposure unverified" failure when the boundary is a forced truncation, and
  `live_runtime.py:447` ignores a user-message item whose text differs from the packet
  without recording anything.
- What is wrong. The runtime cannot tell "truncated before the packet echo" from "echoed a
  different packet". The second case is a transport failure, and a hard stop or the deadline
  turns it into a `stop_truncation`.
- Scenario. A fake transport echoes the luna xhigh user message with one extra trailing
  space. Without a stop, the attempt fails `initial_receipt_exact` and the run holds
  `execution_check_failure`. With `HARD_STOP` touched before the first tool call, the same
  attempt is classified `stop_truncation`, the run holds only `hard_stop`, and the resume
  completes with 11 new runtimes. The truncated attempt is ineligible either way. A
  systematic mismatch would fail the next attempt, so the failure is delayed, not lost.
- Spec. 10: a stop truncation is not an execution failure only when the attempt was
  otherwise clean.
- Fix. Record a user-message item whose text differs from the packet as an infrastructure
  failure in the runtime. Or require, for `stop_truncation`, that exposure was confirmed
  exactly or that no user-message item arrived at all. Add the scenario above as a test.

## Notes

- N3-a. The round barrier bounds drift only coarsely. The collection order has 9 rounds with
  37 to 39 rows per lane and round, so "r + 2" lets a lane run up to about 78 rows ahead
  (`phase.py:970-980`, `collection.py:100`). At 25% of astra xhigh, luna leads 136 to 84.
  Spec 12's fixture-matched rule covers the analysis. If closer pairing across models
  matters for a stopped collection, use a finer unit, such as the block within a round, or
  cap the row lead directly. Calibration rounds are 0, 3 and 6 (`collection.py:69`), so in
  calibration the barrier acts as a strict per-round barrier. That costs nothing, because
  the slowest lane is never blocked.
- N3-b. The collection gate passes a smoke root with zero valid attempts if amendments accept
  every failure (`live.py:1268` counts `valid + accepted`). Spec 10 leaves this to the user,
  but then smoke shows nothing about the lanes it was meant to prove. Consider requiring at
  least one valid smoke attempt per lane. Otherwise, say in spec 10 that an amendment
  accepting every attempt of a lane leaves that lane unproven.
- N3-c. Spec 10 does not say that a smoke attempt truncated by a hard stop or the deadline
  needs an amendment before collection. The stops bullet (`spec.md:546-551`) says such a
  truncation "does not hold later runs", and the smoke bullet (`spec.md:565-568`) says a
  stopped smoke run resumes. Only `implementation-notes.md:298-300` says the gate then needs
  an amendment. Add that sentence to spec 10 and recommend the soft stop during smoke.
- N3-d. Two copy and rebuild residuals. A study copied without `live-roots/` is a complete
  ledger of its own: smoke in the copy reran the original's 3 consumed assignments, and the
  copy's own collection gate passed. A rebuilt instance also has the same assignment and
  attempt IDs as the old one, because the nonce is not part of `assignment_identity`. Both
  are documented or accepted. Since every export carries the nonce and the registry
  listing, have the final collection receipt list them, and have the release audit confirm
  that no other export exists with the same study seal or the same assignment IDs.
- N3-e. Input for the pending resource-proposal tool. The barrier paces every lane of an
  effort at the slowest lane's speed, so every lane's wall now runs about as long as the
  slowest lane's. `lane_wall_seconds` is one value per phase, so size it from the slowest
  lane, with margin. A lane-wall stop truncates active attempts with reason
  `collection_wall_limit`, which counts as an execution failure (only a hard stop and the
  deadline are exempt), so an undersized wall ends the root. Calibration also runs under the
  barrier, so derive timing from per-attempt elapsed seconds, not from lane durations.
  Ignore bounded totals, as spec 10 already says.
