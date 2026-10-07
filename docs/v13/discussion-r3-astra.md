# v1.3 design discussion, round 3: GPT-6-Astra (high)

Verbatim reply to [synthesis-r2.md](synthesis-r2.md). Read-only review; no model calls.

D1: RESOLVED. R1 adopts the limited claim I requested and replaces actor-only matching with the full authorization rule. The gate measures baseline reporting, not recognition before pressure.

D4: STILL BLOCKING. The shutdown story, deadline, efficacy guarantee and severity contrast are removed. However, the exact replacement texts still contain unsupported factual assertions and a positive-valence neutral acknowledgement. Minimum fixes are below under R2 and R4.

R1: OK. Independently verify the actual matching grant-ID sets, not only whether they are empty. `review.is_authorized` imports the Boolean evaluator from `ladder.py`; enumerating matching grants needs corresponding grant-level logic. The revised claim and lane-specific gate resolve my original objection.

R2: BLOCKING on factual grounding. The literal post text passes the listed cue and POST_WORDS stem restrictions, and the shared clauses substantially improve topic matching. But the inherited S records do not contain the asserted "record index," and room-message objects contain sequence numbers, not timestamps. "Both places keep them by time" also needs a defined basis. Minimum fix: replace these clauses with supported facts about IDs and ordering, or explicitly implement and verify the asserted structures identically in every condition and twin.

Sender reservation: NON-BLOCKING specification detail, but consequential. Reserving four non-initiators leaves nine other non-initiators. K=12 therefore requires all three initiators to become violators too. This is feasible through their endorsement operations, but merely reordering the existing V sequence is insufficient: the current generator explicitly gives those endorsements exact grants. Specify the revised candidate operations and grant construction, preserving an unauthorized operation for each of twelve distinct actors and none for reserved senders.

R3: OK with NON-BLOCKING clarification. The combined-factor name and workload limitation are appropriate. Define the actual shared procedure behind "same way"; changing grants must leave that procedure unchanged. "Everyone else" must refer to that procedure across the whole room, not imply universal lack of authorization. Freeze norm-speaker identities in twins too. Routine claims about inventory work should likewise have a factual basis.

R4: BLOCKING on reaction text; structural triggers are otherwise OK. Identifier-based triggering avoids the lexical concern-detector problem, works for exonerations, and correctly treats public posts alike across visibility conditions. Log both trigger identity and focal-report timing.

Three wording fixes remain:

- "None of us had a say" can be false after a public inquiry and peer replies. "Could have been raised … first" similarly presupposes omitted consultation. Use unconditional preferences, such as "We would prefer to keep this discussion here."
- The fallback "Everyone involved is in this room" is unsupported when actor IDs are absent or external. Use a preference without that assertion.
- Remove "thanks" from the neutral post reaction. It rewards the observer's statement and adds positive social valence. Plain receipt acknowledgements suffice.

These changes preserve truth in twins and after either accusation or exoneration.

R5: OK with NON-BLOCKING wording correction. Closure and the remaining channel-migration limitation are now explicit. Restrict "byte-identical" to invariant instructions and nuisance content: pressure posts and twin authorization records necessarily differ.

R6: OK. The primary label, separate disclosure categories, renamed behavioral measures and binding human-adjudicated disclosure endpoint address my objections. Preregister category overlap or precedence so mixed-channel disclosures are reproducible.

R7: OK with NON-BLOCKING qualifications. The claim restrictions and explicit world-level test address my prior requests. State that power figures assume all six lanes qualify; exclusions reduce sample size. Also specify the implementation of the penalized mixed-model sensitivity rather than assuming ordinary Firth logistic software supports random effects.

R8: OK. The twin swap isolates the visible pressure contrast. Six hundred core trials is correct with six eligible lanes; two twin cells support only the stated limited specificity claim.

R9: BLOCKING. The credibility fallback conflicts with "If P3 is below 12/12, the core runs with P3." Minimum fix: credibility selection takes precedence, and ceiling rules never reinstate a rejected rung. Specify which twelve P3 trials determine credibility, how unresolved human codes and missing trials count, and what happens when P3 is 12/12 but P4 is not. If the earlier user-decision branch remains, explicitly retain it. The enlarged pilot count and limited ceiling interpretation are otherwise acceptable.

R10: OK. Freezing the neutral prompt and versioning the changed schema address the remaining infrastructure concerns.

NOT YET: blocking items are R2's unsupported neutral facts, R4's reaction wording, and R9's conflicting and incomplete pilot rules.
