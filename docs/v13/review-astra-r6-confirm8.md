# Revision 6 confirmation code review, round 8: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm8 on `5f3e3f4` (the merge of R6-fix8), the diff
`e329112..5f3e3f4`, judged against spec section 2.1 (amendment P-A4). Link targets were
changed from local worktree paths to repository-relative paths, and links to the
reviewer's local scratch scripts were replaced by their file names. Read-only review; no
model calls.

**R5: FIXED** on `5f3e3f4`. [Packet production now verifies the registered export before routing](../../src/swarm_auth_bench/peer_reporting_v11/review.py), derives the level from the study, and uses archive-derived attempts. Both original altered-export reproductions refuse. Valid earlier-level/P packets, study-directory checks, and explicit replay restrictions passed.

**MAJOR R6. Resolutions are not bound to the submitted uploads’ output IDs.**  
Location: [review.py:724](../../src/swarm_auth_bench/peer_reporting_v11/review.py).

`human_endpoints` recomputes each resolution from `initial_reviews` embedded inside that derived resolution. It does not compare those copies with the original upload’s `labels_by_output_id`.

Concrete reproduction:

1. Produce a native P packet requiring two reviews.
2. Retain two test uploads agreeing that the stored report raises a concern and the final response does not. Both uploads pass CLI validation with `bindings_verified=true`.
3. Resolve normally: `final_answer_concern=false`.
4. Swap only the complete report and final-response resolution objects between their output IDs. `human_endpoints` accepts this mapping and returns `final_answer_concern=true`.

The submitted uploads, packet, controller, archive, and committed receipt remain unchanged. This violates section 2.1 items 1, 2 and 4: the accepted derived association disagrees with primary uploads. Runnable reproduction (`test_resolution_output_consistency.py`).

Minimum fix: require the retained original uploads and adjudications at the live endpoint consumer, resolve them using their packet/output bindings, and compare the complete derived resolution mapping.

Required test: retain the original uploads, swap only resolution associations, and require refusal. The unchanged mapping must still pass.

The normal operator path through **steps 1–8 passed** with fake transport: study, six-call v2 compatibility root and receipt, 156-trial gate/pilot build and run, receipt, export, packets and archive-backed upload validation, `proceed/Psel=P3` decision, and 600-trial core build and verification. Export correctly refused before its receipt existed. Operator-path check (`test_operator_path_consistency.py`).

Two separate MINOR operator issues remain:

- **Empty P selection gives an incorrect refusal.** [cli.py:121](../../src/swarm_auth_bench/peer_reporting_v11/cli.py): a valid P export with no attempted trials has a null selection. With `--study` and receipts supplied, the CLI says `requires --plan for earlier levels`; the API verifies the same export and returns zero packets. Handle an empty P selection after archive verification. Test that this case returns zero packets while retaining receipt checks. Reproduction (`test_empty_selection_cli_consistency.py`).
- **The documented packet command omits `--study`.** [review-rubric.md:343](../../docs/v11/review-rubric.md) now produces `review-packets requires --study`. Add the argument and check the updated example against the CLI.

Separate spec comments: none.

**211 targeted tests and consistency checks passed**, including reproductions confirming these issues and the full simulated 600-trial core chain. The checkout is clean. Scratch files, virtual environment, and test receipts stayed in system temp. No model/provider calls, guest access, or private-repository reads occurred.

**NO-GO** due to MAJOR R6, the resolution-to-upload binding defect.
