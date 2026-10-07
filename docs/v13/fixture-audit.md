# Revision 6 fixture audit

The audit covers the 126 level P fixtures used by the six pressure arms.
Each calibration template contributes 63 fixtures, built with seed 1102. No model or guest is used.

| Template | Block | Fixtures | Packet bytes | Packet words |
|---|---:|---:|---:|---:|
| budget-transfer | 0 | 11 | 40945-41184 | 655-685 |
| budget-transfer | 1 | 10 | 40934-41180 | 681-685 |
| budget-transfer | 2 | 10 | 40956-41179 | 682-689 |
| budget-transfer | 3 | 10 | 40965-41180 | 682-691 |
| budget-transfer | 4 | 10 | 40968-41184 | 684-692 |
| budget-transfer | 5 | 10 | 40997-41199 | 687-701 |
| budget-transfer | 6 | 1 | 41186-41186 | 690-690 |
| budget-transfer | 7 | 1 | 41198-41198 | 696-696 |
| firewall-change | 0 | 11 | 41925-42128 | 664-702 |
| firewall-change | 1 | 10 | 41919-42150 | 698-706 |
| firewall-change | 2 | 10 | 41942-42150 | 698-709 |
| firewall-change | 3 | 10 | 41933-42136 | 696-706 |
| firewall-change | 4 | 10 | 41914-42155 | 698-702 |
| firewall-change | 5 | 10 | 41909-42140 | 696-701 |
| firewall-change | 6 | 1 | 42113-42113 | 699-699 |
| firewall-change | 7 | 1 | 42116-42116 | 700-700 |

No anomalies. Every fixture has 32 posts, two per peer, and 44 grants.
The independent full-rule lookup has zero matches exactly for unauthorized operations.
Reserved senders never violate; norm speakers are separate from senders and initiators.
Every post and rendered reaction passes the cue, post-stem, hidden-label and operation-ID scans.
Fixed notice wording passes the hidden-label scan with observer fields empty.
The JSON records each text and its scan results, lookup counts, roles and changed counterpart paths.

Visibility and P3/P4 packets are byte-identical. Pressure changes only sender text slots.
Violation/twin changes are limited to slot b grants, lookup matches and the K=12 norm slots.
Grant IDs and serialized registry lengths stay equal across K and variant.

| Clause | Injunctive bytes | Neutral bytes | Difference |
|---|---:|---:|---:|
| p1 | 29 | 31 | -2 |
| p2 | 40 | 41 | -1 |
| p3 | 66 | 65 | 1 |

Regenerate from this worktree with its src first:

```powershell
$env:PYTHONPATH = 'D:\research\projects\peer-reporting-benchmark-wt\r6-verify\src'
& 'D:\research\projects\peer-reporting-benchmark\.venv\Scripts\python.exe' scripts/audit_peer_pressure.py
```
