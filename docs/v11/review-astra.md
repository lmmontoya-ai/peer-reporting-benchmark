# Independent Astra review of peer reporting v1.1

Reviewed `v11/review-astra` at `83bbc1d63bd23500c449ae189adef9c816b26e44`, against
`main`, on 2026-10-05. The reviewed delta is 74 added files and 21,212 lines. I read
the specification first, then the implementation notes and the three W09 reviews.
This report makes no implementation changes.

## Verdict

**Not ready for the six compatibility calls, or for calibration, smoke, or
collection.** B1 permits a second execution of the same sealed assignments under
the same authorization, including compatibility assignments. M1 permits a genuine
protocol conflict to pass compatibility or disappear behind a hard stop. Before
behavioral calls, also fix the contradictory tool-delivery handling in M2. Before
collection, bind and validate the human review plan (M3), make export enforce the
authorization evidence that verification requires (M4), and resolve the smoke
recovery limitation in m2. The fixture construction and ordinary structured scoring
look sound; these findings concern execution identity, evidence integrity, and
review controls.

Counts: **1 blocker, 4 major, 2 minor, 1 note**. Findings below distinguish defects
from earlier accepted design decisions. In particular, B1 does not require copying
the study or creating a new study instance.

## Method and completed checks

- Created `.venv` with `uv venv .venv` and installed `.[dev]` with
  `uv pip install --python .venv/Scripts/python.exe -e ".[dev]"`. Python commands
  used `PYTHONUTF8=1`.
- `.venv/Scripts/python.exe -m pytest tests -q -m "not live and not integration"`:
  **1,742 passed, 1 skipped, 650.10 seconds**. The skip is the private P1 regression
  whose controller-directory environment variable is unset.
- `ruff check src tests`: **all checks passed**.
- The requested `git diff main..HEAD --` over `src/swarm_auth_bench/peer_reporting`,
  `runtime.py`, `events.py`, `long_events.py`, `model_catalog.py`, and `isolation.py`
  under `src/swarm_auth_bench/` is **empty**.
- Built and independently verified a real packaged study: **1,224 assignments,
  135 fixtures**, with counts 936 collection, 72 channel failure, 120 low effort,
  84 calibration, and 12 smoke. A separate grant-matching loop, not the generator's
  or verifier's authorization helper, agreed with all **222 recorded operations**.
  That loop checked the current fixtures, whose timestamps all use canonical UTC.
- Ran the new reproductions against the real bundle, real world, real templates,
  and real study. Only process/provider transport, version probing, and guest
  verification used the repository's offline fakes. Fault cases changed the
  indicated wire message or persistence boundary. Test authorization records were
  explicitly test-only. Counts called `live_model_call_starts` and observations
  called `live_model` below are the harness's bookkeeping, not actual inference.
- **No model or provider call, no live or integration test, and no VM start.**
  I did not access or copy from the private source repository. Scratch scripts are
  under `.local/`; scratch roots are under the system temporary directory. Neither
  is committed. Reproduction recipes and observed results are retained below.

Locations below use module names relative to
`src/swarm_auth_bench/peer_reporting_v11/`, unless another path is given.

## Blocker

### B1. Copying a live root defeats both consumption and authorization limits

**Locations:** `live.py:687` (`root_registration`), `live.py:952`
(`prior_root_ledger`), `live.py:1086` (registered path), `live.py:1453`
(root-local coordinator lock).

The registry records an absolute `root_path`, but running, verifying, and accepting
gate evidence identify a root by its plan hash alone. `root_registration` neither
takes the supplied root directory nor compares it with the registered path. Starts
and locks live inside the supplied root. A pre-run copy therefore has the same
nonce, plan hash, assignment IDs, and authorization identity, but a separate empty
journal and separate locks. The study's consumed-attempt checks do not discover the
other physical copy. Compatibility has the same problem without a study registry:
its new nonce distinguishes independently prepared roots, not copies of one root.

**Reproduced, end to end:**

1. Build the real study and complete compatibility using `Harness` and
   `qualifier_steps` from `tests/v11/live_fakes.py`, supplying `load_bundle()`.
2. Prepare a real 12-row smoke root `A`, then call `shutil.copytree(A, B)` before
   either root starts. Keep **one unchanged study directory**.
3. Create `auth = authorization(read_live_plan(A))`. Run `A`, then `B`, passing
   the same `auth`, study directory, caps, and compatibility root. Use
   `report_steps` for each packet.
4. Both runs return `complete`, each records 12 starts, and both report the same
   authorization hash. `verify_live_root(B, study_directory=study)` reports 12
   archived attempts. A collection plan using `B` as smoke evidence is accepted
   for all 1,128 assignments.
5. Separately, prepare one compatibility root, copy it before execution, and run
   both copies under the original authorization. Observed: `complete / 6` and
   `complete / 6`, with the same authorization hash.

Sequential runs suffice; concurrency is not required. Concurrent copies also use
different lock files, so the root-local locks cannot protect their shared logical
identity. I reproduced the sequential case, not a simultaneous two-process run.

**Spec:** section 10, consumed attempts, study instance, explicit authorization;
section 9, one sealed assignment per trial. This is a code defect. The accepted
R2-M3/R3-N3-d residual concerns separate copies of the **study ledger**. Here the
authoritative ledger was never copied, rebuilt, or edited. R3's registered-path
abandonment fix correctly checks this path in `abandon_root`; the other entry points
still omit it.

**Suggested fix:** validate physical root identity at run, verify, export, gate,
and prior-root ingestion. Use the registered resolved path or an explicit recorded
relocation, and make the logical root's lock/consumption claim authoritative outside
copyable per-root journals. Compatibility needs a comparable execution identity
binding. A consumed authorization/assignment claim should be durable before session
start. Add tests for a copied root against the original study and authorization,
including a stale copy supplied as a prior root.

## Major

### M1. Closing admission suppresses conflicting dynamic call IDs

**Locations:** `live_runtime.py:276` (early closed response),
`live_runtime.py:282` (attribution), `live_runtime.py:294` (duplicate conflict);
`live.py:318` (`stop_truncated`).

`_Controller.request` returns a successful transport response containing
`error: closed` before checking attribution, declaration, or conflicting reuse of
a transport call ID. Thus a real protocol failure arriving after `agent_finish`,
a cap, or a hard stop is treated as ordinary rejected work. Merely declining to
execute the call is insufficient: the failure must still invalidate the attempt
and hold new admission.

**Reproduced through the real JSON-RPC server-request handler:** subclass
`FakeTransport`. Immediately before forwarding `turn/completed`, take the raw
parameters of the earlier `read_channel` call, retain its `callId`, change
`arguments` to `{"after_event_id": null, "limit": 2}`, and invoke
`_server_request({"id": 900, "method": "item/tool/call", "params": changed})`.

- After all six qualification tools, including `agent_finish`: the conflicting
  request has `admitted: false`, returns `closed` with transport `success: true`,
  and records no infrastructure failure. Termination is `natural_end` and
  **qualification passes**.
- After a read, a 1,000-token usage notification, and setting the collection hard
  stop event: the same conflict records no failure. The behavioral check classifies
  it as **`stop_truncation`**, with bounded settlement. It therefore produces no
  execution-failure hold for a later run.

The round-3 packet-mismatch fix works for its own event path; this is a different
early return. The reproduction used actual request dispatch, not a fabricated
observer-result dictionary.

**Spec:** sections 7 and 10, unchanged transport idempotency and "a stop never
masks a real failure." Code defect.

**Suggested fix:** separate request validity from permission to dispatch. Check
scope, declared tool, and conflicting reuse of a known call ID even after admission
closes. Valid late work can receive `closed`; contradictory protocol evidence must
remain an execution failure. Test these cases after finish, after each cap, and
after hard stop, including subsequent admission.

### M2. Contradictory tool-delivery receipts still pass behavioral validity

**Locations:** `live_runtime.py:340` (`_tool_receipt`), `live_runtime.py:470`
(unmatched receipts), `live_runtime.py:638` and `:665` (drains);
`live.py:270` (`_transport_checks`).

Exact receipt matching detects a different tool result, but handles it by returning
`False` and keeping the raw event in `pending_receipts`. That list is retried when
responses are sent, then silently left unresolved. No final check distinguishes an
out-of-order receipt from a receipt that contradicts a response already sent.
Behavioral validity does not require the matched receipts that qualification uses.

**Reproduced:** in `FakeTransport._send`, intercept each server response with
`contentItems`, and replace its text with
`{"status":"error","error":"not_found"}` before forwarding it to the fake
model's waiter. Keep IDs and other fields intact. The fake emits the ordinary
completed-tool receipt using the response it actually received. The controller
therefore retains its original durable result and sees a different delivered result.

With the real world and six-tool script, the baseline has six receipts. The changed
wire has **zero confirmed receipts**, no infrastructure failures, `natural_end`,
and **`evaluate_transport(...)["passed"] == True`**. Qualification correctly fails
in this particular reproduction; behavioral trials do not. Altering only the
completed-tool notification produces the same acceptance.

This can change an L3 observer's evidence or its understanding of whether a report
was stored while the trial remains eligible. It also lets admission continue after
positive evidence of a transport mismatch. The finding is about an explicit
contradiction, not a claim that every absent acknowledgement after truncation must
be treated as failure.

**Spec:** sections 5.5, 7, and 10, evidence retrieval, tool transport, and holding on
real transport/protocol failure. Code defect.

**Suggested fix:** preserve support for receipt-before-response ordering, but
reconcile pending receipts at close. An attributable completed item with content,
success, tool identity, or status inconsistent with the prepared response must fail
the execution check. Specify separately how missing receipts are handled. Add a
behavioral-phase fault test that also checks that later assignments do not start.

### M3. The human review plan is neither committed by the live freeze nor validated at packet export

**Locations:** `collection.py:154` (manifest), `live.py:449` (plan assembly),
`live.py:1324` (phase gates), `cli.py:83` and `:101` (seed and plan creation),
`review_plan.py:257` and `:275` (verification versus `plan_rows`),
`review.py:723` and `:736` (packet selection).

The selection algorithm reads only manifest labels, but no live plan binds its
result or seed. Smoke and collection can start without any review plan. The CLI
can generate another plan under any integer seed after outcomes exist; verification
recomputes against the seed in that same supplied plan, not the specified frozen
seed or a precommitted plan. Moreover, `write_review_packets` calls only `plan_rows`,
which checks the seal, kind, and rubric version, not the selection rules.

**Reproduced:**

- Completed fake smoke and built a 1,128-call collection plan without creating a
  review plan. The gate evidence contains qualification and smoke only. A later
  collection run admitted an assignment, also without a review plan.
- After the smoke results existed, generated plans with seeds 1 and 2. Both
  `verify_review_plan` calls return `[]`; the sets of full-scope assignment IDs
  have a symmetric difference of **214**.
- Took a default valid plan, replaced `rows` with `[]`, and resealed it with the
  ordinary `seal` helper. `verify_review_plan` correctly reports
  `plan rows differs from the recomputed plan`. Nevertheless,
  `write_review_packets(collection_export, bad_plan, fresh_output)` succeeds:
  **zero packets**, all **1,128** assignments `not_in_review_plan`, no exception.
  That export had one completed collection attempt with outputs.

A checksum is not precommitment. This permits selecting or omitting human review
after seeing the outcomes, undermining the free-text endpoint comparisons across
models, prompts, and levels. It also contradicts the rubric's fixed seed 20261005.

**Spec:** section 10 step 4, section 11, and `docs/v11/review-rubric.md` section 4.
Code defect; the specified selection algorithm itself is sound.

**Suggested fix:** build and validate the review plan before the specified freeze;
bind its hash and seed in the frozen study execution record and require that binding
at smoke/collection admission and review export. Recompute selection when accepting
a plan, including row coverage and second-review flags. Custom seeds require a
declared pre-freeze protocol change. Keep the accepted distinction between frozen
plan data and post-hoc review implementation hashes; this does not require importing
the review code into the trial runtime.

### M4. Export scores evidence whose retained execution authorization is missing

**Locations:** `live_review.py:336` through `:340` (export checks),
`live_review.py:147` and `:181` (normalized eligibility),
`live.py:1143` (`_verify_authorizations`).

`verify_live_root` checks every journaled authorization against its retained record.
`export_live_review` independently checks registration and the consumption ledger,
then calls `inspect_live_root`, omitting authorization verification. It can therefore
produce verified-looking, scoreable observations from a root the verifier rejects.

**Reproduced in the collection phase:** complete one fake collection attempt and
soft-stop before the next admission. Rename the root's
`authorizations/<hash>.json` to a non-JSON suffix, leaving all journals and attempt
archives untouched.

```text
verify_live_root: EvidenceError, journaled authorization ... is not retained
export_live_review(..., scorer=score_trial): succeeds
  assignments: 1128; attempts: 1; verified_model_observations: 1
  lane_errors: {}; scored: true
exported score eligibility:
  eligible: true; reason: null; resolves_assignment: true
```

The same discrepancy was reproduced for the complete 12-attempt smoke root. This
does not demonstrate an original runtime start without authorization. It demonstrates
that the evidence-processing path admits an observation after the required proof of
authorization is lost, with no quarantine or warning.

**Spec:** section 10, explicit authorization and retained execution evidence;
sections 8 and 12, valid observations and denominators. Code defect. It extends the
earlier authorization-journaling fix rather than repeating its original finding.

**Suggested fix:** share the authorization-evidence check between verification and
export. Fail or quarantine affected rows before scoring; retain the authorization
binding in the export. Preserve partial-export behavior for unrelated valid rows
where that can be established. Add export tests for missing, corrupt, and mismatched
authorization records, not just the existing verify test.

## Minor

### m1. An initial reviewer can adjudicate their own disagreement and bypass second review

**Locations:** `rubric.py:169` through `:182`; `review.py:485` passes packet
reviews into this resolver.

Initial final reviews must have distinct reviewer IDs, but the adjudicator need not
be distinct from them. The adjudication branch also returns before enforcing the
required second review or requiring any initial review.

**Reproduced:** using complete valid records from `tests/v11/review_helpers.review`:

```python
a = review(reviewer="human-A", claims=[("op-01", True)])
b = review(reviewer="human-B", concern=True, uncertainty="decidable")
resolve_output([a, b], targets=["op-01"], second_review=True, adjudication=a)
resolve_output([a], targets=["op-01"], second_review=True, adjudication=a)
resolve_output([], targets=["op-01"], second_review=True, adjudication=a)
```

All three return `status: final`, `resolution: adjudicated`. The first permits
self-adjudication of a dispute. The others remove the independent second-review
requirement. Human endpoints trust that final resolution, including for a truth
conflict.

**Spec:** section 11 and review rubric sections 4.1 and 4.2, specifically a third
human resolving a dispute from both initial reviews. Code defect.

**Suggested fix:** require a distinct adjudicator and the appropriate retained
initial reviews. Define the separate first-review truth-conflict route explicitly;
it should not allow the initial reviewer to act as their own independent check.
Add negative tests for the three cases above. This is identity consistency, not
proof that a reviewer is human.

### m2. An amended smoke archive failure can hold every subsequent resume permanently

**Locations:** `phase.py:260` (`cleanup_debt`), `phase.py:531` through `:545`
(`_ledger_hold`), `live.py:838` (`record_amendment`), and the recovery promise at
`live.py:1441`.

Amendments exempt accepted failed attempts from retained failure and unresolved
usage holds, but cleanup debt can only be cleared by an `attempt_archived` record
with `cleanup_confirmed: true`. A crash or archival failure after runtime shutdown
but before that record loses the cleanup proof. Reconciliation changes the start to
`incomplete_interrupted`; it never records recovered cleanup. The amendment is
accepted, yet every later run is stopped by the same debt. There is no supported
cleanup-reconciliation action.

**Reproduced with a real smoke plan:** temporarily replaced `_PhaseRun._archive`
with a function raising `OSError` at entry, after the actual fake observer had
finished and closed. Restored the method before recovery. This injects the durable
state of a failure before archival; it is not an OS process-kill test.

```text
initial run: held, 1 consumed start, attempt_unarchived
record_amendment(accept_failed_smoke_attempts): accepted
resume 1: held, 0 new runtimes, retained_unreconciled_start/unsettled reservation
resume 2: held, 0 new runtimes, cleanup_unreconciled
resume 3: held, 0 new runtimes, cleanup_unreconciled
```

This goes beyond the one extra zero-call reconciliation run described in the
round-3 review's open item 3. A replacement smoke root also cannot satisfy the
current exact-smoke-rows gate once it excludes the consumed assignment. The
available fallback is a new study and new smoke, not the documented same-root
recovery.

**Spec:** section 10, interrupted smoke, cleanup holds, and amendments. There is a
recovery gap in the code and an overbroad recovery promise in the documentation.
The hold itself is appropriately conservative; this is an availability defect,
not evidence that an unsafe attempt should be admitted.

**Suggested fix:** provide an auditable recovery path that proves the old runtime
is gone and durably reconciles cleanup, while preserving consumption and analysis
exclusion. Alternatively, explicitly reject amendments that cannot enable the
documented recovery and document the required new-study path. Do not simply ignore
cleanup debt whenever an amendment exists. Test multiple resumes after the injected
archive boundary, not only already-archived failures with clean shutdown.

## Note

### N1. The implementation notes still describe the replaced assignment schedule

**Locations:** `docs/v11/implementation-notes.md:33` through `:37`, compared with
`collection.py:41` and spec section 9.

The notes say assignments are grouped by fixture and placed in rounds using
`floor(i * R / c)`. The implementation now pairs violation and twin in blocks and
uses the specified Latin-square schedule. **Verified by reading both and building
the real study.** The code follows the revised specification; the prose describes
the pre-fix algorithm. Replace this paragraph with the current block and round rule
so an analyst does not reconstruct the wrong planned order.

## Earlier fixes re-verified

These conclusions combine code inspection with the passing named regression suites;
the new reproductions above cover the remaining gaps.

| Earlier finding or decision | Current evidence and conclusion |
|---|---|
| R1 M4: study-identifying wire schema IDs | `bundle.py` strips schema IDs and scans all serialized wire specs. The real bundle has exactly six tools. Schema/bundle tests pass, including banned words and neutral `$id` rejection. Fixed. |
| R1 M5: one state for multiple operations/actors | The report description explicitly applies authorization state and certainty to every listed target. Coverage and compound false-alarm tests match the recorded decision. Fixed by that decision. |
| R1 M2 / R2-m1: F applicability and mixed summaries | F hit, false-alarm, uncertainty, and hit-calibration endpoints are not applicable. Every structured and human summary grouping includes `arm`; no pooled overall is emitted. Fixed. |
| R1 M3, m8-m10: concern, duplicates, rejected posts, unknown IDs | Concern prefixes use the documented ASCII boundary. Stored-public evidence is required, transport duplicates and saved report IDs are distinguished, and unknown-only operation IDs fall back to actor/stem coverage. Structured and scorer regressions pass. Fixed under the mechanical scoring contract. |
| R1 M6 / R2-m4: order and lane drift | `collection._interleave` forms paired blocks with Latin-square rounds. The global dispatcher uses the lowest eligible planned position and the per-effort round barrier. Order, barrier, and unequal-speed tests pass. The accepted coarse drift bound and need for fixture-matched stopped-run analysis remain. |
| R1 M7-M8: frozen study inputs and generator parameters | Plan construction checks study caps, tools, protocol and full fixture verification. Configured near-miss lists, distractors, and slot offsets reach generation; invalid/colliding offsets are refused. Fixed. |
| R1 B1, R2-M3, R3-M1: consumed roots and abandonment | Ordinary replacement-root overlap, supersession, pending-root abandonment, wrong-path abandonment, missing finalized plans, and contradictory abandoned starts are covered and pass. Rebuilt studies have fresh nonces. The distinct copied-live-root route in B1 remains. |
| R3-m1: compatibility nonce | Independently prepared compatibility roots have different hashes and need different authorizations. The regression passes. B1 shows copying an existing prepared root still defeats the intended execution identity. |
| R1 m1-m3 / R2-M1: holds and usage | There is no await between a returned failed observer result and its provisional hold. Settlement conflicts and retained ledger stops hold. Bounded settlement requires observed integer usage and no missing-total notice, and retains its accounting label. Regression tests pass. |
| R2-M2 / R3-m2: stops | Soft stops let active work finish; hard stops truncate; clean stop truncation alone does not become a retained execution failure. Packet mismatch, native-tool/protocol, disconnect, and storage faults remain failures after stops in the existing tests. Dynamic conflicting IDs and contradictory receipts are the additional gaps in M1-M2. |
| R1 m4: freeze | Live-affecting code changes refuse admission. The five accepted post-hoc modules are excluded from the run gate but remain reported by verification. The dependency guard test and changed-scorer test pass. M3 concerns missing frozen review-plan data, not this accepted code-hash exception. |
| R1 m5-m7: world mode, authorization, smoke coverage | Mode and quota come from durable world state, starts journal authorization hashes, and the smoke gate requires exactly the study's smoke rows. Every collection lane must retain passed, unamended smoke evidence. All tests pass; M4 identifies export's missing authorization check. |
| Previously pending resources and human review | Both modules now exist. Resource formula, rounding, settled-only measurement selection, lane headroom, and proposal/freezing tests pass. Default review selection sizes and paired/stratified choices match the rubric. M3 and m1 are integration/validation gaps in those new review controls. |

## Other checks and limits

**Stimuli and measurement.** Reviewed schemas, world state transitions, incident
validation and templates, ladder construction and independent authorization,
prompts, config, collection, structured scoring, summaries, review packets and
endpoints. The real fixture set preserves eight peers, the two-operation contrast,
authorized companions, exact-grant twins, raw versus fetched record placement, and
the intended control distinctions. Public identifiers do not encode variant or
level; differences that reveal evidence difficulty are the intended manipulation.
No additional construct label or model/effort identifier leak was found in the
observer's packet, instructions, or real wire tool specifications. Prompt conditions
use the specified text and budget suffix. The unavailable store keeps valid report
attempts without storing private reports. I found no further structured scoring
counterexample under the specified operation/actor coverage contract.

The default review sampler covers all public posts, control reports, all F outputs,
and the paired stratified samples. Its default scopes are 204 full, 567 reports and
posts, and 357 posts; 336 trials receive second review. Selection reads no output.
Human endpoints retain unknowns outside their scope, and model labels remain
provisional. Masked reviewer packets and HTML omit controller bindings and withheld
assignment labels, while controller outputs retain them separately. HTML text is
escaped. These properties do not resolve M3 or m1.

**Execution and durability.** Reviewed `live.py`, `phase.py`, `lanes.py`,
`live_runtime.py`, `live_review.py`, the replay runner, resources, CLI, and their
unchanged P1 dependencies. Same-directory locks, durable start-before-session
ordering, checkpointed journals, orphan reservation handling, archive/index
reconciliation, and refusal to reopen a consumed start work on the tested paths.
Recovery boundaries between registration, plan write, supersession, and finalization
were inspected. Pending registrations fail closed. Ordinary resumed roots do not
rerun started assignments. Actual model, effort, world mode, instructions, tools,
and caps are bound at the intended layers. The new failures are the exceptions
described above, not a claim that the whole state machine is unprotected.

Lock execution was tested on Windows by the suite; Linux locking was reviewed in
the unchanged helper, not executed on a Linux guest. I did not claim exhaustive
process-kill or power-loss coverage of every write boundary. The explicit archive
fault in m2 and existing crash/reconciliation tests are the exercised boundaries.

**Security and privacy.** Scanned all added source, tests, and docs for credential
patterns, private keys, email addresses, and absolute home/research paths. Found no
secret or private machine path in the committed delta. Reviewed identifier
validation, `safe_child` containment including resolved paths, fresh-directory
creation, output overwrite handling, JSON parsing, and HTML escaping. No new unsafe
deserialization or model-controlled filesystem escape was found. Raw research
exports intentionally contain private controller bindings and registry paths;
only the dedicated `reviewer/` packet directory is suitable for blinded reviewers.
Nothing from the scratch roots or their machine-specific paths is included here.

**Test quality.** The suite is broad and catches many meaningful mutations, but
passing it is not sufficient for release. The missing cases are specific:

- Root-copy tests copy the study or independently prepare new roots, not a prepared
  live root used against the same study and authorization.
- Stop-failure tests cover several notifications, but not conflicting dynamic
  requests after admission closes. Tool receipt checks lack a behavioral test for
  explicit mismatched delivered content.
- The missing-authorization regression exercises `verify_live_root`, not export
  and scoring of the same damaged evidence.
- Review-plan verification and packet export are tested separately. Packet export
  does not invoke the verifier, and no test crosses that boundary with a resealed
  bad selection. Rubric tests use a distinct adjudicator only in the positive case.
- Amendment tests mainly use archived failures that already have cleanup proof.
  They do not exercise repeated resume after loss of the archive itself.

Several live-layer tests use a separately implemented fake world and schema bundle.
Those are useful for orchestration isolation, but assertions against that same fake
do not verify the production stimulus. The new reproductions deliberately use the
real bundle and world. Timing-driven tests passed here; event-controlled ordering
would be preferable for any new race regression. Keep the independent authorization
verifier independent as required by the spec; share execution-evidence validators
where their current duplication lets verify and export disagree.
