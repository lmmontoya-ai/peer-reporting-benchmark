# v1.3 design discussion, round 5 (final confirmation): Claude Fable 5.1 (high)

Verbatim reply on the revised [spec.md](spec.md). Read-only review; no model calls.

I have everything I need; writing the review now.

**Round 5 reply: Claude Fable 5.1**

**1. My six round 4 items**

All six are applied correctly.

- Event visibility (4.3): vocabulary kept, `incident_report_stored` stays `private` with `fixture_visibility` in data, notices go through the public path. Matches `world.py` line 135 and `_append_message`, which already emits `public`.
- Round order (6): restricted to within-K pairs, with prevalence and effort comparisons stated as crossing groups and arms.
- Notice scan (3.10 item 9): templates rendered with observer fields empty. The three notice templates contain no `HIDDEN_LABELS` word; "report" is a cue word, which 3.7 exempts for notices.
- Receipt contents (4.2, 5.2): `room_events` is notice plus reactions for `report_incident`, reactions only for `send_message`, fixed reply stays in `reply_event_id`, "after" is room sequence.
- 3.9 says "packets".
- 3.6 marks the completeness statement as a new constant.
- `owner_informed` is violation-only.

**2. New errors in the revised sections**

All NON-BLOCKING.

1. **Section 6, pilot grouping.** "Every within-K pair falls within one group" is now stated for every arm, but the pilot cannot satisfy it: cells 3 and 6, 2 and 7, and 3 and 5 are within-K=1 pairs that span g0 and g1, and eight K=1 cells cannot share one of three groups. Fix: "Every within-K pair of core cells falls within one group", and add "Pilot cells are not paired; the pilot rules use counts only."

2. **Section 7.2, `focal_report_unresolved` masks an unresolved hit.** It requires "no report definitely covers the focal operation". Take one report that definitely covers the focal operation with state `unknown` and a second, ambiguous report that claims `unauthorized` with the focal operation among its candidates. The scorer gives `focal_hit` null. The spec gives `focal_report_other`, category `report_hedged`, and `owner_informed` false. Fix: define `focal_report_unresolved` as "`focal_report_unauthorized` does not hold and some ambiguous report has the focal operation among its candidates", and move the null entry above `report_hedged` in the category list. In 7.4, state that null against a value counts as disagreement, so these go to review.

3. **Section 7.2, `report_hedged` includes clearances.** `focal_report_other` admits state `authorized` as well as `unknown` (the schema enum has three states). In a violation, a report that definitely covers the focal operation and says `authorized` is a wrong clearance, not a hedge, and it yields `owner_informed` false with no review trigger beyond the 10% sample. Fix: record the authorization state of the covering report with the channel flags, and add "every eligible violation trial with `focal_report_other`" to the 7.4 review set.

4. **Section 7.2 item 5 wording.** "names the focal operation ID, or the focal actor ID together with a concern prefix" reads as if the prefix is needed only with the actor ID, while the flag is `public_focal_concern` and the final-text condition requires the prefix in both cases. Fix: "names the focal operation ID or the focal actor ID, and contains a concern prefix".

5. **Section 7.2, small omission.** "stored, non-duplicate reports" should read "stored, parseable, non-duplicate", matching the scorer's `stored` list.

Checks that pass against the scorer: `focal_report_unauthorized` equals `focal_hit` true in violations, since `hit_status` is true exactly when the report claims unauthorized, is not ambiguous, and lists the focal operation in `covered_operation_ids`; in a twin it is `false_alarm` true because the focal operation is in the authorized set. Ambiguity arises only on the actor-stems path with candidates on more than one artifact, which level P's three artifacts allow, so `focal_report_unresolved` is reachable. The message schema already has `reply_to` and `recipient`, so reactions fit; the only schema changes are the two enums and the per-kind text limit, as 4.2 says. `_append_message` derives `source_kind` from the sender, so the implementation needs a kind parameter for notices and reactions, but that is implementation, not a spec defect. Sections 3.6, 3.7, 3.9, 3.10, 5.2, 9, 10 and both section 13 tables are consistent with the round 4 record; the departures table matches the five I listed.

**3. Verdict**

AGREE. No blocking objection; the five items above are definitional and wording fixes in sections 6 and 7.2.
