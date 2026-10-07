Verdict: **NO-GO for unguarded use on the real root.** The committed fault is
repairable, and the implementation meets A4's three mechanical conditions.
Those conditions do not establish that the input is genuine evidence. Add an
independently approved hash binding before applying this evidence mutation.

Reviewed `8cbe359`, merged at `e7ebd49`, from `251781b`, against specification
revision 5 section 8.2. References to `ledger_repair.py` and `live.py` below mean
`src/swarm_auth_bench/peer_reporting_v11/`.

1. **High, blocking: fabricated corruption can authorize a rollback.**
   `ledger_repair.py:282`, `ledger_repair.py:295`, `ledger_repair.py:299`;
   `docs/v12/spec.md:598`. The command trusts the seal supplied inside the
   input and the locally retained journal. Neither proves the ledger's
   pre-corruption state. Reproduction: admit and journal a reservation; save
   its active ledger; call `BudgetLedger.observe(..., None)` without appending
   its journal observation, the legitimate crash window. Replace the ledger
   with the saved bytes after flipping `notifications` to `notiFications`.
   Repair succeeds, returns the reservation to active, and removes the unknown
   notification. Identity and journal are unchanged except for the repair's
   appended declaration. Verify and export both succeed.

   Other successful probes changed `admitted_at` while keeping the journal
   unchanged, and lowered a settlement from 1,700 to 1,600 after editing and
   rehashing the uncheckpointed journal tail. A changed ledger ID plus a
   matching edited creation record also passes. Changing that ID without
   changing the journal correctly fails. These are operator-fabricated inputs,
   not accidental double-bit recovery or a SHA-256 break. The amendment's
   stronger anti-rewrite claim needs an external trust anchor. Require an
   approved record binding the root/lane, corrupt and restored byte hashes,
   identity, and journal checkpoint. For this incident, compare against the
   already committed evidence before mutation; a fresh self-sealed record or
   arbitrary `--approval-text` is insufficient.

2. **Medium: an interrupted repair has no supported completion path.**
   `ledger_repair.py:326`, `ledger_repair.py:328`, `ledger_repair.py:288`.
   Inject `OSError` at `journal.append("ledger_repaired", ...)`. The retained
   bytes and repair record exist and the ledger has already been restored,
   but verify rejects the missing journal declaration. Retrying repair refuses
   because the ledger already verifies. Cleanup also begins with verification,
   so this blocks the proposed recovery sequence. Failure before replacement
   similarly leaves an unmatched record that prevents retry. This fails closed,
   but a disk error can strand real evidence. Add resumable completion of a
   prepared repair, checking its exact retained bytes, target bytes, and
   unchanged journal checkpoint before completing the declaration.

3. **Medium: successor verification skips prior repair evidence.**
   `live.py:1520`, `live.py:1594`; A4 integration at `live.py:1921`.
   After successful repair, delete its retained `.corrupt` file. Verifying
   that root correctly fails. Nevertheless, `prior_root_ledger`,
   `prepare_live_root(..., prior_roots=[root])`, and
   `verify_live_root(successor, prior_roots=[root])` all succeed. The latter
   reports the consumed ledger checked and no repairs. Reproduction uses the
   offline A4 fixture and a newly built successor, without editing the repair
   record or journal. Prior-root traversal checks ledger history but never
   calls `verify_root_ledger_repairs`. Recheck repair trails wherever prior
   roots are accepted, so proceeding to a successor cannot conceal missing
   retention evidence. This probe did not demonstrate lost consumed starts.

The candidate search has a sound exhaustive partition (`ledger_repair.py:125`,
`:130`, `:154`): repair the final LF, change the seal property, or change payload
bytes under an unchanged seal property. Hash filtering does not require damaged
JSON to parse. Canonical serialization and ordinary ledger validation follow.
Zero candidates and the injected multiple-candidate case refuse before writes;
the latter is tested before journal comparison. The ordinary two-bit,
noncanonical, malformed, and journal-disagreement probes refuse.

Successful records retain the required bytes, hashes, bit, approval, reason,
and journal declaration. Historical verification (`ledger_repair.py:198`)
checks each retained file, reconstructed target, identity, exact bit, and the
journal prefix preceding that repair. Tested missing or altered evidence
blocks both verify and export. The same corrupt hash cannot be repaired twice;
two distinct genuine corruptions produce independently checked records.
Deleting the first retained file after the second repair still fails.
An independent subprocess attempting an ordinary ledger update during candidate
search waited for the ledger lock and preserved its update afterward.

The change to `src/swarm_auth_bench/peer_reporting/budget.py:44` and `:167`
preserves ordinary validation order, checks, and messages. The helpers repeat
seal verification already performed by `read_sealed`; they add no repair path
to ordinary opening. All 80 focused budget/history regressions passed, and 136
pre/post-refactor differential cases returned identical values or exception
types/messages. I found no P1/N100 semantic regression.

The real fixture yields exactly byte 36,838, mask `0x20`, restoring `F` to `f`.
Its 837 journal records reconstruct 100 settled reservations and one active
reservation holding 250,000 tokens with 52,209 observed. An offline synthetic
repair followed by `reconcile-cleanup` retained the interrupted start as consumed,
ineligible, and unresolved; subsequent verify/export accepted the historical
repair. Once the findings are addressed, the real sequence should verify the
repair, reconcile that start, then confirm 577 consumed and exactly 71 unstarted
assignments in the successor. The four-file fixture cannot verify the entire
real root or guest cleanup.

Validation: 38 A4 tests, 80 budget/history tests, and 12 independent probes
passed. Driver and XML evidence remain in `.local/a4-review/`; the differential
probe contains the 136 comparisons. No full suite, model/provider call, live
run, guest access, tracked source/test/spec edit, push, or merge occurred. Only this
report is committed with LF endings.
