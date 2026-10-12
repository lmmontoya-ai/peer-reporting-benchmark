# Q2c code review: GPT-6-Astra (xhigh)

Verbatim final reply to the Q2c review brief, reviewing `a27cca6..e1d879d` at the merge
`e1bae19`. Read-only review; no model calls. Links to local files are reduced to their
file names.

Reviewed `e1bae19`, covering `a27cca6..e1d879d`. **Three MAJOR findings block GO.** All 246 targeted repository tests passed; supplemental checks reproduced the findings below.

Findings, most severe first:

1. **MAJOR R1. Execution accepts sizing counts and choices that contradict retained evidence.**  
   `live.py:907` calls `queue_sizing.py:257`, which recomputes the decision from embedded copies without verifying their retained sources.

   Reproduction: start with selected lanes having `o = 4` and no ceiling choice. Alter the copied counts and codes to produce `o = 8`, insert ceiling choice `"a"` and a matching copied approval, and recompute dependent derived fields, seals and registration references. Supply the new root’s first receipt and a fresh execution authorization. Screening evidence and retained human submissions remain unchanged. Full offline verification rejects this record, but execution admits **two fake sessions** using it.

   Minimum fix: before admission, verify the retained submission and evidence bindings, including agreement with the retained approval’s measures hash, through an execution-safe module without scoring/review imports.

   Required test: consistently reseal altered counts, choices and approval while preserving primary evidence; both gate and main execution must refuse before creating a runtime. This is a section 2.1 item 4 defect.

2. **MAJOR R2. Main execution can precede any gate execution.**  
   `queue_plans.py:16` checks registration state and receipt presence. The registration becomes `finalized` during preparation.

   Reproduction: prepare and receipt a gate without running it, then build, prepare and authorize main. Main admits **two fake sessions**, while the gate has **zero starts and no execution authorization**. This violates section 7.1’s execution order.

   Minimum fix: before main admission, require receipt-verified journal evidence that gate execution has finished or deliberately stopped. Offline main planning can remain independent of the gate result.

   Required test: an unrun, prepared and receipted gate must not permit main admission. A finished gate must permit main regardless of whether it passes or its human coding is complete.

3. **MAJOR R3. Later neutral episode reviews invalidate a frozen sizing record and block continuation.**  
   `queue_sizing_posthoc.py:139` compares the supplied uploads with every submission in the episode packet directory, including neutral episodes irrelevant to credibility.

   Reproduction: validate an approved sizing record, then retain one valid neutral episode review in its existing packet set. The original record now fails with “every retained episode submission”. Rebuilding with that upload produces **identical measures and a `proceed` decision**, but a different record hash. A main continuation then fails at `queue_plans.py:49`, requiring an amendment and new screening.

   Minimum fix: restrict the mandatory submission inventory to the eight P3 credibility packets, while continuing to require every relevant P3 submission, including disagreements.

   Required test: adding a neutral episode review after gate preparation must preserve the original sizing record’s validity and permit main planning. Omitting a relevant P3 submission must still fail.

Answers to items 1–8:

1. **Screening measures:** pass. `r/e/o/a/t`, eligibility, missing assignments, coding completion, reached-only ranking and protocol tie-breaks behave correctly. Verified `observer_outputs` already includes room posts; an additional check confirmed all 34 flags were preserved.
2. **Credibility:** pass. The count uses the eight P3 episode reviews and applicable flag resolutions. Changing P3 reporting outcomes did not change lanes, caps or W.
3. **Decision rules:** pass in the offline builders. Two-, one- and zero-lane paths, permitted choices, ceiling pauses and luna-low attention selection are correct. R1 concerns execution accepting a replaced copy.
4. **Caps:** pass. Per-lane statistics, larger-lane selection, median, nearest-rank percentile, censoring, missing usage, fewer-than-six fallback, rounding and `generation_seconds` are correct. Attention uses study caps.
5. **Cost inputs:** arithmetic passes, including common token samples, missing timing, W = 60/80 and attention overlap counts.
6. **Record and builders:** receipt-bound re-derivation and altered-input refusals pass. Executed memberships, luna-low union groups and W populations are correct. R3 affects later builds; R1 and R2 affect execution.
7. **Execution boundary:** the requested import-boundary checks pass. No scoring/review imports were found in execution. Evidence binding still needs R1’s fix.
8. **Earlier levels:** targeted byte guards, goldens and level P pilot-decision checks pass. Changed files contain no CR bytes or non-ASCII bytes.

Judgments on Sol’s interpretations:

1. **Accept.** A common complete token sample keeps input, uncached input and derived cached-input cost coherent. Timing can use its separate available sample.
2. **Accept as a generation-time estimate.** Summing gate and main phase maxima respects sequential phases; required missing means correctly produce null.
3. **Accept.** Both crossed token stops should censor their respective statistics, and unavailable token totals should be omitted.

Spec comments, separate from findings: document these three arithmetic conventions explicitly. Section 11 step 10 should also distinguish the reported parallel generation estimate from elapsed wall time including cleanup.

The worktree remains clean at `e1bae19`. No full suite, model/provider calls, VM access, checkout commits, pushes or merges were performed. Scratch evidence stayed in system temp.

**NO-GO: R1, R2 and R3.**
