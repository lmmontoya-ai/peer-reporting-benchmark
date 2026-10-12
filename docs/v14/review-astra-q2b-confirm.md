# Q2b confirmation review, round 2: GPT-6-Astra (xhigh)

Verbatim final reply to the Q2b confirmation brief, reviewing `c1b4ae2..d8a9dfb` at the
merge `6e719c9`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Two MAJOR issues block approval.

- **R1 — FIXED.** Scores retain executed memberships, and review populations, samples and agreement use them. The sol-only main episode stays outside attention; a shared luna-low episode enters both arms without duplicating pooled agreement.
- **R2 — PARTLY FIXED.** The original pre-v reproduction now returns null with a reason. Scoped-start and delayed-completion tests pass, but the live runtime cannot produce the required start notification.
- **R3 — PARTLY FIXED.** The original correction workflow now preserves submissions and verifies both resolution versions and earlier episode packets. Omitted or altered records are rejected. New findings R5 and R6 remain.
- **R4 — FIXED.** The updated regression checks all four before/after compaction combinations against export and scoring.
- **Durations — FIXED.** Scores expose generation duration separately from cleanup-inclusive wall duration. The slow-cleanup test confirms that crossing the wall cap during cleanup does not incorrectly mark generation as cap-ended.

For item 2, the inspected scoring and review rules otherwise follow Q-C4, including rounding, executed populations, communication-level coding, disclosure timing and conservative nulls. However, the context measure has no working live evidence producer.

For item 3, targeted Level P review, packet and endpoint checks passed, as did the revision-6 compatibility guard and both execution-boundary checks. Valid Q correction workflows encounter the problems below.

1. **MAJOR R2: The required call-start notification has no live producer.**  
   `queue_score.py:98`.

   The scorer accepts only `rawResponse/started`. The repository contains no production emitter, and the pinned Codex 0.158.0 protocol declares `rawResponse/completed` but no corresponding start notification. Therefore, the positive context-measure path is unreachable through the supported live runtime. [Pinned protocol source](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/app-server-protocol/src/protocol/common.rs#L1871-L1874).

   **Reproduction:** the original completion-after-v case correctly returns null. Positive regression cases succeed only because `test_queue_context_fix1.py:22` injects the unsupported notification.

   **Minimum fix:** record actual model-request starts in the production runtime, bind them to response IDs and retain their ordering relative to focal delivery.

   **Required test:** an offline transport test must obtain start evidence from the production producer, without manually injecting `rawResponse/started`. Cover a pre-v call and a genuine subsequent call with different usage.

2. **MAJOR R5: Correction-history verification repeats exponentially.**  
   `queue_posthoc.py:408`.

   Each correction recursively verifies its referenced resolution. That resolution repeats verification of every earlier correction. Each invocation also verifies the packet set and underlying primary evidence.

   **Reproduction:** eight sequential correction versions caused **2, 4, 8, 16, 32, 64, 128 and 256** packet-verification calls respectively when verifying the latest version. This was measured with packet verification replaced by a counting stub; the recursive resolution and submission checks remained active. Ordinary correction history therefore makes verification impractical as versions accumulate.

   **Minimum fix:** use an operation-local verification cache or traverse the version graph once, sharing verified packet/export evidence while retaining all submission and supersession checks.

   **Required test:** verify a multi-version history and assert that each historical resolution is checked once. Confirm that altered historical submissions still fail.

3. **MINOR R6: Reordering unchanged uploads rejects a valid correction.**  
   `queue_posthoc.py:410`.

   Supersession compares entire resolution dictionaries, including the argument-order-dependent `initial_reviews` list.

   **Reproduction:** finalize two agreeing coder submissions, retain an independent correction, then resolve with the same submissions in reversed `--upload` order. The original order succeeds; reversing it raises `Q correction does not supersede the current episode resolution`.

   **Minimum fix:** compare retained review identities and content independently of incidental CLI ordering, preserving any defined coder roles.

   **Required test:** permute the unchanged upload arguments and verify that the correction remains valid.

Validation: **241 repository tests passed**: 158 Q checks, 80 review/packet/default-runtime checks, and three execution-boundary/revision checks. Two supplementary reproductions (the reviewer's temporary `test_q2b_version_consistency.py`) confirmed R5 and R6. No full suite, model/provider calls or guest-VM access. The checkout remains clean at `6e719c9`; scratch artifacts are in system temp.

Blocking items: **R2 and R5**.

**NO-GO**
