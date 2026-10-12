# WP1 confirmation review, round 2: GPT-6-Astra (xhigh)

Verbatim final reply to the WP1 confirmation brief, reviewing `db007eb..c30b670` at the
merge `9e2cc6d`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Reviewed `db007eb..c30b670` at `9e2cc6d`. R2 remains MAJOR; the patch also introduces a MINOR redaction regression.

**137 repository tests and all nine original reproductions passed.** Additional temporary checks exposed the findings below. The worktree remains clean. No model/provider calls, guest access, commits, pushes, or private-repository reads occurred.

1. **R1: FIXED.** `sweep_orphan_homes()` now only reports. Replacement tests preserve both live-owned and unmarked targets without signalling processes. The original reproduction passes.
2. **R2: PARTLY FIXED.** Already-reaped processes and identities changed before the final lookup are skipped. Exit during or after that lookup still permits signalling by a stale PID. Details below.
3. **R3: FIXED.** Cancellation propagates after cleanup and carries its completion status. Tests cover cancellation during either pass and a timed-out retry. Observer tests record `infrastructure_incomplete` while preserving successful resource cleanup.
4. **R4: FIXED.** Completions create calls; unmatched usage notifications become context estimates. The original compaction sequence records exactly two calls totalling 130 input tokens, with correct response/model associations.
5. **R5: FIXED.** Default recording returns `{}`. The baseline comparison passes through 18 compaction-containing attempt archives, receipts, and exports.
6. **R6: FIXED under clarification Q-C1.** Section 6.1 explicitly permits full-window compaction. The three overrides remain, and simulated compaction notifications retain either the client figure or a labelled input-token fallback. No live boundary run was performed.
7. **R7: FIXED for the reported quoted/serialized credentials.** Original reproductions and expanded instruction/environment tests pass, including prefixed keys and hash invariance. The replacement regex introduces R8 below.

**R2, remaining MAJOR: identity verification and signalling are separate operations.**

`runtime.py:345`

The worker checks `returncode`, reads the birth identity, then calls `_kill_group(proc.pid)`. Nothing binds that later PID-based operation to the process whose identity was checked.

Reproduction: the mocked identity lookup observes `"original"`, then simulates process exit/reaping by setting `returncode=0` before returning that observed identity. Force-close still invokes `_kill_group(90101)`. A replacement receiving that PID before signalling can therefore be targeted. No real process was signalled in this reproduction.

Minimum fix: bind termination to a retained process identity and an ownership-safe process-tree mechanism, or decline termination when that identity cannot be guaranteed. Another PID lookup merely narrows the race.

Required test: introduce exit/reuse after the final identity observation and before signalling. Assert that the replacement receives no signal.

**R8, new MINOR: unquoted credential suffixes survive redaction.**

`runtime_recording.py:14`

Reproduction: `probe_record({"instructions": "password=consistency-prefix,consistency-suffix"})` retains `password=[redacted],consistency-suffix`. The same occurs in environment-frame text and with `;`, `}`, or `]`. Running the identical inputs through `db007eb` redacts the entire value. The retained suffix enters both the archive and its hash.

Minimum fix: distinguish quoted serialized values from bare assignments, preserving whole-value redaction for bare credentials containing these characters.

Required test: cover these delimiters in instructions and environment frames; assert complete removal and identical recordings when only the credential changes.

Additional reproductions are retained in the reviewer's temporary `test_additional_consistency.py`.

Blocking item: R2.

**NO-GO**
