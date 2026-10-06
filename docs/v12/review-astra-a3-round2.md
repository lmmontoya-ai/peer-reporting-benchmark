Verdict: **GO** for `social_grid_xhigh`, `social_grid_low`, and
`social_anchor_xhigh` (612 grid trials plus 36 anchor trials). No open blocker,
major, or minor finding from this confirmation review. The coordinator's
full-suite gate and the user's separate plan-hash authorizations still apply.

Reviewed fix `8c13bbe`, merged at `2826bce`, against v1.2 section 8.1 and v1.1
section 10. Source references below are relative to
`src/swarm_auth_bench/peer_reporting_v11/`.

1. **M1 resolved.** `live_runtime.py:518` records every retained error before
   filtering by observer, thread, or turn. `live_runtime.py:714` requires the
   sole notification to be attributed, non-retrying, and identical to the
   failed turn's overload error. I independently repeated the missing-turn and
   wrong-turn `streamDisconnected` reproductions, both during execution and at
   the last drain, after an answered `READ`, then a valid overload. All four
   produced `infrastructure_incomplete`, `execution_check_failure`, no provider
   pause, and unresolved usage. Restart created no runtime and retained the
   hold. Both errors survive in the raw record; added visible output survives
   normalization and export. The checked-in 12-case matrix also covers
   wrong-thread errors and extra `serverOverloaded` notifications at both
   boundaries (`tests/v11/test_live_a3.py:77`).

2. **M2 resolved.** `live_runtime.py:629` builds repeat candidates from
   `result.events`, requiring observer, thread, and turn identity. Last-drain
   items cannot manufacture proof that the turn loop consumed them. I repeated
   the original foreign-turn standalone item followed by the matching actual
   turn's summary, both queued at close after `READ` and an overload. It now
   fails with `unreconciled items in turn lifecycle notification`, sets
   `queue_reconciled` false, holds admission, and stays held on restart. The
   standalone item and embedded text remain in raw evidence; unrelated valid
   output remains in the export. A matching-scope standalone arriving only at
   close also fails, while its text is additionally retained as observer output.
   Both failed attempts verify and export as ineligible execution failures,
   with null endpoints and no provider exclusion.

3. **m1 resolved.** `live.py:251` now describes A3's exact error count and
   attribution, matching failed-turn error, retry prohibition, tool/receipt
   reconciliation, cleanup, final drain, and allowed prior tools/output. A newly
   sealed plan contains that policy (`tests/v11/test_live_a3.py:147`). Both
   protocol JSON copies have the same Git blob, `213189cc4ffdab4ad857550d2e3692d1cc50ab46`,
   at pilot revision `211d0a5` and the reviewed merge.

No new findings. Independent attacks also tested:

- A sole overload error on a wrong thread, and one from another agent ID. Both
  failed and held, so rejection does not depend on a second error.
- A consumed foreign-agent standalone item followed by an exact summary repeat.
  It failed reconciliation and held.
- Two completed messages with the same ID and different text. The existing
  check at `live_runtime.py:587` rejected the contradiction; original normalized
  output and both raw payloads survived.
- A standalone item before `turn/started`, then its summary. The foreign-turn
  version failed. The correctly scoped version passed A3 and retained its output.
  This conforms to section 10: the turn-start response already bound the turn,
  and the loop consumed the exact item. Notification order alone does not make
  it unattributable.

Ordinary completed turns remain strict. Independent foreign-turn and
foreign-agent summary sequences and a missing-turn non-retrying error all
failed, held on restart, and exported without scoring. The changed delivered
set only narrows reconciliation; non-retrying errors now reach
`live_runtime.py:1027` even without attribution. The genuine completed guest
summary repeat still passes with its output preserved.

The pilot replay still yields `provider_unavailable`, `after_tool`, reservation
settlement, and a 600-second pause, followed by successful resumption. Pre-tool
overloads, the genuine completed-turn summary, the silent-stall rule and its
negative cases, and the mixed three-per-hour hold all pass.

Validation: the five focused modules (`test_live_a3`, `test_live_overload`,
`test_live_stall`, `test_live_astra_r3`, `test_live_runtime_v11`) initially had
133 passes and two failures. Both failures have archived `PermissionError:
[WinError 5]` replacing `budget-ledger.json`, the known Windows ledger race.
The retrying-error attempt itself passed; a later ordinary attempt hit the
ledger error. The other failure stopped before the overload under test.
Both passed on the targeted rerun with no code changes. Thus all 135 focused
checks passed across the initial run and rerun. Logs and JUnit records are
`.local/a3-round2/focused.*` and `rerun.*`.

Three additional checks passed (`.local/a3-round2/study.*`): the real revision 5
study build/verification and three-root replay chain, execution-freeze refusal
before runtime creation, and the execution/post-hoc import boundary. The chain
verified 72/612/36-row pilot/grid/anchor plans, including both requested grid
efforts, without live attempts. The unchanged protocol therefore requires no
study revision for these fixes.

Independent experiments: **16 passed**, including 14 held failures and two
positive controls, all verified and exported. Driver:
`.local/a3-round2/attacks.py`; results:
`.local/a3-round2/independent/attacks-results.jsonl`; sealed attempts and exports
remain under that directory's `experiments/`.

No model/provider calls, live runs, guest access, full-suite execution, source
or test edits, specification edits, push, or merge occurred. Only this report
is committed.
