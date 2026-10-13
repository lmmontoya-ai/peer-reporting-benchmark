# Q2c second confirmation review: GPT-6-Astra (xhigh)

Verbatim final reply to the Q2c second confirmation brief, reviewing `173594b..dfe3fe9` at
the merge `dfe3fe9`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Reviewed `173594b..dfe3fe9`. One MAJOR defect remains.

- **Finding 1: FIXED.** The usage swap and resealed packet-index reproductions now refuse both gate and main before runtime creation.
- **Finding 2: PARTLY FIXED.** Final-entry interruptions followed by reconciliation now block main when no stop exists. An earlier session’s stop still permits admission after later interruptions.
- **Finding 3: FIXED.** Token-limit and wall-limit stops permit main. STOP combined with a `preflight_failed` lane also permits main; the ordinary failure alone refuses.

Approval scope, item 2: no derived-field omission found. All 2,270 tested leaf-value changes changed the approval hash, covering screening rows, sources, measures, decision, caps, W and cost. The offline builder refused a retained approval for inconsistent screening inputs. Both execution-boundary checks passed.

Stop timing, item 3: the required sequence fails, as detailed below. All eight listed reasons originate from controls applying to the whole root or the plan’s limits. Ordinary failure reasons remain excluded.

Finished status, item 4: `archived` correctly represents a terminal attempt, including cap endings, ineligible episodes and provider errors. Reconciled interruptions remain `incomplete_interrupted`. No additional status mismatch found.

**MAJOR. Historical stop evidence permits main after subsequent interrupted work.**  
`queue_plans.py:41` scans every journal session and accumulates `deliberate` without accounting for later execution.

Reproduction: record STOP, remove it, resume the gate, interrupt both lanes’ final entries, then resume solely for reconciliation and retain a fresh receipt. The verified root contained **46 archived entries, two `incomplete_interrupted` entries and zero unreconciled starts**. Its two original stop records remained unchanged; no new stop was recorded. Nevertheless, gate prerequisites accepted the root and main admitted **two fake sessions**. The supplemental reproduction (`test_confirmation_consistency.py:75`, in system temp) failed its refusal assertion.

Minimum fix: make stop qualification order-sensitive. A stop superseded by later resumed and interrupted work must not satisfy Q-C9; require a qualifying stop afterward.

Required regression: exercise that complete sequence and require main refusal before runtime creation. Then record a new deliberate stop and verify admission succeeds.

All 120 targeted repository tests passed. The supplemental approval check passed; the stop-order check reproduced the defect. The worktree remains clean at `dfe3fe9`.

**NO-GO**
