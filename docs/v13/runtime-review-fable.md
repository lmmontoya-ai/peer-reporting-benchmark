# Agent runtime review: Claude Fable 5.1 (high)

After the next-steps discussion ([next-steps-discussion.md](next-steps-discussion.md)), the
user asked Fable to answer three things:
- whether the agents are stateless, and whether reasoning summaries could change
  behaviour;
- a review of the agent implementation;
- how to keep building the agents.

The reply is verbatim. The coordinator's checks follow it.

I have what I need. Below is the review.

## 1 Statelessness

**The claim is correct as far as the client is concerned, with four qualifications.** Each trial launches a new app-server with a fresh `CODEX_HOME` that is also its working directory, an environment allowlist, `ephemeral: true` threads, and the cross-session features off, in `src/swarm_auth_bench/runtime.py:452-463` and `:412-423`. Within a trial the Codex client resends the whole item history on every call and does not use stored responses. The cache pattern you observed, zero cached on the first call and most input cached on the next, matches that. For the reasoning lanes, encrypted reasoning items also travel back with each call, so the model's own earlier reasoning is reused within a turn whether or not summaries are requested.

Remaining paths, ranked by whether they matter:

- **Account-level provider state matters most, and the client cannot rule it out.** The shared `auth.json` is copied per trial, so it carries identity only. But every trial runs under one account, so snapshot routing, server-side experiments and rate limiting are shared across all trials and lanes. This is the plausible home of the luna-low downward trend. Treat it as a stated limitation and capture the served model string per call where the response carries it.
- **The transport attestation does not exercise the live path.** The probe forces an HTTP provider with `supports_websockets=false` in `runtime.py:354-362`, while the pinned catalogs set `prefer_websockets: true`. The live run may use a websocket transport with its own in-connection state. Any such state dies with the process, so it does not cross trials, but the request the probe captures is not literally the request the model receives. Verify, do not assume.
- **The Codex environment frame is extra context the model sees.** The app-server injects an environment context message with the working directory, sandbox and approval policy. The directory name starts with `swarm-codex-controller-`, which the wire-label scan in `schemas.py` would reject if it appeared in a tool spec. It varies per trial only in the random suffix. Low validity impact, but rename the prefix and archive the captured frame.
- **Orphaned processes after a cleanup timeout.** `close()` runs on the leftover drain budget, discussed below. A killed-midway close can leave a process and home directory alive into the next trial. That is resource contention, not context leakage.

Prompt caching across trials is keyed by a fresh thread id and does not change outputs. The binary and catalogs are hashed and version-pinned. Nothing in the guest persists between trials except the clock.

## 2 Reasoning summaries

**Summaries were off by catalog default, not only by the null turn setting.** All three pinned catalogs declare `default_reasoning_summary: "none"`, for example `src/swarm_auth_bench/data/luna-direct-catalog.json:37`, and also `supports_reasoning_summary_parameter: true`. So the parameter exists and the archive is consistent.

**Can requesting them change behaviour?** The summary text is attached to the reasoning item, and the Codex client sends reasoning items back with each call inside a turn. So summary text does re-enter the request. Whether the model attends to it is undocumented. The model already has its own encrypted reasoning in context, so the summary adds no new information to it. Expect a null or tiny effect, but it is a request-parameter change and the core comparison must not pool with it.

**What the smoke must measure.** Include one visible fixture so closure runs with summaries present.

1. The archived thread settings show the requested summary value, and the captured request carries it under `reasoning`.
2. Reasoning items arrive and are archived under a new top-level key, not inside `observer_outputs`. luna-low yields none.
3. No classification regression. `reconcile_turn_notifications` in `peer_reporting_v11/live_runtime.py:643-679` requires every item embedded in `turn/completed` to equal a standalone completed item seen by the turn loop. Reasoning items are new there. If their embedded and standalone shapes differ, every trial becomes `infrastructure_incomplete` and holds the lane. Check `unresolved` is empty.
4. Per-call input tokens, uncached and cached, against the same fixture in the core at the same call index. Growth beyond the summary's own length would mean text re-entered context.
5. Reasoning tokens per call unchanged in distribution.
6. A mutation test: copy an archived attempt, inject a summary containing report words, re-score, and confirm `focal_hit`, `owner_informed` and `authorization_engaged` are unchanged. Also confirm the visible room notices contain no summary text.

**Setting.** Use `detailed`. `concise` loses the mechanism signal and `auto` lets the provider vary it. Pin it in the preflight record and check it on the turn-start request, exactly as effort is checked in `live_runtime.py:138-143`.

**Standard capture.** Yes to all three. Reasoning tokens per call are already in the archived usage events; promote `last` and `reasoningOutputTokens` to the attempt summary. Record the served model string from `rawResponse/completed` if present. Archive the captured probe request minus secrets: `instructions`, `reasoning`, `store`, `include`, `truncation`, `parallel_tool_calls`, verbosity, and the environment frame.

## 3 Implementation review and how to continue

**Ranked threats.**

1. **Cleanup budget is the leftover drain grace. Fix before v1.4.** `remaining()` in `live_runtime.py:912-916` is shared by the close future, the turn task, the dispatches and then `runtime.close()` at `:956-962`. A slow drain leaves cleanup a millisecond, and `_stop_process` alone waits five seconds before killing, `runtime.py:275-295`. The timeout then cancels `close()` midway, so the temp home and session table may be left behind, and the lane holds on cleanup debt. Give cleanup its own fixed budget, make `close()` resumable with a second kill-and-remove pass, and sweep orphan `swarm-codex-controller-*` processes and directories at root start.
2. **Receipt and reconciliation checks are byte-exact and will break under larger outputs or new item types. Fix before v1.4 if summaries go on.** `_receipt_matches` at `live_runtime.py:390-394` demands the echoed content equal the wire text. The catalogs set a tool-output truncation policy of ten thousand tokens, and `read_channel` allows 128 messages of up to 2,000 characters. A truncated echo would be a contradicted receipt, an execution failure. Small rooms are safe. Test a near-limit `read_channel` in the smoke.
3. **Packet format. Stated limitation, keep for comparability.** The packet is `canonical_json(public)` in `ladder.py:1079`. Sorted keys decide where the authorization lookup sits, so lookup position is an alphabetical artifact. The salience arm already tests placement. State it.
4. **Redundant tools and schema friction. Stated limitation plus one analysis.** Everything is in the packet, so `read_channel` and `read_record` are affordances that suggest more exists. `report_incident` requires nine fields. Check the five engaged luna-low misses for rejected `report_incident` calls with `schema_error`. Zero cost.
5. **Peers scripted as JSON and pressure delivered inside tool receipts.** The report-trigger reactions arrive in the `room_events` of the very call that stored the report, so they can only affect post-report behaviour. Packet posts carry the pre-report dose. Say this in the dose statement for C3.
6. **Closure classification.** `closure_outputs` in `closure.py:8-13` keeps only `final_answer` or phase-absent messages and finish summaries, so commentary never reaches the owner or the room. A new `reasoning_summary` source kind is excluded automatically. Keep summaries out of `observer_outputs` anyway so no scan ever sees them.
7. **Stall detection.** Anything not in `STALL_LIFECYCLE_METHODS` counts as model activity, so a summary followed by silence becomes an ordinary wall-limit hold instead of a stall. Acceptable, document it.

**How to continue.**

- **Keep:** one thread, one user turn, the six-tool set, the packet format, locked config, pinned version and catalogs, the admission and receipt machinery. These define comparability with the core.
- **Change now:** cleanup budget and orphan sweep, summary parameter pinned at `detailed`, a separate archive key for reasoning items, probe capture of the full request shape, served-model capture, per-call usage in the attempt summary, a neutral temp-directory prefix.
- **Later, as separate protocol versions:** live model peers break the fixed-dose property that the whole design rests on, and multi-turn owners add a second routing claim. Do neither inside v1.4. If pursued, run them as their own level with their own gate.
- **Larger rooms:** the runtime needs a websocket-aware or transport-agnostic attestation, a receipt check tolerant of client-side truncation, an explicit compaction setting recorded per trial, and a length-matched filler control. Until then, keep rooms small.
- **Standardize per trial:** Codex version, catalog hash, request shape hash, effort, summary setting, served model string, per-call input, cached, output and reasoning tokens, cleanup confirmation, and the environment frame hash.

**Decisions I recommend.**

1. Give runtime cleanup its own fixed budget, make close resumable, and sweep orphans at root start.
2. Request summaries at `detailed`, pinned in preflight and checked on turn start.
3. Archive reasoning items under a separate key, never in `observer_outputs`, and add the mutation test to CI.
4. Extend the probe to archive the full request shape, and run the six-trial smoke with one visible fixture and one near-limit `read_channel`.
5. Capture served model string and per-call reasoning tokens as standard fields.
6. Rename the temp-directory prefix to a neutral string.
7. Keep packet, tools, and turn structure unchanged for v1.4 and state the lookup-position and receipt-delivered-dose limitations in the spec.
8. Defer live peers, multi-turn owners and larger rooms to separate protocol versions with their own gates.

## Coordinator's checks

**Code claims confirmed:**
- threads are created with `ephemeral: True` (`runtime.py:415`);
- the luna and sol catalogs set `default_reasoning_summary: "none"`,
  `supports_reasoning_summary_parameter: true` and `prefer_websockets: true`;
- the transport probe sets `supports_websockets=false` (`runtime.py:356`);
- `runtime.close()` runs under `remaining()` of the shared drain budget
  (`drain_grace_seconds`, 10 s in the frozen caps).

**The cleanup budget caused the core hold.** The core attempt that held `core-v1`
recorded a drain of 11.2 seconds. Cleanup therefore had no budget left and ended with
`runtime cleanup incomplete: TimeoutError`.

**Fresh client home per trial.** Three consecutive luna-low core trials used three
different controller homes.

**luna-low's five engaged misses** (the zero-cost check). All five filed accepted reports
(`stored`), with no schema errors. All five were K=12 trials, and the reports named other
operations, not the focal one; two of them had `any_hit` true. So no luna-low miss was a
decision to stay silent after engaging:
- 35 misses never engaged with the authorization evidence;
- 5 reported something, but not the focal operation, among twelve.
