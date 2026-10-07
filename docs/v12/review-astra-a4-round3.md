Verdict: **NO-GO for the real-root sequence until finding 1 is fixed.**
The original 17-byte partial-write reproduction is resolved. A different
interruption case still permits completion without a durable declaration.

Reviewed `a3c683c`, merged at `0b81e74`, against section 8.2.1.
Code references below are under `src/swarm_auth_bench/`.

The fsynced 17-byte write followed by either a short return or `OSError` now
resumes. Repository cases cover one byte, halfway, the missing final newline,
and the full fsynced line. Sixteen independent write-cut cases and six
interruptions during recovery also resume. Verify and export reject incomplete
repairs, then pass after completion with exactly one declaration and the original
checkpoint bytes preserved. The prior partial-line finding is resolved.

1. **Medium, blocking: an already-complete visible line skips fsync on retry.**
   `peer_reporting_v11/ledger_repair.py:611`, `:626`, `:628`;
   `peer_reporting/live.py:498`.
   Independently wrap `write(encoded)` to write the whole line, flush it, then
   raise `OSError` before fsync. The prepared record remains, and the full line
   is readable. Rerunning returns success and replaces the record with
   `complete`, but tracing fsync by the journal's file identity records **zero
   journal fsyncs**. Both truncation and append are skipped when `tail == intended`;
   closing the journal supplies no durability barrier.

   Verify/export pass while those bytes remain visible. In an explicit scratch
   simulation of losing only the unsynced journal tail, both reject, and repair
   refuses because a record for the same corrupt hash already exists. This is
   a missing durability guarantee, not an observed physical power-loss test.
   Fsync the recovered full declaration before completing the repair, and leave
   it prepared if that sync fails. Add the full-write-before-fsync interruption
   case; the current full-line test fsyncs before injecting its error.

2. **Low: blanket prepared-record tamper refusal is not enforced.**
   `peer_reporting_v11/ledger_repair.py:368`, `:377`.
   Change the stored declaration's `wall_time`, recompute its event hash, and
   reseal the prepared envelope. Recovery, verify and export succeed with the
   unchanged fixture binding. They also succeed with a forged partial tail
   matching that replacement declaration. This changes declaration metadata,
   not the approved ledger or checkpoint. Document this limit if retaining the
   local, unauthenticated prepared envelope; do not claim every alteration refuses.

With the prepared envelope unchanged, nonprefix tails, a different record's
prefix extending past its first differing byte, extra complete records, and
extra bytes after the declaration all refuse without evidence changes.
Changed bindings, bound prepared fields, invalid seals, and altered/missing
retention also refuse. A prefix shared with another record is indistinguishable
from a legitimate short write and is accepted under the stated byte-prefix rule.
Ordinary readers remain strict; 80 pre/post-change differential comparisons agree.

The trust-boundary paragraph correctly makes publication and user approval
external prerequisites. Git equality is checked by the binding builder;
repair itself checks commit syntax and bound evidence, not Git provenance.
The `8cbe359` fixture still matches the specified hashes, byte 36,838 and mask
`0x20`. A synthetic repair, fake-environment cleanup, verify/export and successor
preparation pass, preserving the interrupted start as consumed, unresolved,
unscored and excluded from the successor. Actual guest cleanup and the
577-consumed/71-unstarted counts require the prescribed real-root checks.

Validation: 211 focused repository tests passed; one Windows storage test
was marked as an expected failure. Scratch drivers, XML and durability observations are in
`.local/a42-review/`. No full suite, model/provider calls, live runs, guest access,
tracked code/test/spec edits, push or merge. Only this LF report is committed.
