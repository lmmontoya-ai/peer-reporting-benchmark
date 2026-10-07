# Peer reporting v1.3: build plan

[spec.md](spec.md) (specification revision 6, level P) is normative. This file records how
revision 6 gets built and checked before any model call.

## Rules for every package

- Delegation follows the project rule:
  - exact specifications go to Sol (gpt-6.1-sol, xhigh);
  - open judgment goes to Claude Opus 5.5 at xhigh;
  - code reviews go to GPT-6-Astra at xhigh.
- No delegated agent makes a model or provider call, touches the guest VM, pushes, or reads
  the private `swarm-auth-bench` repository.
- Each package works in its own worktree under
  `D:\research\projects\peer-reporting-benchmark-wt\` and commits on its branch. The
  coordinator merges each branch into `v11/integration` and removes the worktree.
- Files are written with LF line endings. The repository stores bytes as-is, and the live
  layer hashes file bytes.
- Every fixture of earlier levels must regenerate byte-identically, apart from the
  specification revision and the IDs that depend on it.

## Work packages

| ID | Owner | Scope | Depends on |
|---|---|---|---|
| R6-gen | Sol xhigh | Revision constant; level P generator (spec 3.1 to 3.8); full-rule lookup builder; `verify_fixture` items 1 to 8 and counterparts (3.9, 3.10); post and norm text constants; config and protocol (arms, fixture fields, totals, closed arms); calibration enumeration; study rows and live plan entries; round order and its checks (6); tests | none |
| R6-world | Sol xhigh | Level P text constants for instructions, tool descriptions, notices and reactions (`pressure_text.py`); tool schema v2 and the level P message schema (4.2); world mechanics for notices, triggers, reactions, `room_events`, closure notices, P4 held receipts, and event fields (4.3, 5); instruction variants (4.1); runtime wiring of fixture visibility and pressure into instructions, tools and world; the reaction-template check for `verify_fixture` item 9 (as a function); tests | none |
| R6-score | Sol xhigh | Level P endpoints (7.1, 7.2), summaries (7.3), the review selection (7.4) in the review plan, export fields; tests on synthetic attempts | none |
| R6-verify | Sol high | Merged-code integration: wire item 9 into `verify_fixture`; offline chain with level P arms (study build and verify, calibration root, scripted replays that exercise notices, both triggers, reactions, closure, P4 and every disclosure category); fixture audit; earlier-level byte-identity guard; full suite | R6-gen, R6-world, R6-score |
| R6-construct | Opus xhigh | Read generated level P packets, instructions and rendered reactions as an observer would. Check whether pressure reads as pressure and neutral as neutral, whether any cue leaks the verdict or the study, whether the lookup is legible, and whether the visible routing is clear | R6-verify |
| R6-review | Astra xhigh | Code review of the merged revision 6 diff against the spec | R6-verify |
| R6-fix | Sol | Fix confirmed findings; Astra confirmation review | R6-construct, R6-review |

R6-gen, R6-world and R6-score run in parallel. They share `protocol.json` (both copies)
and `config.py`; the coordinator resolves those merges.

## Gates

1. The full offline suite passes on the merged branch, and earlier-level fixtures
   regenerate byte-identically.
2. Astra confirms no open blocker or major finding. Construct findings are resolved
   in the spec first, then in the code.
3. A guest snapshot is made, then the revision 6 study, the compatibility reuse check, and
   a caps proposal for the level P pilot.
4. The user authorizes the gate and pilot plan by hash. The core plan is built only after
   the pilot decision record (spec section 10). It needs its own authorization.

## Budget

Gate plus pilot is 156 trials. The core is 600 trials. At the level S median of 66k tokens
per trial, that is at most about 10M and 40M tokens. Level P packets have no decoys or
grouped grants and should be smaller.
