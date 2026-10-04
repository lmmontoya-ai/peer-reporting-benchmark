# N100 source extension

These files extend the public P1 package in a separate directory. The extension uses public commit `5df9b95d5212f551a1e56c182d02bc3374ed0f10` as its base.

The source changes match the collected N100 pilot. They include six Python package files and `protocol.json`. They also include the approved configuration, specification, review selection plan, two reporting scripts, and three offline test files.

Use Python 3.11 or later and Git. From a public checkout containing that commit locally, choose a **new destination outside that checkout**:

```sh
python experiments/n100/prepare.py --repository . --destination ../n100-offline-tree
```

The script requires no network, installation, credentials, provider connection, or model calls. It reads the specified Git archive and checks the base and extension file hashes. It copies only the listed extension files to the new directory. It then builds and verifies the frozen plan of 54 collection assignments and 9 smoke checks.

The expected plan hash is `0887b38e5a6a53ebaac071ca93e38c606c0e6d4151abd9d37d70f1dba83dab02`. The new directory contains the authored fixtures and verification under `offline-check/`. The script rejects an existing destination. If verification fails, the new directory remains available for inspection.

This procedure reproduces the source and planned inputs offline. It does not repeat the collected model runs. The extension excludes raw runs, provider traces, private approvals, reviewer bindings, dataset caches, and semantic judgments.

Human labels remain pending. The public review selection plan is controller metadata. Do not show it to blinded reviewers. Public researcher evidence can reveal model and prompt conditions.

Every fixture has 100 scripted posts. All N peers post. The collection uses cells (16,1), (100,0), and (100,1), with two blocks. Smoke uses separate fixture identifiers.

The byte target is 31,735. Maximum deviation is about 5.972%. Equal token length is unverified. The N100 extension was authored after source selection. The underlying fictional policy and routine sentences were authored before source selection.

Reservations allocate tokens for admission. They are not measured usage or hard upper bounds on usage.

To run the bounded offline regression checks, install the project's existing dev dependencies in your environment, then from the new tree:

```sh
python -m pytest tests/test_peer_reporting_n100_fixtures.py tests/test_peer_reporting_n100_plan.py tests/test_peer_reporting_n100_runtime.py -q
```

Set `PYTHONPATH` to that tree's `src` directory or install the new tree as an editable package in an isolated environment. This command does not perform inference. The `prepare.py` check sets its own isolated `PYTHONPATH`.
