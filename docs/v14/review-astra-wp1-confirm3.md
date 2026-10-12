# WP1 confirmation review, round 3: GPT-6-Astra (xhigh)

Verbatim final reply to the third WP1 review brief, reviewing `3e23858..2a5ecba` at the
merge `c303cbe`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Reviewed `3e23858..2a5ecba` at `c303cbe`. **R2 is PARTLY FIXED; R8 is FIXED.** One new MINOR defect remains.

All **116 targeted repository tests passed**. Re-running `test_additional_consistency.py` produced 10 passes and one failure because its handle-less fixture now raises the intended “no bound termination handle” error. Adapted checks confirm that this path sends no signal.

1. **R2 remains MAJOR: pidfd acquisition can bind a replacement process.**  
   `runtime.py:320`

   Acquiring the pidfd immediately after `create_subprocess_exec()` does not guarantee original-process identity. CPython 3.11’s default `ThreadedChildWatcher` can reap the child before publishing its exit to the event loop. During that gap, `proc.returncode` remains `None`, although the PID can be reused.

   Reproduction: execute the installed watcher implementation with synthetic `waitpid`, delay exit publication, and assign the PID to a replacement before `pidfd_open`. The runtime retains the replacement’s handle and force-close records `('replacement', 9)`. No real replacement process was signalled.

   Minimum fix: coordinate launch and reaping so acquisition occurs while the original child is guaranteed unreaped, or obtain a handle during process creation. Otherwise, leave termination unavailable and record incomplete cleanup.

   Required test: exercise exit/reaping and PID reuse during handle acquisition, with exit publication delayed. Assert that force-close cannot signal the replacement.

2. **R8 is FIXED.**  
   `runtime_recording.py:16`

   Bare assignments now redact through the next whitespace. All eight original delimiter/location reproductions pass. Repository tests also verify hash invariance, quoted-value structure, and Basic authorization redaction.

3. **R9, new MINOR: failed launch bookkeeping can leak the pidfd.**  
   `runtime.py:323`, `runtime.py:337`

   Reproduction: let handle acquisition succeed, then make the second `mark_home()` raise `OSError`. `_launch()` never returns, so `self._proc` remains `None`. Subsequent `close()` cancels the reaper through `_background`. If the child remains live, the reaper skips closing the fd; later child exit cannot trigger cleanup. Cancellation before the reaper’s first execution also leaks the fd, even if the child has already exited. Both cases reproduced with `closed=[]` and `_runtime_pidfd=42`.

   Minimum fix: explicitly unwind failed launches and preserve handle ownership until cleanup closes the descriptor. Do not rely exclusively on a cancellable background task.

   Required test: inject bookkeeping failure after acquisition, both before the reaper starts and while it awaits exit. Verify eventual, exactly-once descriptor closure.

For item 2: normal paths retain and close the pidfd, including a cancelled first close followed by force-close. Acquisition is not certainly bound to the launched process, and closure does not cover every path. **No force-close path still signals by PID or group.** Windows uses the subprocess handle; missing handles produce incomplete cleanup. The unchanged first-pass `_stop_process()` still uses POSIX process-group signals.

Additional reproductions are in the reviewer's temporary `test_handle_consistency.py`. Linux checks used synthetic process state on Windows. The worktree remains clean; no model/provider calls, guest access, or private-repository reads occurred.

Blocking item: R2.

**NO-GO**
