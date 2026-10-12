# Q1 code review: GPT-6-Astra (xhigh)

Verbatim final reply to the Q1 review brief, reviewing `b0476d2..899741a` at the merge
`7b3b01f`. Read-only review; no model calls. Links to local files are reduced to their
file names.

Reviewed `b0476d2..899741a` at `7b3b01f`. All **1,114 selected offline tests passed**, including both execution-boundary checks. Independent checks reproduced four findings. The worktree remains clean; no model/provider calls, VM access, commits, pushes, or merges occurred.

For the six requested checks:

1. **Earlier levels:** Fixture, tool, cap, and replay guards pass. Baseline v1/v2 tool manifests and retained receipt bytes match. However, revision 7 rejects a valid revision 6 sealed study, as described in F1.
2. **Generator and verifier:** Namespace, counterparts, roles, grants, answers, distinct locators, focal actor/field uniqueness, and both room schedules checked out. Closing posts occur at positions 38 and 3. All ten verifier checks passed their tests. Fifteen additional malformed fixtures were refused; I found no fixture that passed verification while violating those generation rules. An 8,976-item sweep found no size violations.
3. **Pre-v property:** Passed the 24 randomized counterpart-sequence tests and code inspection. Earlier posts/reports neither fire nor consume reactions. `read_channel` exposes only released or observer-posted messages.
4. **Tools and world:** Normal serving, envelopes, canonicalization, replacement, idempotency, lookup scope, replies, quotas, notices, and closure passed. Receipt lifecycle validation and one input restriction have defects, F2 and F4.
5. **Output budget:** Generated results fit the conservative bound. Its serializer differs from the client serializer, causing pagination to violate the longest-page requirement, F3.
6. **Text rules:** Scheduled posts, reaction templates rendered for every roster actor and fallback, and notices with empty observer fields pass the required scans.

Sol’s interpretations:

- **Queue length 4 across offline combinations:** Accept. Section 3.1 expressly permits combinations subject to its two restrictions; phase selection belongs to the study builder.
- **Legacy truth projections:** Accept for Q1. They retain the required keys and preserve the specified counterpart differences.
- **ASCII-escaped budget serialization:** Accept as a conservative size bound, but reject as the pagination contract while the client uses a different serializer.
- **Three-sentence report description:** Accept. The specific instruction to inherit level P’s description takes precedence over the general one-sentence rule.

Findings, most severe first:

1. **BLOCKER F1. The revision bump breaks verification of existing sealed studies.**  
   Location: `__init__.py:4`, through `collection.py:289` and `collection.py:394`.

   Reproduction: Build and verify a study using `b0476d2`, then verify the unchanged directory using the review worktree. The 405-fixture study passes under revision 6 and fails under revision 7 with **407 errors**: manifest fixture references, manifest seal, and every regenerated fixture differ. `_study()` implicitly uses the new fixture revision instead of the recorded protocol revision. `live.verify_sealed_study()` inherits this failure.

   Minimum fix: Pass the recorded protocol revision explicitly during legacy study regeneration. Keep revision 7 for Q.

   Required test: Retain a revision 6 sealed study and verify it unchanged under revision 7. Same-version regeneration tests do not cover this regression.

2. **MAJOR F2. Receipt replay trusts request status without validating the corresponding lifecycle.**  
   Location: `queue_world.py:364` and `queue_world.py:376`.

   Reproduction: In an exposed, open world, complete `read_channel`. Change its archived status to `rejected`, replace its receipt with an enveloped `closed` error, update the result event, and recompute hashes. `audit_state()` accepts it despite unchanged evidence showing that the request was admitted and the world never closed. It also accepts a completed second request while the first remains `held`.

   Minimum fix: Derive permissible statuses and rejection reasons from lifecycle events and admission/exposure state. Enforce dispatch ordering across held requests.

   Required test: Reject both inconsistent archives after hashes are recomputed, while retaining valid crash-prefix acceptance.

3. **MAJOR F3. Pagination measures different JSON bytes from those sent to the client.**  
   Location: `queue_schema.py:141` and `queue_world.py:177`. The inherited wire serializer uses `ensure_ascii=False` at `peer_reporting/live_runtime.py:135`.

   Reproduction: Store three posts containing `"\U0010ffff" * 1000`, then call `read_channel` with `limit=16`. It returns one message. The complete three-message envelope occupies **12,693 client-serialized bytes**, below `B=24,000`. Section 4.4 requires the longest fitting run. Extra pages also consume tool requests.

   Minimum fix: Use the actual wire serializer for pagination, or explicitly make Q wire serialization match the budget serializer.

   Required test: Check maximal pages against captured fake-transport wire text, including non-ASCII and escaped characters. The current pagination test incorrectly requires one message for this case.

4. **MINOR F4. ID restrictions also constrain the report’s `action` text.**  
   Location: `queue_schema.py:31`.

   Reproduction: An otherwise valid report with `action='change "payee_account"'` is refused with `action: pattern`. `_ids()` classifies every 100-character string schema as an ID. Section 4.3 preserves level P limits for other text fields.

   Minimum fix: Apply the ID pattern to identified ID fields, leaving `action` under its inherited text constraints.

   Required test: Accept quoted and non-ASCII action descriptions within 100 characters while continuing to reject those characters in IDs.

The reproduction scripts are the reviewer's temporary `q1_revision_consistency.py` and `q1_review_consistency.py`. All scratch data is in system temp.

Separate spec comments: Explicitly exempt `report_incident` from the one-sentence description rule, and pin the Q wire serializer in section 4.4. Neither clarification removes the current findings.

Release-blocking items: F1, F2, and F3.

**NO-GO**
