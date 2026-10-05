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
each round. `planned_order` starts at zero in every split.

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
`max_concurrency` 1 and a token target of one reservation per planned trial, as
in P1.

The coordinator opens every lane under its own lock and admits work through one
global dispatcher (spec 9, W09 M6). When a slot is free, the dispatcher starts
the lowest unstarted `planned_order` whose lane is idle. Each attempt holds a
global slot from preflight to archive. At most `global_max_concurrency` attempts
hold a slot, and the caps record limits this to 6. With one slot the realized
start order is the planned order; with more, a lane never runs ahead of a lower
planned position whose lane is idle. `attempt_started` records `dispatch_seq`
and `planned_order`, and `status.json` keeps `realized_order`.

The admission policy is that study's policy, amended by spec 10. These events
hold all new admission for the rest of the run:

- an archived attempt whose execution check failed;
- an archived attempt whose usage settlement is not `settled` or
  `bounded_by_reservation` (unknown usage after an unclean close, or a
  settlement conflict, which is also recorded as an orchestrator failure);
- a halted lane, a lane error, a crashed lane, or a retained lane ledger stop on
  a lane that still has unstarted work;
- the admission cutoff, the forced-stop deadline, `root/STOP`, or the
  `--stop-file` (both are watched).

The lane checks the hold before preflight and again after preflight, with no
`await` before the durable `attempt_started` record. After an observer returns,
nothing awaits until the attempt is settled, evaluated, archived, and its holds
are set. A provisional hold is set from the observer result before the archive
(W09 m1). Active attempts finish within their own caps. At the forced-stop
deadline or on a stop file, the coordinator sets a stop event, and each active
attempt is truncated and drained. A failed archived attempt also holds every
later run of the same plan. WP1 has no usage reconciliation command and no
admission amendment.

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
interrupted by the cutoff or a stop file is resumed under a new authorization of
the same plan instead. A failed smoke attempt can never be replaced, so the
collection gate then fails for that study.

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
prior root. A superseded root never runs again. `run`, `verify --prior-root`, and
`export-review --prior-root` recheck the ledger: exactly the sealed prior roots,
each still showing exactly its sealed starts and carrying the marker, and no
planned attempt among the consumed ones. An interrupted `prepare` can leave a
registered root without a top plan; recovering from that is an explicit manual
step. Compatibility roots have no ledger.

Unknown final usage (M1, m2). When an attempt's usage total is unknown but the
world closed and the runtime shut down cleanly (no observer error, no
orchestrator or infrastructure failure, runtime closed, queue reconciled, world
checkpoint present), the reservation settles at the larger of the observed usage
and the ledger reservation. The settlement is labeled `bounded_by_reservation`
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
both stop files.
