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
Each split groups rows by fixture, world mode, and effort. A group with `c`
cells puts its seeded cell `i` in round `floor(i * R / c)`, where `R` is the
largest group size in the split. A separate seeded hash orders entries within
each round. `planned_order` starts at zero in every split. Every row also
carries its spec 9 `round`, which the live dispatcher's round barrier reads.

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
  is `stop_truncation` (W09 R2-M2) or an approved amendment accepts it;
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
