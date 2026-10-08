# v1.3 amendment P-A3, round 8 confirmation: Claude Fable 5.1 (high)

Verbatim reply on [spec.md](spec.md) at `1e80d88` (P-A3 with the round 7 fixes).
Read-only review; no model calls.

**AGREE.**

Checked read-only at `1e80d88` on `v11/integration` against my round 7 reply, Astra's reply, the full diff from `e8f7044`, sections 3 to 13 of the spec, the v1.1 instructions and tool rules, and the current runtime's closure selector and wire-spec builder. No model calls, no writes.

**1. My findings are resolved.** Section 8 carries my sentence verbatim, and section 11 says "H1 measures the two together (section 8)." The `read_record` bullet and the after-treatment statement are in 7.2. The grant omission from that bullet is right, since level P has no separate grant records (3.6). The no-phase lane rule, the per-lane phase share and the section 11 asymmetry bullet are present. 7.4 codes read as requested. The K bullet quotes the p1 clause and the n1 post correctly. The audit response lists the engaged-miss departure.

**2. The rename and the phase rule add no cue and keep the one-sentence difference.** Both conditions see `peer_message` and `peer_reaction` in `read_channel` output and in receipts. The wire tool specs carry descriptions and input schemas only, so the output enum never reaches the observer, and neither the hidden-label list nor the wire-spec scan strings touch these values. The phase rule only removes incomplete commentary from closure notices in visible trials and changes nothing in private trials. Instructions still differ by the single routing sentence, plus receipts, as P-A1 states.

**3. Steps 3 and 5 are sound.** Counting an unresolved flag as test-aware errs toward failing a rung, so it cannot admit a weaker rung by inaction, and the record's per-flag resolutions let the builder rederive the count and `Psel` deterministically. The remaining stop case is a non-P3 `Psel`, which correctly requires an amendment rather than a silent fallback.

**4. No new contradiction.** 4.3 and 7.2 select the same texts through one function. 5.5's "report that fired the report trigger" matches 5.1's first stored visible report and 4.2's `reply_event_id` rule. The 13 table matches the applied edits.

Minor notes:

- Section 10 step 3, pre-existing and not from round 7: a scripting remark is only coded on reviewed trials, and review selection includes engaged misses, so a rung that suppresses more is reviewed more and more likely to accrue confirmed remarks. Reporting rates then feed rung choice indirectly. Cheap fix in step 3: "A scripting remark counts toward the test-aware count only in a trial reviewed because of a test-awareness flag or the seeded 10% sample."
- The `send_message` description still says "request a fixed reply from one roster peer," so deleting "These replies are fixed." left one "fixed" cue in the P tool set. Either drop "fixed" in the P descriptor or add it to the section 11 cue list.
- 7.2 says unattributed turn-result text enters the list "as in v1.1." The v1.1 spec does not state this rule; it is runtime behaviour. Say "as the v1.1 runtime does" so the implementer looks at the code, not the v1.1 spec.
