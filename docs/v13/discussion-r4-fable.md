# v1.3 design discussion, round 4 (spec confirmation): Claude Fable 5.1 (high)

Verbatim reply on [spec.md](spec.md) as first written. Read-only review; no model calls.

I've finished checking the arithmetic, the texts with the package scanners, and the code paths the spec references. Writing the review now.

**Round 4 reply: Claude Fable 5.1**

**1. Faithfulness to the agreed design**

The spec implements synthesis-r2 as amended by round 3. All of my non-blocking items are applied: the two neutral clauses, the stated V pools and grant construction, the visible sentence as a replacement, the post-word scope, the notice contents, reply requests firing the post trigger, the named credibility cell, and announced finish summaries. Astra's three blocking fixes are applied as asked. I found five deviations from synthesis-r2, none of which I object to, but three are not in the section 13 table and should be:

- **Norm speakers at K=1.** Synthesis-r2 R3 gave the three speakers matched routine lines at K=1. The spec instead keeps their standard second-round posts. This is cleaner and removes the unverifiable "before the others". Add a row to section 13.
- **Gate exclusion.** Synthesis-r2 excluded a failing lane from the primary tests. Section 10 step 2 excludes it from the core plan, so the lane does not run at all. This is a stricter, cheaper rule and a sensible one. Note it in section 13 so the change is visible.
- **Ceiling rule.** Synthesis-r2 paused only when P3 and P4 were both 12/12. Section 10 step 4 pauses on Psel alone and drops P4 from the condition, which answers Astra's "P3 at 12/12 but P4 not" case. The 24/24 lower bound is gone with it. Acceptable.
- **Sensitivity model.** World random intercept became world fixed effects. Mapped in section 13.
- **Neutral fallback reaction** lost ", for anyone who needs it." Fine.

**2. Errors**

Arithmetic. Everything checks. Each pool is 3 non-initiator operations plus 1 initiator endorse, giving 12 slots on 12 distinct actors across three disjoint actor sets. Grants are 3 + 12 + 12 + 17 = 44, with 17 being the 29 endorse operations minus the 12 V slots. Fixtures per template are 2 + 11 + 50 = 63. Trials are 12, 12, 66, 66, 300, 300 = 756, and 2,166 + 756 = 2,922 matches protocol.json. The gate is 6 per lane and 36 pooled. Each credibility cell is 12 trials. Pairs are 120, 40 and 20, twins 60 and 10. Group sizes sum to 10 and 11. Pool orderings, norm speakers V[1..3], and nesting from K=1 to K=12 are consistent. Every violator's other operation has an exact grant, including the initiators' initiate grants.

Texts. I ran every post, norm line, reaction and record field through the package cue, post-word, hidden-label and routine-only scanners, with a roster-style actor substituted. All pass. Byte differences are p1 −2, p2 −1, p3 +1. The notice and instruction sentences contain "report", which the spec exempts. Every text is true or a stated preference in every world: records have IDs, room messages and reports have IDs, the notice lists actor IDs so neutral reaction 1 is true, and the observer ID is literally `observer`, so the notice names the observer as the instructions promise. The lookup record's completeness statement differs from the code constant in one phrase ("each operation's request record" instead of "the named request record"). That is the right change for three request records, but the spec should say it is a new constant so no one reuses the old one.

Errors found, all NON-BLOCKING:

1. **Event visibility (section 4.3).** The spec says the stored-report event records `visibility` as the fixture value. The world's state audit at world.py:135 only accepts `controller`, `private` and `public`, so `visible` would fail validation. Fix: emit `public` in visible trials and `private` otherwise, or keep `private` and add a data field `report_visibility` holding the fixture value.
2. **Round order claim (section 6).** "Every pair of section 8 falls within one group" is false for the section 8 secondary K=12 vs K=1 pairs, which span g0 and g1 by construction. Fix: "every H1, H2 and twin `false_alarm` pair".
3. **Notice scan (section 3.10 item 9).** The closure notices embed observer text that cannot be checked at fixture time. Fix: "notice templates, with observer-supplied fields empty".
4. **Receipt contents (sections 4.2 and 5.2).** Section 4.2 defines the send_message `room_events` as reaction messages only, while 5.2 says reactions come "after any fixed reply". Fix: state that `room_events` lists the notice and reactions only, that the fixed reply stays in `reply_event_id`, and that "after" refers to room sequence.
5. **Section 3.9 wording.** "Fixtures differing in pressure differ only in the texts of the three sender slots" should say "packets", since parameters, truth and, under P4, the instructions also differ.
6. **`owner_informed` in twins (section 7.2).** The scorer treats `focal_hit` as not applicable outside violation trials, so in twins this endpoint can only be true or null. Say it applies to violation trials, or state the twin reading explicitly.

Feasibility. Reactions returned inline in receipts fit the one-turn runtime, since fixed replies already work that way. Closure notices are feasible: the runtime holds the final assistant text and finish summaries when it closes the world, so a world method can append them before the archive is written, and nothing is shown to the observer afterward, as the spec says. The lookup record carries the authoritative `registry_id` without a `grants` list, and both the rule evaluator and the review record check tolerate that. The new source kinds, the `room` sender and the `held` status all need the v2 schema version the spec declares. The scorer currently keys level-specific logic on level S and `build_instructions` takes no fixture parameters, so both need level P branches, but that is implementation, not a spec error. Inherited references to level S sections 3.2, 3.3, 3.5, 3.6, 3.7 and 7, v1.1 sections 3, 7 and 8, `_stable_order`, `incidents.CUE_WORDS`, `POST_WORDS` and `structured._CONCERN` are all correct.

Leaks. None found. Matching-grant counts are 1 for every authorized operation and 0 for every unauthorized one, so the lookup reveals only the verdict it is meant to supply. Grant IDs are hashes. No post names an actor or operation.

**3. Verdict**

AGREE. No blocking objection. The six items above are wording and one event-field fix.
