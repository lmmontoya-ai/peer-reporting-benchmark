# WP1 confirmation review, round 4: GPT-6-Astra (xhigh)

Verbatim final reply to the fourth WP1 review brief, reviewing `f0ea80a..6c8f3f7` at the
merge `52f3d28`. Read-only review; no model calls. Links to local files are reduced to
their file names.

WP1 (runtime cleanup and opt-in episode recording) is GO after this round.

Reviewed `f0ea80a..6c8f3f7` at `52f3d28`. **121 targeted repository tests, five adapted reproductions, and one additional transport check passed.**

1. **R2: FIXED.** `runtime.py:859` skips termination on every retry. The adapted watcher reproduction delays exit publication while assigning the PID to a replacement: no handle is acquired and no signal is sent. Repeated retries remain signal-free.

2. **R9: FIXED.** `runtime.py:298` no longer allocates a pidfd or schedules its reaper. Both bookkeeping-failure reproductions pass, covering exit before cleanup and exit afterward. Neither leaves an added descriptor or reaper task.

For item 2, I found no signalling path in the second or subsequent cleanup passes. `_stop_process()` is byte-identical across the baseline, fix, and reviewed HEAD. The tested retries cancel and await reader/background/peer-dispatch tasks, cancel pending futures, remove the home, and clear sessions.

A live child deliberately remains referenced, with its ordinary subprocess transport and pipe handles still open. The additional local-process check confirmed those pipes close after child exit, with no runtime tasks remaining. This is the accepted incomplete-cleanup fallback. Observer and lane tests preserve `runtime cleanup incomplete: TimeoutError: `, `runtime_closed=False`, and the hold.

For item 3:

- **R1 remains FIXED:** directory-replacement checks preserve both live-owned and unmarked targets; the sweep only reports.
- **R3 remains FIXED:** cancellation propagates during either pass, including when the retry times out.
- **R4 remains FIXED:** the compaction sequence records two calls totalling 130 input tokens.
- **R5 remains FIXED:** default compaction preserves bytes across 18 attempt archives and their receipts/exports.
- **R6 remains FIXED under Q-C1:** the clarified full-window exception and recorded context-source checks pass. No live boundary run was performed.
- **R7 and R8 remain FIXED:** quoted credentials and bare delimiter-containing values are removed before hashing; invariance checks pass.

No new BLOCKER, MAJOR, or MINOR findings in the fix diff. Blocking items: none.

Adapted reproductions are retained in the reviewer's temporary `test_handle_consistency.py`. The worktree remains clean. No model/provider calls, guest access, private-repository reads, commits, pushes, or merges occurred.

**GO**
