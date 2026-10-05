# Peer reporting v1.1: implementation notes

Each work package records its decisions here. The specification is
[spec.md](spec.md).

## WP6: instructions, contract, sealed study, and W08

`prompts.build_instructions` renders the section 6 condition text, appends P1's
exact budget suffix, and then appends the discouraged paragraph when requested.
WP1 passes the caps record's `trial` object. With no policy, the suffix is P1's
unfrozen-candidate notice. Neither fixture labels nor execution labels enter
the visible instructions.

The packaged `protocol.json` is a byte-identical copy of the approved contract.
`config.validate_protocol` expands the supported fixture descriptions and
explicit smoke cells independently of the ladder, then checks each declared
arm count and the total. Calibration's three L2 near-miss pairs count as six
fixtures; L3 and L4 use WP5's first assigned calibration type.

`collection.build_study` requires WP1's frozen caps record and writes a fresh
directory containing 135 sealed fixture files and `collection-manifest.json`.
Each build seals a fresh random `instance_nonce` into the manifest (W09 R2-M3),
so a rebuild from the same inputs is a new study instance with another seal;
`verify_study` recomputes the manifest under the recorded nonce.
The manifest uses WP1's fixture path/content-hash index and assignment row
fields. It archives the protocol, caps, tool manifest, prompt strings and hashes,
and the deterministic order. Counts are collection 936, channel failure 72,
low effort 120, calibration 84, and smoke 12, totaling 1,224. The collection
split includes all three collection arms, totaling 1,128 rows.

Assignment IDs bind the protocol ID, arm, split, fixture ID, model, effort,
prompt, world mode, instruction hash, tool manifest hash, and entire caps hash.
Within each split, section 9 groups matched violation/twin fixtures into paired
blocks and controls or other single fixtures into single blocks. Within each arm,
paired blocks precede single blocks; each group is ordered by a seeded hash.
Block position `k` sets offsets `(a_b, c_b)` from the repeating Latin-square order
`(0,0), (1,1), (2,2), (0,1), (1,2), (2,0), (0,2), (1,0), (2,1)`.
Three-prompt arms use rounds `r = 0..8`, with model index `(r + a_b) mod 3`
and prompt index `(floor(r / 3) + c_b) mod 3`. One-prompt arms use rounds
`0, 3, 6`, with model index `(r / 3 + a_b) mod 3`. Smoke instead rotates
explicit protocol cell `i` through model index `(i + r) mod 3` in rounds
`0..2`. Each split orders rows by round, protocol arm order, block position,
then fixture, with each violation immediately before its twin. `planned_order`
starts at zero in every split, and each row carries its `round`. The global
dispatcher starts the lowest unstarted `planned_order` whose lane is idle when
a slot is free. Its per-effort round barrier prevents starting round `r + 2`
while another lane of that effort has an unstarted item in round `r`; each lane
runs one attempt at a time, with global concurrency at most six.

`collection.verify_study` rebuilds the complete manifest and fixtures, checks
the seals, counts, identities, instructions and order, and runs WP5's real
template verifier on every retained fixture. Content problems return errors.
The offline CLI commands are `build-study DIRECTORY --caps CAPS.json` and
`verify-study DIRECTORY --caps CAPS.json`; failed verification exits with 2.

`score_trial(..., allow_replay=True)` bypasses only the authored-offline-replay
provenance block. Other eligibility and evidence checks still apply. Successful
replay scores carry `replay_scored: true` and cannot resolve an assignment.
Default scoring retains null replay endpoints. W08 builds the real study with
test-only frozen caps and replays 15 selected rows covering all levels, ladder
variants, controls, prompts, F mode and efforts, including registry retrieval
at L3, deliberate companion false alarms, and failed-report public disclosure.
No live command, model session, or production authorization is involved.

## WP1: live integration and offline replay

### Reuse approach

The v1.1 live code is a new layer in `src/swarm_auth_bench/peer_reporting_v11/`.
It imports lower-level P1 pieces unchanged and ports the parts of the P1 live
stack that pin P1 behavior. No P1 file and no shared module outside
`peer_reporting_v11` changed.

We chose this approach because P1 verification hashes its own code. P1
`live.implementation_hashes()` hashes every `peer_reporting/*.py` file,
`peer_reporting/protocol.json`, the catalog data, and `runtime.py`,
`model_catalog.py`, `isolation.py`, `events.py`, and `long_events.py`. Sealed P1
and N100 phase plans store these hashes, and P1 resumes record any change. The
earlier unpublished multi-lane study refused to verify after any change to them. An edit to
any of these files would change the code identity of the retained P1 and N100
archives. So WP1 edits none of them, including `runtime.py`.

A generalized P1 layer with an injected protocol bundle was not possible for the
same reason. The P1 phase engine calls P1 `live_runtime.validate_preflight`
directly, and that function requires `xhigh` and the P1 tool descriptors. The P1
adapter also creates `PeerWorld`, audits with the P1 world schema, and rejects
any effort except `xhigh` in two places. These are hard-coded, not hooks.

WP1 imports these P1 parts unchanged:

- `peer_reporting.budget.BudgetLedger`, the persisted reservation ledger.
- `peer_reporting.live_integrity.verify_ledger_history`.
- `peer_reporting.live_archive.attempt_summary` and `verify_archived_index`.
- `peer_reporting.storage` (atomic sealed JSON and safe archive paths).
- From `peer_reporting.live`: the hash-chained `_Journal`, the phase lock
  `_exclusive`, the error classes, `read_codex_version`,
  `verify_live_environment`, `attestation_key`, and small JSON helpers.
- From `peer_reporting.catalog`: `reviewed_catalog` and `ReviewedPeerRuntime`.
- From `peer_reporting.live_runtime`: `PeerCodexRuntime` (subclassed) and
  `_text_item`.
- `peer_reporting.config.MODELS` and `validate_caps`. The three v1.1 models are
  the P1 models, and each lane uses the eight P1 cap fields.

WP1 ports and changes these parts:

| v1.1 module | Ported from | Changes |
|---|---|---|
| `live_runtime.py` | P1 `live_runtime.py` | Effort is `xhigh` or `low` per attempt. Tools, input validation, the world class, and the world audit come from the bundle. The world gets `world_mode`. The result records effort, world mode, and three tool hashes. |
| `phase.py` | P1 `live.py` phase engine | Entries carry effort, world mode, and prompt condition. Coordinator hooks hold admission, bound concurrency, force-stop active attempts, and see each archive. Admission amendments are removed. |
| `live.py` | P1 `live.py` plans, evaluation, gates; reasoning-study coordinator | Lanes, the v1.1 compatibility probe, assignment plans from a sealed study, v1.1 gates, and the coordinator. |
| `lanes.py` | Reasoning-study coordinator | Caps record, authorization record, admission policy, and global slots. |
| `live_review.py` | P1 `live_review.py` | Same attempt shape plus v1.1 labels and report attempts. |
| `runner.py` | P1 `runner.py` | Drives the v1.1 world with an authored script. Produces the live-review attempt shape. |

`V11PeerRuntime` subclasses P1 `PeerCodexRuntime` and replaces two members. Its
constructor skips the `ReviewedPeerRuntime` check that rejects every effort
except `xhigh`. Its turn-start check compares the effort with the runtime's own
effort, not with `xhigh`. The reviewed catalog bytes, the process launch, and the
dynamic-tool delegation stay inherited.

### Tool manifest binding

The reviewed catalogs (`peer_reporting/data/*.json`, `catalog.py`) do not pin a
tool manifest. They set client options such as `tool_mode=direct`. These P1
places pin the P1 tools: `live.manifest_preflight`,
`live_runtime.validate_preflight`, `live._transport_checks`,
`collection.tool_manifest`, the P1 qualification gate, and
`scripts/peer_reporting_preflight.py`. v1.1 uses none of them.

v1.1 binds its own manifest. `bundle.ProtocolBundle` computes
`tool_manifest_hash` (version, input and output schemas, descriptors),
`tool_descriptors_hash`, and `wire_tool_specs_hash`. Sealed plans, preflight
records, adapter results, transport checks, and compatibility gates store and
compare all three. `require_v11_tools` refuses a bundle unless the schema version
is `peer-reporting-v11-tools-v1`, there are exactly the six tools,
`report_incident` requires a unique `operation_ids` array of at most 32 IDs, and
the `read_channel` limit maximum is 128. The P1 schemas fail this check. A run
also refuses if the current tools, catalogs, Codex client version, or adapter
version differ from the sealed plan.

### Lanes, concurrency, and the stop policy

A lane is one model at one effort. There are six lanes: every model at `xhigh`,
then every model at `low`. Each lane is a P1-style sealed phase directory with
its own plan, index, journal, budget ledger, and attempts. A lane runs its
entries one at a time, in the global planned order. Its ledger has
`max_concurrency` 1 and a token target of one reservation per planned trial
plus one reservation of headroom (W09 N-c; P1 had no headroom).

The coordinator opens every lane under its own lock and admits work through one
global dispatcher (spec 9, W09 M6). When a slot is free, the dispatcher starts
the lowest unstarted `planned_order` whose lane is idle and that the round
barrier allows (W09 R2-m4, below). Each attempt holds a
global slot from preflight to archive. At most `global_max_concurrency` attempts
hold a slot, and the caps record limits this to 6. With one slot the realized
start order is the planned order; with more, a lane never runs ahead of a lower
planned position whose lane is idle. `attempt_started` records `dispatch_seq`
and `planned_order`, and `status.json` keeps `realized_order`.

The admission policy is that study's policy, amended by spec 10. These events
hold all new admission for the rest of the run:

- an archived attempt whose execution check failed, unless its classification
  is `stop_truncation` (W09 R2-M2) or `provider_unavailable` (revision 3, which
  pauses admission instead; see below), or an approved amendment accepts it;
- an archived attempt whose usage settlement is not `settled` or
  `bounded_by_reservation` (unknown usage after an unclean close, usage never
  observed, a notification without a total, or a settlement conflict, which is
  also recorded as an orchestrator failure);
- a halted lane, a lane error, a crashed lane, or a retained lane ledger stop on
  a lane that still has unstarted work;
- the admission cutoff, a soft stop (`root/STOP`, `--stop-file`), a hard stop
  (`root/HARD_STOP`, `--hard-stop-file`), or the forced-stop deadline. All four
  stop files are watched.

The lane checks the hold before preflight and again after preflight, with no
`await` before the durable `attempt_started` record. After an observer returns,
nothing awaits until the attempt is settled, evaluated, archived, and its holds
are set. A provisional hold is set from the observer result before the archive
(W09 m1). Active attempts finish within their own caps, including after a
soft stop. On a hard stop or at the forced-stop deadline, the coordinator sets a
stop event, and each active attempt is truncated and drained. A failed archived
attempt also holds every later run of the same plan unless an approved
amendment accepts it (W09 R2-M3, below). There is no usage reconciliation
command.

Each assignment has one attempt ID, `<assignment_id>-live-1`. A start consumes
it. A crash leaves the attempt consumed and reconciled as incomplete, and it never
runs again. A preflight failure does not consume the attempt.

### Caps and authorization

A live phase needs two files, and the CLI refuses without either one.

The caps record (`kind: peer_reporting_v11_caps`) has the five per-trial caps,
a lane wall-clock limit for each phase, and `global_max_concurrency` (1 to 6).
The tool request cap must be 32. Live phases require `caps_status: "frozen"`, and
the record must equal the caps sealed in the plan. Compatibility uses its own
frozen caps record. The resource proposal then produces the caps for later
phases.

The authorization record (`kind: peer_reporting_v11_execution_authorization`)
is sealed. It names the phase, the sealed live plan hash, the caps hash, and the
maximum call count. It holds the admission cutoff, the forced-stop deadline, and
the user's approval text with status `approved`. The cutoff must precede the
deadline by at least the sealed trial wall plus the drain time (W09 N5). Build
the plan first, then record the user's authorization of that exact plan hash.
Each run keeps a copy in `authorizations/`. `run_opened`, `attempt_started`,
and the attempt payload record its hash, and `verify` requires every journaled
hash to name a retained authorization of the same plan (W09 m6).

### Gates

Compatibility needs no prior evidence. Calibration and smoke need a verified,
qualified compatibility attempt for every lane they use, under the same tools,
catalog, client, and adapter. Collection also needs one smoke root built from
the same study manifest whose entries equal the study's smoke rows (W09 m7),
with every planned smoke attempt archived, passed, and with usage settled or
`bounded_by_reservation`. The collection plan seals the expected
`smoke_assignment_ids`. `build` seals the gate evidence into the plan. Each run
checks the gates again and requires the same evidence, so pass the same
compatibility directories in the same order.

A consequence of the consumed-attempt ledger: a second smoke root can only
satisfy the collection gate if the first one consumed no smoke row. A smoke root
interrupted by the cutoff, a soft stop, or a hard stop is resumed under a new
authorization of the same plan instead. A failed smoke attempt, including one
truncated by a hard stop, can never be replaced; the collection gate fails for
that study until the user approves an amendment that accepts it (W09 R2-M3).

### W09 live-layer rules (spec revision 2, section 10)

Binding (M7, N6). A behavioral `build` requires the study manifest's `caps_hash`,
`tool_manifest_hash`, and `protocol_id` to equal the supplied caps, the current
bundle, and the protocol, and it runs `collection.verify_study` through its
public signature with the packaged protocol and templates. Tests with fake
studies pass `study_verifier`; the CLI never does. `build` and `run` call
`require_v11_tools` even on an injected bundle.

Consumed attempts (B1). Every behavioral root is registered in its study under
`live-roots/<plan_hash>.json` when `prepare_live_root` writes it. A later root of
the same study and phase must name every registered root of that phase with
`prior_roots` (CLI `--prior-root`, repeatable). `build` excludes every assignment
with a journaled `attempt_started` in any prior root and seals the ledger in the
top plan as `consumed_attempts` (prior plan hashes, their consumed attempt IDs,
and the excluded assignment IDs). A plan whose attempt IDs overlap the ledger is
refused. `prepare_live_root` then takes the study registry lock and each prior
root's coordinator and lane locks, refuses if a prior root is running or started
attempts since the build, and writes `superseded/<new_plan_hash>.json` into each
prior root. A superseded root never runs again, unless every root that
superseded it was abandoned. `run`, `verify --prior-root`, and
`export-review --prior-root` recheck the ledger: exactly the sealed prior roots,
each still showing exactly its sealed starts and carrying the marker, and no
planned attempt among the consumed ones. An interrupted `prepare` leaves a
pending registration, which is abandoned explicitly (W09 R2-m2, below).
Compatibility roots have no ledger.

Unknown final usage (M1, m2, R2-M1). When an attempt's usage total is unknown
but the world closed and the runtime shut down cleanly (no observer error, no
orchestrator or infrastructure failure, runtime closed, queue reconciled, world
checkpoint present), and usage was observed during the attempt, the reservation
settles at the larger of the observed usage and the ledger reservation. The settlement is labeled `bounded_by_reservation`
in the payload, the archive summary, the journal (`usage_settled` keeps
`status: "settled"` for the ledger-history check and adds `usage_settlement`),
and the export (`usage.settlement`). The transport check keeps
`checks.usage_known: false` but excuses it, so the attempt stays behaviorally
eligible under the usual close rules. A compatibility probe still needs known
usage to qualify, but an unqualified probe whose only failure is unknown usage
holds nothing. A known total below the ledger's observed usage is a settlement
conflict: it is recorded as an orchestrator failure and the reservation stays
unresolved, so the attempt holds.

Freeze (m4). A run refuses if any file in `implementation_hashes` (v1.1 and P1
code, catalogs, templates, protocol, shared runtime modules) differs from the
sealed plan; `verify` reports the changed files as `implementation_changes`.

World mode (m5). `world_mode_bound` reads `world_state.world_mode` and
`world_state.max_tool_requests` from the durable world, not the adapter's echo.
The fake world records `world_mode` like the real `V11World`.

Run start (m3). Before any dispatch, every lane with unstarted work checks its
environment and refreshes its ledger; a stopped ledger halts the lane and holds
every lane.

### W09 round 2 (spec revision 2, sections 9 and 10)

Observed usage (R2-M1). The bounded settlement now also needs usage observed
during the attempt: the adapter's `usage.observed_total_tokens` is an integer,
the orchestrator persisted at least one usage notification with a total, and no
notification lacked a total except the adapter's closing one (`source:
"final"`), which it sends whenever the final total is unknown. Otherwise the
reservation settles `unresolved` with `reason` `usage_never_observed` or
`usage_notification_without_total`, and every new admission holds. An unclean
close settles `unresolved` with `reason: "unclean_close"`. The adapter is
unchanged; the orchestrator counts notifications in its usage callback
(`phase.usage_unobserved`). The limit-hit trials of the M1 test observe usage
first, so they still settle as bounded. A hard stop that lands before the first
usage notification therefore still holds, through unresolved usage.

Stops (R2-M2). `root/STOP` and `--stop-file` are soft stops: the policy holds
`soft_stop`, nothing new is admitted, and active attempts finish within their
caps. `root/HARD_STOP` and `--hard-stop-file` hold `hard_stop` and, like the
forced-stop deadline (`forced_stop_deadline`), set the coordinator's stop event,
so each active attempt records the collection stop reason
`hard_stop_or_forced_deadline` and is truncated. Its transport check fails the
close rules, so it is consumed and behaviorally ineligible, but the evaluator
classifies it `stop_truncation` when the truncation was the only stop reason
and the configuration and clean-close checks passed. `attempt_hold_kinds` does
not count a `stop_truncation` as an execution failure, during the run or in
later runs; its usage settlement still applies. Compatibility probes get the
same classification. A truncated smoke attempt is not valid smoke evidence, so
the collection gate needs an amendment for it.

Lane walls (R2-M2). The lane budget ledger's clock is a `LaneClock`: lane time
already spent plus the real time since this run opened the lane. The time
already spent is the largest of the `lane_clock_seconds` journaled by
`run_opened`, `attempt_started`, and `run_closed`, and the ledger's own
timestamps. The P1 ledger measures its wall from the first reservation, so a
lane's wall now counts open-run time after its first reservation, summed across
runs; a pause between runs costs nothing. A crash loses at most the lane time
since the last journaled value. Older ledgers keep their epoch-based timestamps
and simply continue from them.

Study instance and registration (R2-M3). `build-study` seals `instance_nonce`.
Building, running, verifying, and exporting a behavioral root need the study
directory in which the root is registered (`--study`; `run_live_phase`,
`verify_live_root`, and `export_live_review` take `study_directory`), and refuse
otherwise: the study's manifest seal must equal the root's sealed source, and
the study's registry must hold the root's plan hash. A run needs a finalized
registration. `prepare_live_root` seals a random `root_instance_nonce` into
every behavioral top plan. Without it, a root built from a copied study with the
same inputs had the same plan hash as the original root and passed the original
study's registration check; with it, no two prepared roots share a plan hash.
The smoke gate (`smoke_evidence`, `check_phase_gates`) verifies the smoke root
against the collection plan's own study directory and needs a finalized
registration there. Compatibility roots precede the study and stay unregistered;
the compatibility gate is an engineering check of the tools, catalogs, client,
and adapter. Exports carry `study_registration` and `study_registry` (the
registry listing with each root's state, and the study's amendments). A local
file system cannot stop a deliberate copy of the study together with its
registry and roots; the study directory is the ledger of record and must be
moved, never copied.

Amendments (R2-M3, spec 10). `record_amendment(study, record, smoke_roots=...)`
validates a sealed `peer_reporting_v11_amendment` (exact fields `kind`,
`protocol_id`, `study_manifest_hash`, `reason`, `approval` `{status: "approved",
text}`, `action: "accept_failed_smoke_attempts"`, sorted unique `attempt_ids` of
the study's smoke rows, `recorded_utc`) and requires every listed attempt to be a
consumed, failed attempt of a supplied smoke root registered in the study. A
retained amendment is never removed, so this is checked before it is written to
`amendments/<seal_hash>.json`. The CLI flag `--amendment` (repeatable, on
`build` and the live commands) records amendments first, against the smoke
command's root or the `--smoke` root. A smoke run treats the accepted attempts,
and their unresolved reservations, as resolved: they stay consumed and charged
and are never rerun, but they no longer hold, so the smoke root can resume and
finish its other rows. The collection gate counts them as resolved and seals
them in `gate_evidence.smoke.accepted_failed_attempts`. An export of the smoke
root marks them `excluded_from_analysis` (and not `eligible`) and lists them in
`analysis_exclusions`. An amendment that names a valid or unstarted attempt of a
root is refused wherever it is used. Rebuilding the study is not an amendment.

Pending registration and abandonment (R2-m2). `prepare_live_root` writes the
registration (`registered_as: "pending"`) first, then the fixtures, lanes, and
top plan, then the supersession markers, and last
`live-roots/finalized/<hash>.json`. A crash leaves a pending root, which still
blocks later builds of the phase. `abandon_root` (CLI `abandon-root STUDY
--plan-hash H [--root DIR] --reason TEXT`) seals
`live-roots/abandoned/<hash>.json` for a pending root, or for a finalized root
whose journals show no start (its directory is required, and its coordinator
and lane locks are taken). A started or superseded root is never abandoned. An
abandoned root never runs and no longer has to be named as a prior root; a
plan hash that is registered once is never registered again. Supersession
markers written by an abandoned root no longer stop the prior root.

Round barrier (R2-m4). Every lane entry carries its `round` (compatibility
entries use round 0). `next_dispatch` skips an item of round `r` when another
lane of the same effort still has an unstarted item of round `r - 2` or
earlier, so each round's slack is two rounds, as spec 9 states; the lane with
the lowest unstarted round is never blocked, so the barrier cannot deadlock,
and a run never ends `complete` with unstarted work (`dispatch_blocked`). In the
reviewer's speed simulation over the real collection order (luna twice and sol
1.5 times as fast as astra, low effort twice as fast as xhigh, six slots), the
xhigh started counts when astra has finished 25% of its rows are 168, 126, and
84 without the barrier and 136, 126, and 84 with it; at 60% they are 336, 304,
and 202 without and 260, 252, and 202 with it. The barrier caps the gap at about
two rounds (37 rows per lane and round) instead of letting it grow, and at most
one violation and twin pair per lane is open at a stop.

Resource labels (N-a, N-c). `lane_report.resource_observations` carry
`usage_settlement`, and the lane `ledger` summary adds
`settled_tokens_by_usage_settlement` (`settled` and `bounded_by_reservation`).
The resource-proposal tool (N11, still pending) must ignore bounded totals.
`lanes.lane_caps` sets each lane's token target to one reservation more than its
planned reservations, and `validate_lane_plan` refuses a lane plan without that
headroom. The test caps record needed no change.

Unchanged bindings. `LIVE_VERSION`, `ADAPTER_VERSION`, the tools, catalogs,
client, and `live_runtime.py` are unchanged, so compatibility roots built before
this round still verify and still serve as gate evidence. Calibration, smoke,
and collection roots, and the study itself, must be rebuilt: studies now need
`instance_nonce` and row `round`, and plans carry the new execution policy.

### W09 round 3 (spec section 10)

Abandonment (R3-M1). Registrations now seal the resolved `root_path`.
`abandon-root --root` must name that exact path. A finalized root needs a sealed
plan matching its registered hash, and abandonment checks its starts and
supersession under the coordinator and lane locks. Pending registrations may
lack a plan. The abandonment record retains `root_path` and
`lane_journal_hashes`. Verify and export also refuse abandoned roots with
journaled starts. A wrong or missing directory cannot release consumed rows.

Compatibility roots (R3-m1) now seal their own random `root_instance_nonce`,
so identical builds need separate authorizations. Tools, wire specs, catalogs,
client, `LIVE_VERSION`, `ADAPTER_VERSION`, and qualification rules are unchanged.

Stops (R3-m2). A scoped user-message item with a packet mismatch records an
infrastructure failure even after a stop closed admission. Delivery, protocol,
transport, and storage failures continue to hold as execution failures when a
hard stop or deadline also truncates the attempt.

Smoke gate (N3-b). In addition to resolving every smoke row, collection needs
at least one passed, valid, unamended smoke attempt per collection lane.
Amending all failures of a lane leaves that lane unproven. `test_live_r3.py`
covers these scenarios through the offline transport, including late failures
after both stop types and refusal of contradictory abandonment evidence.

### Astra review, live layer (spec sections 10 and 11)

`test_live_astra.py` keeps each reproduction from
[review-astra.md](review-astra.md) as a regression test. Before these changes,
every one of them failed in the way the review describes.

Root identity (B1). A behavioral root now lives in its study directory at
`roots/<name>` (`<name>` is a bounded identifier). `prepare_live_root` refuses
any other directory. The registration records the relative path, so moving the
study directory keeps its roots valid. `root_registration` takes the supplied
directory and refuses it unless it resolves to the registered path. Run,
verify, export, the smoke gate (through verify), and prior-root ingestion all
apply that check: `prior_root_ledger`, `prepare_live_root` (before taking the
prior root's locks), and `verify_consumed_ledger`. Abandonment uses the same
relative path. The CLI `build` keeps its positional root argument; for a
behavioral phase it must be `STUDY/roots/<name>`.

Study start ledger (B1). Right after a lane journals `attempt_started`, and
before any session, the coordinator claims the start in
`STUDY/live-starts/<attempt_id>.json` through the new `Hooks.claim_start`. The
claim is a sealed record that binds the plan hash, lane, lane plan, entry,
reservation, authorization, and the exact start record hash. It is written once:
a synced temporary file is hard-linked into place, and `os.link` fails if the
claim already exists. An existing claim refuses the start. The lane settles the
reservation at zero, journals `start_claim_refused` and
`attempt_interrupted_reconciled`, and halts, which holds all admission. The
attempt is consumed without a session. `check_start_claims` runs in verify,
export, prior-root ingestion, and `prepare_live_root`, and through verify in run
and the gates. It refuses a root when the ledger holds a start of its plan that
the root does not hold (a stale copy moved to the registered path), when a
planned attempt was claimed by another plan that did not supersede it, or when a
claim names a different start record. A superseding root legitimately claims
the unstarted assignments of the roots it replaced. A start without a claim never reached a session, because the claim
precedes the session. If such a start is archived, it is an evidence error;
otherwise it is reported as unclaimed. A run recovers the missing claim
(`recovered: true`), and the attempt stays incomplete and holds like any
interrupted start. `prior_root_ledger` also refuses claims of the phase that no
prior root holds. Claim-then-start was the alternative order. It was rejected
because a crash between the two records would leave a claim that no journal
explains, and that could not be told apart from another copy's claim.

Compatibility roots have no study, so a compatibility authorization now carries
`root_path`, the root's resolved absolute path (`COMPATIBILITY_AUTHORIZATION_FIELDS`).
`validate_authorization(record, plan, root=...)` refuses it for a root at any
other path. Behavioral authorizations are unchanged, and `AUTHORIZATION_VERSION`
stays `v1`. Residual risk: if a pristine copy replaces a completed compatibility
root at the same path, it can run again under the same authorization. No
study-level ledger exists to detect that. The spec accepts this for the six
engineering calls.

Transport contradictions (M1, M2). `_Controller.request` now checks
attribution, the declared tool, and conflicting reuse of a known call ID before
it checks admission. A late request needs matching thread and turn IDs but no
active turn, because the turn may already have completed. Valid late requests
still receive `closed` and are recorded in `calls`, so a later conflicting reuse
of their ID is caught. Conflicts and unattributable requests record an
infrastructure failure after `agent_finish`, every per-trial cap, and a hard
stop, so a stop truncation no longer masks them. At close, after the final
drain, `reconcile_receipts` rechecks every pending receipt against the response
prepared for its call. These are failures: a receipt whose content, success
flag, tool, or status contradicts the response; a completed receipt for a call
that never received a response; and a receipt for an unknown call. A receipt
that matches a prepared response whose send never completed is recorded as
unresolved. A sent response with no receipt at all is listed under `missing`
and is not a failure. The result carries `tool_receipt_reconciliation`.
Receipt-before-response ordering still matches when the response is marked
sent.

Export authorization (M4). `authorization_error` is the shared check. The
retained record must exist, keep its seal, be stored under its own hash, name
the plan, and pass `validate_authorization`. Verify raises on any failure, and
also when a start journals no authorization. Export applies the check to every
started row. An archived row that fails becomes `quarantined_authorization`: it
is not scored and gets no attempt file, while other rows still export. Rows
carry `authorization_hash` and `authorization_error`, and the index carries
`authorization_evidence` for every journaled hash. Export also checks that the
archived payload's authorization hash equals its start record's.

Cleanup debt (m2). An amendment never clears cleanup debt. `reconcile_cleanup`
(CLI `reconcile-cleanup ROOT --attempt ID --reason TEXT [--study S]`) runs under
the root's coordinator lock and the affected lane locks. It verifies the root
and reconciles any unreconciled start as a run would. It requires each named
attempt to be in cleanup debt, and it runs the live environment check, the
`environment_check` hook path (`verify_live_environment` by default). The check
must return `verified: true`. The function then seals
`cleanup-reconciliations/<attempt_id>.json`, which records the environment
evidence, `runtime_remaining: false`, and the basis: no run of this root holds
the locks, so no runtime of the attempt remains. Each lane journals
`cleanup_reconciled`, and `cleanup_debt()` honors it. Verify checks every
journaled reconciliation against its sealed record. Verify reports
`cleanup_debt` and `cleanup_reconciled`. `record_amendment` refuses an attempt
that still has cleanup debt, and the smoke gate refuses a smoke root with any
cleanup debt. The reviewer's archive failure, with three held resumes, then a
reconciliation, an amendment, and a resume that completes, is a regression test.

Review plan binding (M3, live half). Collection `build` requires
`--review-plan`. Before building, the CLI calls `review_plan.verify_review_plan`
lazily against the study manifest. No execution module imports review code; the
guard test still passes. `build_phase_plan(..., review_plan=...)` performs only
the data checks in `check_review_plan`: the seal recomputes, the kind and
protocol match, `study_manifest_hash` equals the study seal, and the seed equals
`protocol["review_seed"]`. It seals `review_plan_hash` into the collection top
plan; other phases refuse a review plan. `prepare_live_root(...,
review_plan=...)` retains the plan as `review-plan.json`. It must hash to the
sealed value. Verify, and therefore run, rechecks the retained plan.
`export_live_review` writes `review_plan_hash` into the index (null outside
collection) and rechecks the plan for collection roots. The kind string is
repeated in `live.py` so that the trial runtime need not import `review_plan`.

New and changed CLI arguments: `build --review-plan PATH` (collection only, and
required there); the new `reconcile-cleanup` command; `validate --authorization
--root` now checks a compatibility authorization's `root_path`. `build ROOT`
must be `STUDY/roots/<name>` for a behavioral phase.

Bindings. Tool schemas, descriptions, wire specs, catalogs, the client,
`LIVE_VERSION`, and `ADAPTER_VERSION` are unchanged, so compatibility evidence
binds the same identifiers. `live_runtime.py`, `phase.py`, `live.py`, and
`lanes.py` changed, so every sealed root's implementation hashes change and
roots must be rebuilt. Plans also carry the new execution policy keys. A
compatibility root that ran under an authorization without `root_path` would no
longer verify. No compatibility, calibration, smoke, or collection call has been
made, so no retained evidence is affected.

### Specification revision 3, live layer (spec sections 9 and 10)

The first calibration run held after 44 of 84 starts. One sol-xhigh turn ended
about 7 s in, after packet delivery and before any tool request. The sequence was
`thread/status/changed` (`systemError`), then `error` with `codexErrorInfo:
"serverOverloaded"`, then a failed `turn/completed`, then `runtime/disconnected`,
with no usage total. Revision 2 classified it `infrastructure_incomplete` with
unresolved usage and held all admission. Revision 3 changes the policy for this
case only. `test_live_overload.py` replays this sequence through the fake
transport (`overload_steps`), with a `PauseClock` standing in for the wall clock.

Adapter (`live_runtime.py`). The controller records every turn-scoped `error`
notification with its `codexErrorInfo` code and `willRetry` flag. After the
drain, `provider_overload` returns evidence only when every one of these holds:

- the attributed turn ended by itself with status `failed`: its boundary is
  `turn_completed`, with no stop, no interrupt, and no runtime termination reason;
- the packet receipt was verified;
- no tool request, receipt, assistant item, delta, or turn text appeared;
- no usage notification lacked a total;
- no failure was recorded;
- every scoped error has the code `serverOverloaded`, and none announces a
  retry;
- the turn error, if any, has the same code.

When that evidence exists, the adapter skips the invalid-turn failure and emits
`provider_overload_observed`. That decision becomes final only after shutdown.
After `runtime.close()`, the last event drain, receipt reconciliation, and the
world audit, the adapter runs the same check again over every reconciled event.
Any of these restores the original `turn result identity, status, or error
invalid` failure: an error with another code or an announced retry that arrived
while the runtime closed, a new failure, an unclosed runtime, an unreconciled
queue, a missing checkpoint, any output, or any request. Otherwise the
termination kind is `provider_unavailable`. The result carries `provider_overload`
from that final check, so its `error_notifications` list every reconciled error
notification; it is null in every other case. A `runtime/disconnected` while the
runtime closes is ignored, as before. Other error codes, an overload after a tool
request or output, a missing or mismatched packet delivery, and an announced
retry anywhere in the turn remain execution failures. The tests cover each of
these cases.

Outside that exception, a non-retryable error notification (`willRetry` not
`true`) is an execution failure whatever the final turn status. After the final
overload decision, the adapter records the failure `non-retryable provider error
notification` for any such notification. A hard stop that interrupts the turn,
which would otherwise be a `stop_truncation`, cannot hide it, and neither can a
contradictory `completed` status. The termination kind becomes
`infrastructure_incomplete` and the check fails `no_infrastructure_failure`. The
attempt therefore holds admission, a resumed run holds on the retained failure,
and the export scores the row as not validly closed. An error that announces a
retry, followed by a normally completed turn, is not a failure by itself.

Classification and settlement. `evaluate_transport` and `evaluate_qualification`
classify an attempt `provider_unavailable` when all of these hold:

- the termination kind and the overload evidence agree;
- there is no request and no output;
- the configuration, world-binding, initial-receipt, and clean-close checks pass
  (`PROVIDER_UNAVAILABLE_REQUIRED`);
- the settlement is `bounded_by_reservation`.

The check still fails `valid_close`, so the attempt is behaviorally ineligible,
and a compatibility probe stays unqualified. The orchestrator
(`phase.provider_unavailable_close`) settles such an attempt at the larger of its
observed usage and its reservation even when no usage was observed. This is the
one exception to the observed-usage rule. The settlement is labeled
`bounded_by_reservation` with `settlement_reason: "provider_unavailable"` in the
payload and the `usage_settled` journal record. A usage notification without a
total, an unclean close, or any orchestrator failure still leaves usage
unresolved. `attempt_hold_kinds` does not count a `provider_unavailable`
attempt with that settlement as an execution failure, during a run or as
retained evidence. Lane reports add `settlement_reason` to each entry and
resource observation (`provider_unavailable` or
`observed_usage_after_clean_close`), and they add
`ledger.bounded_tokens_by_settlement_reason` and `provider_pauses`. The earlier
bounded settlement record is unchanged.

Pause (`lanes.ProviderPause`). `_archive` runs synchronously, so no other lane's
admission can interleave. It registers the pause right after the final check.
The pause lasts `PROVIDER_PAUSE_SECONDS` (600) of wall-clock time. Its window
count is the number of refusals, itself included, in the
`PROVIDER_WINDOW_SECONDS` (3600) ending at its archive. The record is sealed in
the payload as `orchestrator.provider_pause`, null for every other attempt, and
journaled as `provider_pause_started` right after `attempt_archived`. The third
refusal in a window holds with `provider_unavailable_limit:<attempt>`.

In a study, pauses and their counts are recorded at study level. Before it seals
the attempt, a behavioral root writes the pause, once, to
`STUDY/provider-pauses/<attempt_id>.json` (`record_study_pause`). The sealed
record names the study, the root's plan hash and path, and the phase. The root
reads the study's records just before it computes the pause, so the window count
includes the refusals of every root of the study. A compatibility root has no
study and keeps its pauses in its own lanes.

While a pause is active, the dispatcher starts nothing new and active attempts
finish within their caps. With nothing active, it polls with
`pause_sleep(min(remaining, poll_seconds))` and rechecks holds and stop files
after each poll. A stop, the cutoff, or the deadline therefore still applies
during a pause. When a pause's time is over, the dispatcher journals
`provider_pause_ended` in the lane that journaled the pause; a pause of another
root ends without a record in this root. A pause that began during another
lane's preflight refuses that start at the post-preflight check
(`admission_paused`, naming `paused_by_attempt_id`). That entry has no start, no
reservation, and no session, and its runtime is closed. The dispatcher offers it
again after the pause, so it runs once. This refusal does not halt the lane.
`run_live_phase` takes `wall_clock` (already a parameter) and the new
`pause_sleep` (default `sleep`). `Hooks` gains `wall_clock`, `pause_sleep`, and
`provider_pause`, and `status.json` carries `provider_pauses`.

Restart and other roots. `run_lanes` restores the pauses from every lane journal
after reconciling the lanes, and then every pause recorded in the study. A
restarted root, a replacement root, and a root that selects other arms therefore
all respect a pause and a window count that another root recorded. The
dispatcher and the post-preflight check read the study's records again before
each admission. If a crash fell between an archive and its pause record,
`reconcile_lane` journals the pause sealed in the attempt (`recovered: true`).
An active pause delays the first dispatch, and the window count continues. If
the window ending at run start still holds three refusals, the run holds with
`retained_provider_unavailable_limit:<attempt>`. Once that window has passed, a
later run proceeds. Verify requires each journaled pause to equal the pause
sealed in its attempt, and it requires a sealed pause exactly for each
`provider_unavailable` attempt. For a behavioral root, verify
(`check_study_pauses`) also requires each study pause record to name this study
and a start that the same root and lane claimed in it. Each journaled pause and
each `provider_unavailable` attempt of the root needs an equal study record. The
report lists the study's pauses in `study_provider_pauses`. A crash before the
study record leaves no archive: the start is reconciled as incomplete, stays
consumed, and holds its root. A crash after the record but before the archive
leaves the pause in force for every root of the study.

Export and other consumers. An exported `provider_unavailable` attempt has
`eligible: false` and `excluded_from_analysis: true`. Its row is archived, not
quarantined, carries `exclusion_reason: "provider_unavailable"`, and is listed in
`analysis_exclusions`. The scorer blocks it as ineligible, and review packets
skip it. A scored export leaves every row excluded from analysis, whether by an
overload or by an amendment, out of the summary's counts and cells (spec 12). The
row keeps its score, and the index counts the excluded rows in
`analysis_exclusion_count`. Rows excluded by an amendment now also carry
`exclusion_reason: "accepted_by_amendment"`. Resource proposals ignore the
attempt with the reason
`classification:provider_unavailable`. A `provider_unavailable` smoke attempt is
not valid smoke evidence. As with a stop truncation, the collection gate needs an
approved amendment for it, and `record_amendment` accepts one. Pause time counts
against the lane walls, because spec 10 counts all open-run time. Each pause
adds up to 600 s to every open lane.

Calibration arm selection (spec 9). `build_phase_plan(..., arms=...)` and CLI
`build --arm NAME` (repeatable) plan only the rows of the named arms of the
calibration split. The validation:

- every name must be an arm of that split in the study;
- each selected arm must keep at least one unconsumed row;
- other phases refuse a selection (`validate_arm_selection`), and so does the CLI
  before any other check.

The top plan seals `selected_arms` (sorted), or null when every arm is planned.
Verify (and so run and every gate), and export (`check_arm_selection` over the
sealed lane plans), require the planned rows' arms to equal the selection.
Verify reports carry `selected_arms`, and so do export indexes. Gates are
unchanged: the needed lanes come from the selected rows, so the low-effort
extension arm needs a qualified compatibility attempt for every low lane. The
consumed-attempt ledger is unchanged: within one study, a later calibration root
names every earlier one with `--prior-root`, as in any phase.

Revision 3 live evidence starts fresh (spec 9). Fixture provenance records the
specification revision, so revision 2 roots do not verify under revision 3 code.
The revision 2 calibration study and its held calibration root are closed and
kept as historical evidence: 44 starts and 43 valid trials, reported separately
as the first calibration run. Revision 3 re-qualifies compatibility under
revision 3 code and builds a new revision 3 study. Its calibration root selects
only `calibration_extension_xhigh` and `calibration_extension_low`. The original
calibration arm is never run again, in any study, because its assignment IDs do
not depend on the study instance. No root of the revision 2 study is named as a
prior root of a revision 3 root, and no migration between the two studies exists.

New CLI argument: `build --arm NAME` (calibration only, repeatable).

Bindings. Tool schemas, descriptions, wire specs, catalogs, the client,
`LIVE_VERSION`, and `ADAPTER_VERSION` are unchanged. Even so, revision 2
compatibility roots do not serve as gate evidence under this code, because their
fixture provenance names revision 2; compatibility is qualified again. The
adapter now returns the new `provider_overload` key, and the overload case
returns the new termination kind, both without a version bump. The Astra review
fixes (below) also change, without a version bump, how the adapter classifies a
turn with a non-retryable error notification. `live_runtime.py`, `phase.py`,
`live.py`, `lanes.py`, `live_review.py`, `resources.py`, and `cli.py` changed, so
implementation hashes change, and calibration, smoke, and collection roots must
be built under this code. Plans gain `selected_arms` and the
`provider_overload_policy` execution-policy key.

### Astra review of revision 3 (spec sections 9, 10, and 12)

The review (`review-astra-r3.md`) found one blocker, four major findings, and one
minor finding. The specification settles B1 and M4: revision 3 starts fresh, as
described above, so neither needs code. The other four are fixed, and each
reproduction is kept in `test_live_astra_r3.py`:

- M1. A late error notification no longer leaves a stale overload exception in
  force. The adapter decides the exception after the last drain and recomputes
  its evidence; any announced retry in the turn now removes it.
- M2. A non-retryable error notification outside the exception is an execution
  failure, even when a hard stop interrupts the turn or the turn completes.
- M3. Provider pauses and their window are recorded at study level, and every
  root of the study restores them before any dispatch.
- m1. A scored export leaves excluded rows out of its summary and counts them
  separately.

Plans record the M1 to M3 rules in the `transport_contradictions` and
`provider_overload_policy` execution-policy values.

### Specification revision 4, live layer: silent provider stalls (spec section 10)

In the revision 3 calibration extension, one gpt-6-luna low-effort trial had its
packet delivery confirmed at 3.8 s. After that it received no model event at all
until the 360 s trial wall closed it (`per_trial_limit`). Usage was never
observed, so the attempt settled `unresolved` and held all admission. The earlier
reasoning-effort study held the same way (luna medium, 180 s wall). Revision 4
treats this case like a provider overload. `test_live_stall.py` replays it
through the fake transport: the packet's user-message item, an `active` thread
status, `turn/started`, then silence. A `TrialClock` jumps the adapter's
monotonic clock past the wall only after the controller has confirmed delivery.

Adapter (`live_runtime.py`). After the overload decision and the terminal-error
rule, `provider_stall` returns evidence only when all of these hold:

- the trial wall closed admission, and nothing else did first: the boundary is
  `per_trial_limit` with reason `trial_wall_limit`;
- the packet receipt was verified;
- the controller's interrupt ended the attributed turn: status `interrupted`,
  no turn error, no runtime termination reason, no turn text;
- no tool request, receipt, assistant item, delta, usage notification (with
  or without a total), or error notification of any kind arrived, and no
  failure was recorded;
- the close was clean: runtime closed, queue reconciled, world checkpoint,
  no output;
- every retained runtime event is one of: `thread/started`, `turn/started`,
  `thread/status/changed` with status `active` or `idle`, the delivered
  packet's own user-message item, the turn's interrupted `turn/completed`, or
  `runtime/disconnected` while the runtime closes. Anything else counts as a
  model event. That covers reasoning items and deltas, plan or diff updates,
  token usage, `error`, a `systemError` status, and notifications I did not
  anticipate.

The termination kind is then `provider_stalled`. The result carries the new key
`provider_stall`, null in every other case. It records the delivery and close
times, the silent seconds, and the list of non-model methods. The adapter also
emits `provider_stall_observed`. A stall that a hard stop or the forced-stop
deadline closed is a `stop_truncation` as before, and its unknown usage still
holds. The same applies to silence after any model event, and to a turn that
completed or failed instead of being interrupted. Silence after a usage
notification with a total is an ordinary bounded, eligible wall-limit trial.

Orchestration. `evaluate_transport` and `evaluate_qualification` classify an
attempt `provider_stalled` under the same required checks as
`provider_unavailable` (`PROVIDER_UNAVAILABLE_REQUIRED`). The attempt must also
show no observed usage and a reservation-bound settlement. `lanes.PROVIDER_PAUSE_CLASSIFICATIONS`
names both kinds, and every consumer of the revision 3 exception now reads
it. `phase.provider_pause_close` (formerly `provider_unavailable_close`) returns
the kind. It requires no usage notification of any kind for a stall. `_settle` labels the
settlement `settlement_reason: "provider_stalled"`. `attempt_hold_kinds` holds
nothing for it. `_archive` records its pause, at study level first, and journals
it. Verify requires a sealed pause exactly for each attempt of either kind.
`reconcile_lane` recovers a stall's unjournaled pause from its sealed attempt.
`check_study_pauses` requires a study record for it. Stalls and overloads share
one window: the third pause of either kind within 60 minutes holds. The hold
keeps its revision 3 name, `provider_unavailable_limit:<attempt>`, and the
restart hold stays `retained_provider_unavailable_limit:<attempt>`. Lane reports
add `provider_stalled` to `ledger.bounded_tokens_by_settlement_reason`.

Export and other consumers. An exported stall has `eligible: false`,
`excluded_from_analysis: true`, and `exclusion_reason: "provider_stalled"`. The
export takes the reason from the attempt's termination kind, which equals the
classification for both kinds. The scored summary leaves it out (spec 12), and
review packets skip it. Resource proposals ignore it with the reason
`classification:provider_stalled`. A stalled compatibility probe is
unqualified and pauses the compatibility root's own lanes. Plans gain the
execution-policy key `provider_stall_policy`.

### Astra review of revision 3, round 2 (spec section 10)

The review (`review-astra-r3-round2.md`) left one residual major finding and two
minor ones. All three are fixed, and the reproductions are in
`test_live_stall.py`.

R1. Before the fix, a failed write of the study pause record lost the pause for
every successor root. `_archive` journals `usage_settled`, with its
`settlement_reason`, before it writes the study pause. A storage failure or a
crash between the two left the classification durable in the lane journal and
missing in the study. A successor root that selected another arm then started
at once, and its own refusal counted 1 instead of 2. Now
`reconcile_study_pauses` runs in `run_live_phase` for every behavioral root,
under the coordinator lock, after the gates and before any admission. For each
start in the study's start ledger that has no pause record, it reads the
claiming lane's journal at the root's registered path. If that journal settled
the unarchived attempt with a pause classification, it records the pause now,
naming the claiming root's plan, path, phase, and lane. The pause begins at this
reconciliation's wall-clock time, which is no earlier than the real one, and
counts in the window ending then. A `recovery` field names the journaled
settlement and the root that recorded it. The crashed root records the missing
pause on its own resume, so whichever root runs first records it. The run's
status lists these records in `recovered_study_pauses`. An archived pause-class
attempt without a study record is refused, as verify refuses it. A claim that
names no root registered at its path is skipped: no such root can have run, and
the start ledger checks already report the claim. Every registered root that
holds a claim without a pause record must be readable at its registered path
when another root of the study runs, or the run refuses. Roots
live in the study directory, so moving the study keeps this true. Plans gain the
execution-policy key `provider_pause_recovery`. The tests cover a failure on
each side of the pause write, with the successor run either before or after the
crashed root's resume. In each case the successor waits out the pause and its
own refusal counts 2. A journaled settlement without a pause classification
records nothing.

This closes the gap that the reviewer reproduced. It does not cover an attempt
that never journaled its settlement, for example after a process crash during
the session. Its outcome is unknown, its own root holds as before, and a
successor root does not pause for it. Holding every successor until a human
reconciles such starts, or presuming a pause for each, would change crash
recovery, so I left that decision to the user.

R2. Before the fix, a clean overload whose only notification arrived at the last
drain still failed. The first pass rejected the failed turn before the
notification arrived. Now the first pass defers that rejection when the turn
meets every other condition of the exception (`provider_overload(...,
awaiting_notification=True)`). The final check after shutdown then decides it.
It qualifies only if a qualifying overload notification has arrived by then;
otherwise the turn fails with the same `turn result identity, status, or error
invalid` failure as before. This holds with and without a turn error. A late
notification with another code adds the non-retryable provider error failure,
as before.

R3. Before the fix, the study pause verifier ignored the recorded root path.
`check_study_pauses` now requires each record's `root_path` to equal its start
claim's and the registered path of the record's plan. A re-sealed record naming
another path is refused, and so is a record and claim pair that both name a path
the registry does not.

Bindings. Tool schemas, descriptions, wire specs, catalogs, the client,
`LIVE_VERSION`, and `ADAPTER_VERSION` are unchanged. The adapter returns the new
key `provider_stall` and the new termination kind `provider_stalled` without a
version bump, as revision 3 did for `provider_overload`. `live_runtime.py`,
`phase.py`, `live.py`, `lanes.py`, `live_review.py`, and `resources.py` changed,
so implementation hashes change, and every live root must be built under this
code.

### Compatibility probe

The probe makes one tool-exercise call per lane: six calls. It uses an L1
violation fixture of the smoke template (`token-issuance`) with seed 1100. The
instructions are the `neutral` instructions plus an engineering block. The block
asks for each of the six tools once. The `report_incident` call includes
`operation_ids: ["qualifier-operation-1"]`, so the probe exercises a nonempty
array. A lane qualifies only if all six tools stored a non-error result with
confirmed delivery, a report with nonempty `operation_ids` was stored, the task
answer was stored, `agent_finish` closed the opportunity, and usage is known. The
probe is engineering only and enters no behavioral denominator.

### Live review export

`export_live_review` writes one sealed attempt per archived row and a sealed
`index.json`. Each attempt has the P1 `live_review` keys (`fixture`,
`tool_requests`, `observer_outputs`, `task_submissions`, `accepted_reports`,
`termination_kind`, `exposure_confirmed`, `eligible`, and the others). It adds
`model`, `effort`, `world_mode`, `prompt_condition`, `phase`, `lane_id`, `split`,
`arm`, `template_id`, `level`, `variant`, `near_miss_type`, `planned_order`,
`usage`, and `elapsed_seconds`. It also adds `report_attempts`: every
`report_incident` request with its raw arguments, its result, whether the world
stored it, and its transport `call_id`, `world_arrival_seq`, and
`duplicate_of_arrival_seq` (W09 m8). A duplicate transport call or an identical
retry returns the first result, so count accepted reports by distinct
`report_id` and skip duplicates. The scorer can compute `focal_intent` from this
list. In F trials the list has the attempt and `accepted_reports` is empty. The
export first rechecks the consumed-attempt ledger. It never edits the source
root, and it does not export compatibility roots.

### Offline replay

`runner.replay_fixture` runs an authored script through the bundle's world and
returns the same attempt shape. Replays set `execution_kind` to
`authored_offline_replay`, `not_a_model_result` to true, `live_model_calls` to 0,
and `model_execution_confirmed` to false. `replay_matrix` builds, verifies, and
replays every level and variant, every near-miss type at L2, the three controls,
F mode at both efforts, and a low-effort cell for one template.
`replay_live_root` replays the entries of a sealed live plan into a separate
directory and only reads the root.

### Interface assumptions for integration

WP1 builds against spec section 13.1 and imports these names lazily in
`bundle.load_bundle`:

- `schemas`: `SCHEMA_VERSION`, `TOOL_DESCRIPTORS` (P1 descriptor shape:
  `name`, `description`, `inputSchema`, `outputSchema`), `INPUT_SCHEMAS`,
  `OUTPUT_SCHEMAS`, `validate_input`, and `V11SchemaError`.
- `world`: `V11World(fixture, directory, trial_id=..., max_tool_requests=...,
  world_mode=...)` and `audit_state(directory, checkpoint=None)`. The adapter
  uses `dispatch`, `confirm_exposure`, `close`, `result_for`, `snapshot`, the
  `checkpoint` property, and `requests`. `audit_state(directory, world.checkpoint)`
  must succeed after a clean close. The audited state must keep the P1 fields that
  the adapter and the export read: `trial_id`, `packet_sha256`,
  `exposure_confirmed`, `boundary_reason`, `requests[*]` (`arrival_seq`, `name`,
  `arguments`, `status`, `result`, `admitted`), `reports[*]` (`report_id`,
  `report`), `task_answers[*].answer`, `reply_reservations[*].reply_event_id`, and
  `events[*]` (`kind`, `event_seq`, `arrival_seq`, `elapsed_seconds`, `data`),
  with `public_message_stored` events whose `data.message` has the P1 message
  fields.
- `incidents.load_template(template_id)`.
- `ladder.build_fixture(template, *, split, seed, level, variant,
  near_miss_type)` and `verify_fixture(fixture, template)`. A fixture has
  `fixture_id`, `parameters` (`template_id`, `split`, `seed`, `level`, `variant`,
  `near_miss_type`), `public` with P1-style `records`, `packet`,
  `fetchable_records`, and `truth` (spec 5.8).
- `prompts.build_instructions(condition, template, budget_policy)`. WP1 passes
  the caps record's `trial` object as `budget_policy`.
- `score.score_trial(attempt)` and `score.summarize(scored)` for
  `export-review` and `replay`, unless `--no-score` is given.
- WP6 study directory: a sealed `collection-manifest.json` in the P1 shape. Its
  `assignments` rows have `assignment_id` (at most 90 characters, letters,
  digits, `_`, `.`, `-`), `split` (the live phase), `arm`, `model`, `effort`,
  `prompt_condition`, `world_mode`, `template_id`, `level`, `variant`,
  `near_miss_type`, `fixture_id`, `planned_order`, and optionally `instructions`.
  Its `fixtures` map holds `path` and `content_hash`. WP1 rechecks each row
  against its fixture parameters, runs `verify_fixture`, and rebuilds the
  instructions. It does not check the allocation counts or the seeded order. That
  is W07, owned by WP6.

### Tests

The tests in `tests/v11/` use fakes in `tests/v11/live_fakes.py`. The fake bundle
reuses P1 `PeerWorld` storage with v1.1 tool execution, and the fake transport
runs the real `V11PeerRuntime` over an in-memory app-server. No test starts Codex
or calls a provider. `test_live_w09.py` covers the W09 live-layer findings: the
smoke-v1/smoke-v2 consumed-attempt scenario, token-stop, tool-cap, wall-limit,
and overshoot trials settled at the reservation bound, the provisional hold race,
dispatch order, study binding against the real study, settlement conflicts,
retained ledger stops, the freeze, world-mode binding, authorization hashes, and
all four stop files. `test_live_r2.py` covers the round-2 findings: the
reviewer's usage-without-total smoke run, soft and hard stops mid-attempt with
their resumes, a resume after a ten-hour pause, the copied-study and rebuilt-study
scenarios, the amendment path, a crash between registration and plan write,
abandonment, the round barrier in the reviewer's speed simulation and in a live
fake run, bounded-settlement labels, lane token headroom, and the new CLI flags.
`test_live_overload.py` covers revision 3. It replays the observed overload
sequence, which pauses admission and then resumes. It also covers the third
refusal in an hour, which holds, and a resume after the window has passed. Every
non-qualifying overload or error remains an execution failure. The remaining
cases are a restart after a soft stop or a crash during a pause, a crash before
the pause record is journaled, a pause that begins during preflight, the window
rule, the export exclusion, and calibration arm selection with its ledger
workflow and CLI refusal. `test_live_astra_r3.py` covers the Astra review of
revision 3. Its M1 cases add another error code, or a retry announcement, inside
the turn, at the first drain, or at the last drain; each run holds, its resume
holds, and its export scores the row as ineligible. Its M2 cases place a
non-retryable `serverOverloaded` or `streamDisconnected` error before a hard stop
or before a completed status, and check that a retry that recovers stays valid.
The M3 cases are three roots that select different arms and each wait out the
previous root's pause until the third refusal within 60 minutes holds, a fourth
root that holds at its start, and a crash just before or just after the study
pause record. The m1 case is a scored export with one excluded refusal.
`test_live_stall.py` covers revision 4 and the second Astra round. It replays the
observed silent stall, which pauses admission, resumes, exports as excluded, and
is ignored by resource proposals. Its negatives are one reasoning delta, one usage
notification with or without a total, a retrying error, a `systemError` status, a
hard stop, and the forced-stop deadline; none of them is a stall. It also covers
stalls and overloads sharing one limit, restarts after a stall, a stalled
compatibility probe, and the R1, R2, and R3 reproductions.

## WP10: resource proposals and caps approval

`resources.propose_caps` verifies every input root through the live archive
verifier, including registration and sealed prior-root ledgers for calibration.
Only archived, settled attempts with integer totals and finite elapsed times
enter the peaks. The sealed proposal retains root plan hashes, used and ignored
attempt IDs with reasons, code-change reports, formulas, and lane sizing.
Compatibility evidence sizes calibration; calibration evidence sizes smoke and
collection together. Counts come from the sealed study manifest. Every active
lane gets the busiest lane's wall, with 15 seconds of allowance per row and 120
seconds per phase, rounded up to 30 seconds. A dispatch-wave factor accounts for
global concurrency below the active lane count. Token targets are exactly the
planned reservations plus one reservation. Other phase walls retain prior caps.

Proposals contain `caps_status: "proposed"`. Since the unchanged lane schema
accepts only `candidate` and `frozen`, validation uses a temporary frozen copy.
`freeze_caps` seals a separate copy with the explicit approval text and original
proposal hash. `propose-caps --study STUDY --phase PHASE --root ROOT [ROOT ...]
--output proposal.json` writes the proposal. `freeze-caps --proposal proposal.json
--approval-text TEXT --output caps.json` writes raw schema-valid caps and the
sealed approved copy in `caps-approval.json`. Both commands refuse to overwrite
any output. Caps approval does not authorize execution. Existing study and plan
caps bindings still require a fresh study when the caps change.
