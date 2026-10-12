# Q2b confirmation review, round 3: GPT-6-Astra (xhigh)

Verbatim final reply to the third Q2b review brief, reviewing `cfc399a..6a4a844` at the
merge `1f2ec59`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Reviewed `cfc399a..6a4a844` at `1f2ec59`. One MAJOR issue remains.

- **R2: PARTLY FIXED.** The new producer exists and ordinary bindings work under Q-C5. Silent retries can still attribute another response’s usage to the focal request.
- **R5: FIXED.** Re-running the eight-version reproduction gives packet-verification counts `[1, 1, 1, 1, 1, 1, 1, 1]`, replacing exponential growth. The regression also confirms that each historical resolution and submission is checked once.
- **R6: FIXED.** Reversing unchanged uploads now succeeds with identical endpoints. Reviewer identity, content and role differences still matter.
- **Q2a R5: FIXED.** Six roots now require five ancestor audits instead of 31. A subsequent operation rejects changed ancestor evidence.

For R2, pinned-source inspection confirms that `FunctionCall` contains `call_id` and the app server forwards the item through `rawResponseItem/completed`. See the [item definition](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/protocol/src/models.rs#L1074) and [notification producer](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/app-server/src/bespoke_event_handling.rs#L1155).

The production runtime retains these notifications in controller evidence through `live_runtime.py:592`, which export verification supplies to scoring. Offline wire checks exercised that path and passed primary-evidence verification. Cross-turn interleaving, duplicate-item rejection, completion before focal delivery, and absence of a later response all behaved correctly.

**MAJOR R2: A silent retry can supply the wrong context measure.**  
Location: `queue_score.py:107` and `queue_score.py:115`.

The scorer assumes an unfinished focal response will emit an `error` before a retry completes. Codex 0.158.0 deliberately suppresses the first WebSocket retry notification in release builds. Consequently, the next completion need not belong to the response that emitted the focal tool call. [Pinned retry implementation](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/responses_retry.rs#L120).

Reproduction: the offline wire emits the focal `next_item` raw item and serves v, omits that response’s completion and retry notification, then completes a later response with 987 input tokens. Evidence verification succeeds. Scoring incorrectly returns `input_tokens_before_v=987`, reason `focal_request_response`, instead of null. The ordinary-response control returns 123 correctly. The reproduction remains in the reviewer's temporary `test_q2b_response_consistency.py`.

Minimum fix: retain an authoritative response/stream identifier with each tool item, or enforce and attest a transport with reliable retry boundaries. Return null wherever response ownership cannot be established.

Required test: cover the suppressed first retry through the production wire path, with distinct later-response usage, and require null for the unfinished focal response.

For the caches, I found no bypass in the supported operation paths. Production callers create fresh caches and share them only within an operation. Keys distinguish resolved paths and packet/upload context. Fresh-operation checks rejected altered historical resolutions, validation records, inventories, primary submissions and ancestor evidence.

Validation: **161 targeted repository tests passed; 12 supplementary checks passed; one supplementary check reproduced R2.** Receipt-dependent tests used immutable snapshots while retaining primary-file hash comparisons, so they did not retest Git anchoring. No full suite, model/provider calls, guest access, commits, pushes, merges or excluded-repository reads occurred. The worktree remains clean at `1f2ec59`; whitespace checks passed.

Blocking item: **R2**.

**NO-GO**
