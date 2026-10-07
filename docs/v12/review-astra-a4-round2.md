Verdict: **NO-GO for the real-root sequence until the interrupted journal-write
case below is fixed.** The approved binding and prior-root fixes work. Recovery
still fails for an interruption inside the declaration append.

Reviewed `49be5c1`, merged at `b987132`, against section 8.2.1. Unqualified code
references below are under `src/swarm_auth_bench/peer_reporting_v11/`.

1. **Medium, blocking; prior finding 2 remains partly open.**
   `ledger_repair.py:559`, `ledger_repair.py:491`;
   `src/swarm_auth_bench/peer_reporting/live.py:467`.
   The declaration uses an ordinary append, which can leave a partial record.
   Reproduction: in the offline lane fixture, wrap the journal stream so
   `write(encoded)` writes and fsyncs `encoded[:17]`, then returns 17. The real
   append raises `OSError("short journal write")`. The ledger is already restored,
   retention and binding exist, and `repair-1.json` remains `prepared`. Remove
   the wrapper and rerun the identical repair command. It fails with
   `phase journal missing or corrupt: unterminated event at line 10`, before
   reaching resumable completion. Verify and export also fail. No changed
   approval, binding, or evidence was needed to strand the root.

   Make declaration persistence atomic or recoverable against the approved
   journal prefix and durable intended declaration bytes. Do not discard an
   arbitrary unexpected tail. Add an interruption test inside the write.
   The existing tests interrupt before append or before completion, missing
   this case. My 11 before/after operation-boundary cases all resume correctly;
   incomplete repairs fail verification, and all 22 corresponding target-byte
   or binding substitutions refuse without further evidence mutation.

2. **Low, documentation; clarify the external trust boundary.**
   `docs/v12/spec.md:636`, `docs/v12/spec.md:642`;
   `ledger_repair.py:240`, `ledger_repair.py:311`, `ledger_repair.py:466`.
   The builder checks equality with a local Git commit. It does not establish
   publication, repository authority, or who approved the binding. Repair
   checks the commit's format, not its provenance. Reproduction: change
   `admitted_at` from 10 to 11, reseal, flip the notification key, commit that
   evidence in a fresh local repository with no remote, and build its binding.
   Repair and verify succeed. A binding resealed with a nonexistent all-zero
   commit also succeeds. Dirty or uncommitted evidence is refused by the builder.

   These successes are outside the prescribed trusted-binding sequence, not
   bypasses of an unchanged genuine binding. Section 8.2.1 names public evidence
   and user approval but should explicitly call them externally enforced
   prerequisites. The trust anchor is the committed, pushed `8cbe359` evidence
   plus the user's approval of that exact binding. A seal and approval string
   alone provide no such authority.

Prior finding 1, fabricated rollback, is **resolved for that trusted-binding
sequence**. All four original probes refuse with the genuine committed binding:
rollback of unresolved usage, changed `admitted_at`, lowered settlement with a
rehash of the journal tail, and changed ledger ID with an edited creation record.
The original tests confirm those fabrications still satisfy A4's local candidate
and history checks. Additional mutations of the real fixture also refuse.
Other-root/lane/study bindings, an appended post-checkpoint journal record, and
replaying a completed binding with either restored or re-corrupted target bytes
all refuse. Refusals leave evidence unchanged.

Prior finding 3 is **resolved** at `live.py:1536`. Deleting or altering the prior
root's retained bytes or binding, including resealing an altered binding,
fails `prior_root_ledger`, successor preparation, successor verification, and
export. The failed preparation/export creates no destination.

The genuine fixture still yields byte **36,838**, mask **0x20**, and every pinned
hash in section 8.2.1, including the identity and 837-record journal checkpoint.
A binding built directly from `8cbe359` repairs a byte-faithful copy of its
ledger, identity, index, and journal, and the historical repair recheck passes.
Only absent root/phase-plan and registration envelopes were supplied through
test adapters. The ledger retains 100 settled reservations and one active
reservation with 250,000 reserved and 52,209 observed tokens.

A separate synthetic root completes binding creation, repair, cleanup
reconciliation, verify, export, and successor preparation. Its interrupted start
stays consumed, unresolved, ineligible, and excluded from the successor.
Cleanup uses an explicit fake environment check. The four-file incident fixture
cannot certify the real root's other lanes, guest cleanup, or the expected
577 consumed and 71 unstarted assignments. After fixing finding 1, those counts
must be checked during the prescribed real sequence.

Validation: 104/105 focused repository cases passed initially; the remaining
outside-Git case passed with Git discovery bounded above the scratch directory.
All 51 independent checks passed after correcting scratch fixture setup,
including the expected refusal reproducing finding 1. Driver and XML/log
evidence are in `.local/a41-review/`. No full suite, provider/model call, live
run, guest access, tracked source/test/spec edit, push, or merge. Only this LF report
is committed.
