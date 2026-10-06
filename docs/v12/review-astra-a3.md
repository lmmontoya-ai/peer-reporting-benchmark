Verdict: **NO-GO** for `social_grid_xhigh`, `social_grid_low`, and
`social_anchor_xhigh` (612 grid trials plus 36 anchor trials). Two major findings
allow A3 to hide transport defects behind a provider pause. Fix both before
sealing the plans for authorization.

Reviewed `781aee7..6c951e5`, principally `6a94f2b`, against v1.2 section 8.1 and
v1.1 section 10. Source references below use
`src/swarm_auth_bench/peer_reporting_v11/` unless stated otherwise.

1. **Major M1. Retained error notifications can escape A3's error count and code check.**
   `live_runtime.py:709` checks `self.provider_errors`, but `event()` returns at
   `live_runtime.py:529` before recording errors with a missing or different
   `turnId`. The new raw-event scan at `live_runtime.py:695` checks retry flags
   everywhere, but checks error codes only inside lifecycle notifications.

   Reproduction with `FakeTransport`: deliver the packet, answer `READ`, emit
   `("raw_thread", "error", {"error": {"message": "broken stream",
   "codexErrorInfo": "streamDisconnected", "additionalDetails": None},
   "willRetry": False})`, then append `overload_steps()`. The extra error has the
   correct thread ID but no turn ID. A wrong turn ID also reproduces it. Both
   errors remain in the raw trace, yet the overload evidence lists only one and
   classification is `provider_unavailable`, `after_tool`, with no infrastructure
   failures. This violates conditions 1 and 2 and conceals an attribution defect
   in a fresh, single-turn runtime. Reconcile every retained error notification;
   an unattributable error must prevent the exception. Add missing/wrong-turn
   cases both during execution and at the last drain.

2. **Major M2. A foreign-turn item can falsely reconcile an unconsumed summary.**
   The final A3 decision at `live_runtime.py:1013` trusts reconciliation whose
   delivered-item table (`live_runtime.py:628`) accepts standalone items without
   checking their observer, thread, or turn identity. In contrast, output
   collection rejects a wrong-turn item at `live_runtime.py:529`.

   Reproduction: after an answered `READ` and a matching failed overload, queue
   two notifications for the last drain. First, `item/completed` on the correct
   thread but `turnId: "wrong-turn"`, containing
   `{"id": "unconsumed", "type": "agentMessage", "text": "Evidence never consumed"}`.
   Second, a matching `turn/completed` for the actual turn, with the same failed
   status/error, `itemsView: "summary"`, and that exact item in `items`. The item
   is treated as a delivered repeat, `queue_reconciled` stays true, and A3 grants
   `provider_unavailable`. Both adapter and exported `observer_outputs` are
   empty. The raw evidence survives, but the output disappears from the
   normalized record and condition 4 incorrectly passes. Restrict repeat
   matching to correctly attributed, consumed completions; reject this sequence
   while retaining the valid exact-repeat case.

   I reproduced M1 and M2 through the scheduler, registry, verifier, and scored
   export. Each fake root completed all 12 assignments with no holds, charged
   the affected attempt its 75,000-token test reservation, recorded a 600-second
   study pause, and exported it without quarantine. Running both sequences
   against the adapter extracted from `781aee7` instead returns
   `infrastructure_incomplete`. These older attribution gaps become unsafe
   exclusions when A3 removes the post-tool rejection.

3. **Minor m1. Newly sealed plans still describe the pre-A3 policy.**
   `live.py:251` retains
   `server_overloaded_before_any_tool_request_and_output_is_provider_unavailable`;
   `_assemble()` copies it into each top plan at `live.py:618`. Reproduction:
   build a root under A3 and inspect `execution_policy.provider_overload_policy`.
   Update this plan description to identify A3's predicates. This need not
   change the protocol JSON or invalidate the existing study.

The ordinary A3 path correctly requires the matching non-null turn error,
exactly one scoped non-retrying overload notification, sent responses, unique
call IDs, reconciled receipts, an audited world, and confirmed cleanup. Missing
receipts remain recorded without independently failing an attempt. The pilot
replay, ordinary pre-tool overloads, output-bearing overloads, bounded usage,
pause recovery, and mixed before/after-tool three-per-hour holds pass the focused
checks. The silent-stall predicate is unchanged. M1 and M2 prevent claiming
conformance for every retained event.

The existing-test edits reflect A3 rather than weakening the same contract:
post-tool/output rejection cases move to positive A3 cases with separate
negative-condition coverage; duplicate overloads and a null turn error now
correctly fail. The new negative cases omit the attribution sequences above.

Freeze verification passed. A sealed test root carrying the actual `211d0a5`
pilot-era implementation hashes was refused at `live.py:2209` before any runtime
factory call. All five changed source files are execution-gated. Both protocol
JSON copies are byte-identical to the pilot commit. The optional field at
`src/swarm_auth_bench/peer_reporting/live_archive.py:20` leaves P1 summaries
unchanged when the original payload has no `overload_stage`; I compared the old
and current functions directly.

Validation: **106 focused checks passed**. These cover `test_live_a3.py`,
`test_live_overload.py`, `test_live_stall.py`, `test_live_astra_r3.py`,
`test_live_runtime_v11.py`, the revision 5 offline study/three-root chain, and
individual P1 archive and ordinary missing-usage regressions. The real-template
chain built and verified the revision 5 study and its 72/612/36-row
pilot/grid/anchor plans. Scratch reproductions are under `.local/`. No
model/provider calls, live runs, guest access, full-suite execution, code edits,
or specification edits occurred. The coordinator's full-suite gate and the
user's plan authorization remain outstanding, after M1 and M2 are fixed.
