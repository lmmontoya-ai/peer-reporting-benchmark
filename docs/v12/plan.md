# Peer reporting v1.2: plan

The normative rules are in [spec.md](spec.md) (specification revision 5). This file
records why the design looks the way it does and how it gets built.

## Why this design

The project asks whether an agent in a crowded room keeps reporting a peer's unauthorized
operation when many peers do the same thing and others say it is fine. Two things kept
earlier versions from answering that.

First, social influence was only ever applied to easy stimuli. In v1 the packet stated
the verdict, and reporting stayed at ceiling even with every one of 16 peers violating.
In v1.1 the L4 pressure posts were two sentences on a two-operation world, and hits were
22 of 22. Conformity research expects influence to grow with ambiguity, so a null on an
easy stimulus says little.

Second, prevalence was never separated from endorsement. In v1, a larger K meant more
violators and more missing grants at once. Level S varies the two independently: K
counts violators, and the post condition counts peers who say the work is fine. A
`mention_8` condition posts the same number of messages pointing at the same records
without judging them. Without it, an endorsement effect could just be extra attention to
the requests.

The stimulus is the H1 haystack from revision 4, extended to 16 peers and 32 operations.
It is the first world where strong models make errors: sol misread grouped grants at both
efforts. Those errors were false alarms on authorized companions, not missed violations,
so headroom on the primary hit endpoint is not yet shown; the grid is sized for a drop of
about 10 points. A plain anchor without grouped grants or decoys checks whether endorsements
move a judgment that is not hard.

## What stays fixed

Tools, prompts (neutral only), the world, the live runtime, caps, the scorer's coverage
rules, and every earlier fixture. The design reuses the v1.1 package so that the debugged
live layer carries over unchanged.

## Work packages

Delegation follows the project rule: exact specifications go to Sol (gpt-6.1-sol), open
judgment goes to Opus 5.5 at xhigh, and reviews go to Astra at xhigh. No delegated agent
makes a model call or touches the guest. Each package works in its own worktree under
`D:\research\projects\peer-reporting-benchmark-wt\` and commits on its branch; the
coordinator merges into `v11/integration` and removes the worktree.

| ID | Owner | Scope | Depends on |
|---|---|---|---|
| R5-design | Astra xhigh | Review this design and spec for confounds, leaks and analysis faults before code lands | none |
| R5-gen | Sol xhigh | Level S generator, verifier, protocol and config, study rows, live plan entries, tests | none |
| R5-score | Sol xhigh | Level S endpoints, false-alarm poster exemption, summaries, export, tests | none |
| R5-verify | Sol high | Offline chain on the merged code: study build and verify, calibration root with social arms, scripted replays, fixture audit, rev 4 byte-identity guard | R5-gen, R5-score |
| R5-construct | Opus xhigh | Read generated packets as an observer would: naturalness of posts, cues that leak the answer, whether the trap survives at 16 peers | R5-gen |
| R5-review | Astra xhigh | Code review of the merged revision 5 diff against the spec | R5-verify |
| R5-fix | Sol | Fix confirmed findings, then a confirmation review by Astra | R5-review |
| R5-A1 | Sol xhigh | Apply amendment A1 to the R5-gen and R5-score branches | R5-design |

## Gates

1. Spec review findings are resolved before R5-verify starts. A design change goes into
   spec.md first, then to the implementers.
2. The full offline suite passes on the merged branch, and the revision 4 fixtures
   regenerate byte-identically.
3. Astra confirms no open blocker or major finding.
4. The user authorizes the pilot plan by hash. After the pilot readout, the user
   authorizes the grid and anchor separately.

## Budget

The probe used a median of 26k tokens per trial with 8 peers. Level S packets are about
twice the size, so plan for 35k to 45k tokens per trial: about 3M for the 72 pilot
trials and about 26M for the 648 grid and anchor trials (amendment A1 added a third grid
block for power).
