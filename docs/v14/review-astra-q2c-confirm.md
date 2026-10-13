# Q2c confirmation review: GPT-6-Astra (xhigh)

Verbatim final reply to the Q2c confirmation brief, reviewing `0380a56..aeba1ac` at the
merge `aeba1ac`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Reviewed `0380a56..aeba1ac`. Three MAJOR defects remain.

- **R1: PARTLY FIXED.** The original resealed-counts/choices/approval reproduction now refuses both gate and main before runtime creation. Other evidence-binding gaps remain.
- **R2: PARTLY FIXED.** An unrun gate now blocks main. Completion and deliberate-stop recognition still have defects in both directions.
- **R3: FIXED.** A later neutral review preserves the sizing record and main planning. Omitted P3 submissions and unresolved P3 disagreements correctly refuse through the normal verification path.

Answers to items 2–5:

2. **R1 coverage:** incomplete. Per-episode usage and packet-index bindings can still be changed and resealed without execution verification refusing. Both execution-boundary checks pass; the new module and its execution imports reach no scoring or review module.

3. **Stop semantics:** ordinary soft/hard stops, including external files, cutoffs and deadlines work before and during execution. Unrun gates and unopened lanes refuse. Ordinary holds and interruptions refuse while work remains. However, final-entry interruptions can be accepted after reconciliation, and some deliberate stops are refused, as detailed below. Multiple roots were checked, including six non-abandoned gate roots. Abandoned roots are ignored; an entirely abandoned gate population does not qualify.

4. **Tool-cap change:** this repairs a real verification defect. Runtime already used the sized cap; the missing entry field made verification compare it against the fallback of 300. An archived episode confirmed approved/runtime/entry caps of **220**, with verification failing under the old entry shape and passing under the new one. Earlier-level byte guards passed, and entry comparisons against `0380a56` matched for earlier levels and Q compatibility, smoke and screening.

5. **Q-C8:** the common token sample, sequential phase-max time estimate, double censoring and normal credibility-inventory behavior conform. The execution-check requirements remain unmet by the findings below.

Remaining findings:

1. **MAJOR — R1 still accepts sizing records that disagree with retained evidence.**  
   `queue_sizing_evidence.py:69`, also lines 145 and 183–193.

   Reproduction: swap the usage objects of a neutral and P3 episode in the same lane. The approved aggregate measures remain identical. Execution verification accepts the resealed record; offline verification rejects it.

   A second reproduction starts with a retained P3 disagreement that correctly invalidates the original record. Replace the eight P3 packet hashes in a copied, resealed packet index and update the sizing source reference. Execution verification accepts again because the inventory no longer recognizes the retained P3 submissions. Primary evidence, submissions and approval remain unchanged.

   Minimum fix: authenticate the complete screening-input projection and packet/source identities, either through the retained approval or execution-safe comparison against primary evidence. Do not derive the mandatory inventory from unchecked packet hashes.

   Required tests: both reproductions must refuse gate and main before runtime creation.

2. **MAJOR — R2 accepts an interrupted gate once every entry has been consumed.**  
   `queue_plans.py:38`.

   Reproduction: interrupt each lane’s final entry after its journaled start, then resume solely for reconciliation. The resulting root contained **29 archived and two `incomplete_interrupted` entries**, no unstarted entries, and outstanding cleanup debt. No deliberate stop was recorded. Nevertheless, `gate_prerequisites(require_execution=True)` accepted it.

   Minimum fix: distinguish completed entries from consumed entries. Reconciliation must not turn `incomplete_interrupted` into evidence of normal completion.

   Required test: interrupt final entries, reconcile without a deliberate stop, and require main refusal before runtime creation.

3. **MAJOR — R2 rejects valid deliberate stops when lane halt reasons differ.**  
   `queue_plans.py:40`.

   Reproduction: one lane fails preflight while an explicit `STOP` ends the root. Another lane’s receipt-verified close records `soft_stop`, but the failed lane retains `preflight_failed`; main is refused. A separate reproduction cleanly reached `collection_token_limit`, closed both lanes with no active or unresolved reservations, and was also refused.

   Minimum fix: recognize the runtime’s deliberate terminal conditions and root-wide stop evidence, while preserving checks for interruptions and unresolved execution.

   Required tests: budget-limit stops and explicit stops combined with an earlier lane failure must qualify; ordinary failure holds and unfinished interruptions must still refuse.

All three added regressions and 69 other targeted tests passed. Supplemental checks reproduced the defects above. The worktree remains clean at `aeba1ac`.

**NO-GO**
