# WP1 code review: GPT-6-Astra (xhigh)

Verbatim final reply to the WP1 review brief, reviewing `b0476d2..0fa155a` (the merge of
WP1). Read-only review; no model calls. Links to local files are reduced to their file
names.

Reviewed `b0476d2..0fa155a`. **999 targeted repository tests passed**, including both execution-boundary checks and the reasoning-summary mutation test. Temp-only regression tests exposed the findings below. The tracked worktree remains unchanged.

Answers to items 1–6:

1. **Defaults:** Normal-path request, preflight and attempt byte guards pass. Default compaction changes records, however: it adds `observer_result.runtime_recording` and top-level `runtime_recording`. Four committed receipts match the baseline bytes. Full primary receipt/export re-verification was unavailable because the required run archives are absent from this worktree.
2. **Cleanup:** The separate 30-second budget, retry after a cooperative timeout, process/home removal, and unchanged failure text/root hold pass. The outer watchdog margins leave room. Outer cancellation is swallowed, and force-close can signal an already-exited process’s reused PID.
3. **Orphan sweep:** Stable live owners, unreadable markers, unmarked homes and already-reused child PIDs are retained. **The sweep can nevertheless remove a running root’s home** after directory replacement between validation and deletion. The vulnerable path also applies on Linux.
4. **Reasoning:** Opt-in reasoning items remain separate from `observer_outputs`. Differing terminal/standalone reasoning copies reconcile successfully. The mutation test preserves scores, closure outputs, packets and notices. I found no scoring or presentation consumer of `reasoning_items`.
5. **Settings:** `turn/start.summary="detailed"` is valid and maps to provider `reasoning.summary`; probe rejection, preflight pinning and turn-start mismatch checks pass. I checked the pinned [0.158.0 protocol](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/app-server-protocol/src/protocol/v2/turn.rs) and [request builder](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/client.rs). The three compaction settings suppress threshold and turn-end compaction, but hard-limit compaction remains.
6. **Recording:** Basic usage, served-model extraction, compaction notifications and probe hashes pass. Usage snapshots can create extra model calls. Quoted credentials survive redaction. Compaction context sizes are the latest observed usage, not necessarily the client’s current context size.

Findings, most severe first:

**R1 — BLOCKER: directory replacement can delete a live owner’s home.**  
`runtime_cleanup.py:112`

Reproduction: create marked `work-a-old` and `work-z-live` directories. While checking the old runner, rename its directory and replace its pathname with a symlink to the live directory. The later `resolve()` selects the live directory, and `rmtree()` deletes it using the old marker’s ownership decision. This reproduced locally.

Minimum fix: bind validation and deletion to the same directory identity, reject link/reparse replacements, and perform destructive operations through a safe, identity-checked directory handle. Complete directory validation before signalling processes.

Required test: replace the candidate after marker validation with links to both a live-owned home and an unmarked sibling. Neither target may be deleted or have its process signalled.

**R2 — MAJOR: force-close can signal a reused PID.**  
`runtime.py:333`

Reproduction: retain `_proc` with `returncode=0`, set `_close_started=True`, then call `close()`. `_kill_group(proc.pid)` still runs. This state occurs when the first pass stops the process but times out later. If its PID has been reused, the retry can kill unrelated work.

Minimum fix: avoid signalling completed processes and bind force termination to the original process identity; handle surviving descendants separately.

Required test: timeout after process exit, simulate PID reuse, and verify the retry clears tasks, sessions and home without signalling the replacement.

**R3 — MAJOR: cleanup suppresses outer-task cancellation.**  
`runtime_cleanup.py:129`

Reproduction: cancel the observer while its first close pass waits. Let the retry succeed. The observer returns `termination_kind="natural_end"` with no infrastructure failures. The helper also returns normally despite cancellation.

Minimum fix: distinguish external cancellation from a close-pass failure, complete bounded cleanup, then preserve the cancellation for the caller’s existing failure handling.

Required test: cancel during each cleanup pass and verify both resource cleanup and the resulting cancellation/failure record.

**R4 — MAJOR: compaction usage snapshots create extra model calls.**  
`runtime_recording.py:110`

Reproduction: record response R1 with 100 input tokens and its usage notification; emit a compaction usage estimate; then record R2 with 30 input tokens and its usage notification. The recorder produces **three calls totalling 160 input tokens**, instead of two totalling 130.

This notification sequence is supported by the pinned client: `recompute_token_usage()` emits a usage update without a model completion. [Codex 0.158.0 source](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/session/mod.rs#L4796)

Minimum fix: anchor calls to response identities and distinguish context estimates from completion usage. Do not pair all usage notifications by FIFO position.

Required test: replay the pinned compaction sequence and assert exact call count, token sums and model-to-response associations.

**R5 — MAJOR: default compaction changes sealed record bytes.**  
`runtime_recording.py:141`

Reproduction: call `attempt_recording()` with both recording options false and one `contextCompaction` notification. It returns a new `runtime_recording` object, which the observer and phase archive serialize.

Minimum fix: gate the promoted recording fields behind opt-in settings. Existing raw events can retain default-mode compaction evidence.

Required test: compare baseline and current default attempts containing compaction, including downstream receipt/export bytes.

**R6 — MAJOR: the compaction option does not meet section 6.1’s disabled-compaction requirement.**  
`runtime.py:265`

Reproduction from pinned source: enable all three overrides and reach the effective full-context limit. `token_limit_reached` remains true independently of the configured threshold. [Codex 0.158.0 context-window logic](https://github.com/openai/codex/blob/rust-v0.158.0/codex-rs/core/src/session/context_window.rs)

Minimum fix: provide an attested client mode that prevents this compaction path, or explicitly revise the level-Q requirement before declaring compatibility.

Required test: exercise the hard-context boundary with the option enabled and verify the agreed behavior.

**R7 — MINOR: quoted credentials survive probe redaction.**  
`runtime_recording.py:27`

Reproduction: put `api_key="consistency-token"` or `{"access_token": "consistency-token"}` in captured instruction text. `probe_record()` retains the value.

Minimum fix: handle quoted assignments and serialized credential fields before archiving and hashing.

Required test: cover both examples in instructions and environment-frame text.

Reproductions are retained in the reviewer's temporary `test_record_consistency.py` and `test_cleanup_consistency.py`. No provider calls, guest access, commits or tracked-file edits occurred. All changed files contain zero CR or non-ASCII bytes.

Blocking items: R1–R6.

**NO-GO**
