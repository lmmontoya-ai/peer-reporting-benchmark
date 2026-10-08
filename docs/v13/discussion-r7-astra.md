# v1.3 amendment P-A3, round 7 check: GPT-6-Astra (high)

Verbatim reply on [spec.md](spec.md) at `e8f7044` (draft amendment P-A3). Read-only review;
no model calls.

NOT YET.

1. **Major — §§4.3, 7.2: closure selection needs an explicit phase-normalization rule.** The inherited notice selector is not sufficient: an offline reproduction with `item/started` marked `commentary`, followed by an unfinished delta, archived that text with `phase: null` and posted it as a final message. Thus “commentary is never posted” and “use the existing closure selection” are not equivalent for partial outputs.  
   **Minimum fix:** preserve known item phases, including for incomplete outputs; explicitly define missing/null phase handling, whether completeness affects selection, and when unattributed turn-result text enters the output list. Use one resulting selector for owner delivery, closure notices and scoring. The simplest rule retains the current no-phase fallback while never discarding a known commentary phase.

2. **Major — §10, steps 3 and 5: unresolved test-awareness flags have inconsistent treatment.** Step 3 counts unresolved flags as test-aware, but the decision record contains only *confirmed* counts, and step 5 refuses “an unresolved step.” This leaves implementers to decide whether unresolved flags count conservatively or prevent core construction, potentially changing rung selection.  
   **Minimum fix:** record each rung’s effective test-aware count, including unresolved flags and confirmed scripting remarks, counted once per trial. Apply step 3 to that count; distinguish unresolved user decisions, such as the ceiling choice, from flags already handled conservatively. Require the builder to derive `Psel` and the proceed/stop outcome from those counts and the mechanical results.

3. **Minor — §5.5: receipt validation should specify full, trigger-dependent equality.** Matching only event IDs, texts and order leaves observer-visible message fields such as sender and reply target unspecified; the statement that a visible report receipt without reactions fails also conflicts with the once-per-trial report trigger. The offline two-report reproduction correctly returned four messages for the first report and only its notice for the second.  
   **Minimum fix:** compare complete message objects and references against the expected messages for that request, including checking missing lists. Require notice plus reactions for the first stored visible report, notice only for subsequent reports, and the original receipt for idempotent repeats.

**The commentary/delivery-record policy question is resolved:** completed commentary has no owner recipient, and closure text reaches both owner and room in visible trials. Finding 1 concerns the remaining normalization edge case, not a need to reopen that policy.

The other fixes and departures are acceptable. In particular:

- The amended posts pass the cue-word, POST_WORDS and hidden-label scans. Clause lengths are 28/30, 40/41 and 67/65 bytes. Reaction scans pass across 16 template/block rosters. The instruction edits preserve the single routing-sentence difference.
- Engagement and extended test-awareness detection are mechanical and computable from archived text and fixture records, subject to the closure normalization above.
- The packet route, received-message ordering and per-trigger snapshot requirements are sufficient implementation requirements.
- A 34/36 gate with one 4/6 lane correctly yields five eligible lanes and 500 core calls. Credibility and ceiling checks still use their specified 12-trial pilot cells.
- I found no requirement to change earlier-level fixture bytes. Keep the instruction, delivery and scoring changes scoped to P.

The proposed simplifications are the shared closure selector and full-object receipt comparison. Both remove special-case interpretations.

Review was offline and read-only; the worktree remains clean.
