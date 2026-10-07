Verdict: **GO** for the prescribed real-root sequence: build the binding from
`8cbe359` with the user's approval, push before repair, then `repair-ledger`,
`reconcile-cleanup`, verify, export, and prepare a successor for the 71 unstarted
assignments. No remaining blocker or major found in this confirmation of `d98c71a`.

1. Finding 1, blocking durability failure: **resolved**.
   An independent stream wrapper wrote the entire declaration, flushed it, and
   raised before fsync. The first invocation left the repair prepared with zero
   journal syncs. On recovery, tracing `(st_dev, st_ino)` against the journal's
   file identity recorded a successful fsync before the completion write.
   Injecting failure into that journal sync left the record prepared; a later
   rerun synced it and completed with exactly one declaration. Verify rejected
   the prepared state and accepted completion.

   The sole completion write in `ledger_repair.py:633` follows a durability
   barrier on every permitted path. Fresh repairs and prepared repairs with an
   empty tail append, flush and fsync through `_Journal._append`. Strict partial
   prefixes are truncated and synced before the full declaration is appended,
   flushed and synced. Full tails take the new fsync branch. Failures at either
   partial-tail barrier also leave the record prepared and recover successfully.
   Nonempty tails with corrupt targets or missing retention refuse unchanged.

2. Finding 2, low metadata mutability: **accurately documented; retained limit**.
   Resealing a changed declaration `wall_time` still succeeds for empty, matching
   partial and matching full tails. Recovery, verify and export accept it while
   the approved binding, ledger bytes, identity and checkpoint stay unchanged.
   Section 8.2.1 now states this unauthenticated-envelope limit correctly. It is
   not a remaining blocker for the approved-binding sequence.

Validation: 126 focused repository cases and 27 independent scratch cases passed
across focused runs. The committed incident fixture still matches the specified
hashes, offset 36,838, mask `0x20` and 837-record checkpoint. A synthetic
repair/cleanup/verify/export/successor sequence kept its interrupted start
consumed, unresolved, unscored and excluded from the successor. Cleanup used an
explicit fake environment check. Actual guest cleanup and the real root's
577-consumed/71-unstarted counts remain operational checks for that sequence;
publication and the user's approval remain external prerequisites.

Scratch drivers, observations and test XML are in `.local/a43-review/`.
No full suite, model/provider calls, live runs or guest access. Only this LF
report is committed; no tracked code, test or spec edits, push or merge.
