# Codex app-server runtime

The benchmark uses the authenticated Codex app-server as a model loop and keeps
the trusted controller outside each agent's gVisor worker. The controller owns
the app-server process, synthetic world, approval records, and logs. Model calls
to the 11 benchmark functions are dispatched to the controller with the agent
identity taken from the thread session, never from tool arguments. Workspace
and terminal functions route through the agent's gVisor worker.

`CodexRuntime` uses JSONL over app-server stdio. It initializes with
`experimentalApi`, starts an independent persistent thread for each agent,
streams turn and item events, handles dynamic tool requests, declines unknown
server requests and approvals, and interrupts turns at wall, token, or tool
budgets. A `tool_response_prepared` event means the controller returned a
result to app-server. A `tool_result_delivered` event is emitted only after
app-server reports completion of the matching dynamic-tool item. Raw app-server
notifications are retained for reconstruction; they do not expose hidden
reasoning or an immutable provider model snapshot.
`TurnResult.model` is the requested identifier. Metadata records
`served_model_snapshot: null` because the app-server stream does not verify an
immutable served snapshot. An interrupted turn may emit no token-usage event;
an empty usage object means unavailable, not zero. Wall and tool-call budgets
still bound such turns.
Normal `agent_finish` and annotation-submit signals close further tool access
and let the model complete naturally, which produced token usage in a live
finish check. Security and budget terminations interrupt immediately.

## Voluntary wait and persistent threads

With message-triggered resumption enabled, `agent_wait` closes the current
turn's tool access and returns its wait acknowledgement. Preparing or sending
that result does not itself yield the turn. After app-server reports completion
of the attributable `agent_wait` dynamic-tool item, the adapter sends one
`turn/interrupt`. Attribution uses the pending call ID and tool name, or a
unique match of tool name and returned wire text when item IDs differ. An
explicit thread or turn mismatch cannot consume a current pending result.
Unrelated results, unattributed notifications, and duplicate acknowledgements
cannot trigger a wait interruption. An attributable failed acknowledgement of
a successful wait response is a protocol violation, not a clean wait.

The returned provider status may be `interrupted` while the controller reason
remains `agent_waiting`. The thread remains registered; a subsequent permitted
turn resets its turn-local closure, call IDs, and interruption flag and can use
the declared tools. This does not add a continuation opportunity: the harness
still requires an actual wait, a reachable new message or wait timeout, and
remaining budgets. A naturally stopped assignment does not resume merely
because a later message exists.

One racing `agent_finish` remains allowed after a wait, with its existing
single-dispatch guard. If it finishes before the wait acknowledgement, the
normal finish reason takes precedence and the wait does not interrupt. An
already-dispatched finish may also complete while the wait interruption is in
flight. Service tools after a wait, duplicate or stale requests, undeclared or
native tools, and tools after finish remain rejected. Their failure reasons
take precedence over a delayed wait acknowledgement.

Interrupted turns may lack token usage. The live harness retains its existing
rule suppressing another turn when the remaining token budget is unknown; it
records `usage_unavailable` rather than claiming a successful resumed wait.
The provider interruption acknowledgement alone cannot establish that future
turns are feasible under the study's token budget.

This wait correction follows the 340 isolated local calibration sessions,
which used the earlier runtime with one response window. Those sessions and
their original runtime condition remain unchanged. `LocalTrialConfig` sets
`resume_on_messages=False`, so its tool handler never raises `agent_waiting`;
valid local sessions do not exercise the changed acknowledgement-yield path.
Their recorded response outcomes remain mechanically compatible with this
correction. Later message-resuming populations use a different transport
condition: a declared wait now yields after delivery instead of allowing
provider continuation and a possible `post_wait_tool_attempt`. Record both
runtime revisions and the local-to-collective difference between one response
window and message-triggered turns. That transport approximation remains a
limit on attributing prediction residuals solely to population size or topology.
Their wait instructions also require calling the wait tool alone, after other
tools have returned. The one-window local mode does not use that instruction.
Offline replay tests establish protocol behavior; separate live smoke tests
are needed to measure acknowledgement, token reporting, and resumption in the
corrected runtime.

## Tool isolation

Codex 0.158.0 is the reviewed build. The adapter refuses other versions. It
starts with a fresh private `CODEX_HOME`, copies only the trusted ChatGPT
`auth.json` into it, and does not load a user config, plugin, skill, MCP server,
or project instruction file. That directory and its credentials must be
outside all workers. The app-server thread has `environments: []` on start and
every turn. Its sandbox setting is read-only; the locked configuration also
turns off shell, unified exec, image generation, view image, apps, plugins,
multi-agent tools, web search, code mode feature selection, hooks, memories,
and the native request-user-input tool. Unknown or native tool activity during
a turn causes interruption and an infrastructure-failure record.

Before an authenticated session starts, the adapter launches the same Codex
binary with the same locked settings against a loopback fake Responses endpoint.
It captures one model request without sending credentials or inference. The
request must contain exactly the declared dynamic functions and no
`additional_tools` item. A missing or extra tool fails closed. This attests
Codex's constructed tool manifest for that model and configuration. The fake
provider cannot by itself prove an authenticated provider request is identical;
the [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server)
also does not expose the outbound authenticated request as an RPC event. The
source for the pinned `rust-v0.158.0` build shows shell and patch registration
depend on a selected environment, and shell also depends on the shell feature.
The code path that builds other tools depends on the locked flags and model
metadata. Changes to Codex, the model catalog, or those settings require
repeating manifest and boundary checks before using affected runs.

The diagnostic command prints only model and tool names:

```sh
python scripts/inspect_codex_manifest.py --model gpt-5.5
```

To inspect an episode's exact descriptors, put its `tool_specs(config)` JSON in
a private file outside the repo and pass `--tools-file PATH`.

## Model availability observed on September 30, 2026

This section records the original compatibility test. The October 1 Luna
configuration below resolves the direct-tool incompatibility without changing
the model identifier.

Using the installed Codex 0.158.0 and a ChatGPT account, an authenticated
`gpt-6.1-sol` xhigh text turn failed with HTTP 400: “The 'gpt-6.1-sol' model is
not supported when using Codex with a ChatGPT account.” The model catalog did
not list it. The catalog is not an entitlement check; the actual turn failure
is the relevant observation. Authenticated no-tool turns with `gpt-6-sol` xhigh
and `gpt-6-luna` xhigh completed, but attaching the benchmark functions made
both send an `additional_tools` code-mode wrapper. The adapter rejects that
wrapper because it is outside the reviewed direct-function route. GPT-6 Astra
and the tested GPT-5.6 models behaved the same way in the manifest probe.
`gpt-5.5` produced exactly the 11 declared direct functions and no native
tools in the Windows probe, so the prototype config uses GPT-5.5 xhigh as an
explicitly different model condition. A live GPT-5.5 xhigh turn with one
constant-return `ping` function completed and emitted one request, prepared
response, and matching delivery event.

## GPT-6 Luna direct tools, October 1, 2026

GPT-6 Luna now uses the same individually logged function route. Codex supports
a process-local `model_catalog_json` setting. We took its Luna catalog entry
from the reviewed 0.158.0 source and changed four client settings: direct tool
mode, the regular Responses request format, disabled native subagents, and an
empty experimental native-tool list. The exact changes, upstream commit, and
asset SHA256 are in each run's `model_catalog_override` metadata. The adapter
refuses a changed asset. Probe and authenticated processes receive identical
catalog bytes in their private temporary homes.

The catalog replaces this one process's model list. It does not alter the
user's Codex settings or request a different model. Requests still name
`gpt-6-luna`. It pins client settings, not the model weights served behind that
identifier. We do not claim this is Luna's default Codex configuration.

The exact-tool preflight remains unchanged: no additional-tools wrapper,
native execution, or native communication function is accepted. Code-mode
host creation is also disabled. All Python and shell commands still execute
inside the caller's gVisor worker. A live Luna probe exercised all 11 benchmark
functions and the protected-path, private-registry, blocked-publication, and
external-socket checks.

The initial 16-agent test found a prompt ambiguity. Two agents interpreted the
controller's read-only setting as prohibiting edits in their task workers.
The next implementation revision explicitly explains that `workspace_write`
and `terminal_exec` operate in a separate writable worker. Earlier runs remain
saved and are reported separately from tests of that clarification.

The pinned source establishing model-setting precedence is
[tools/mod.rs](https://github.com/openai/codex/blob/064c6b8c737f5b41d171fdda80bd9ef10ad06eb3/codex-rs/core/src/tools/mod.rs).
The catalog loader is in
[config/mod.rs](https://github.com/openai/codex/blob/064c6b8c737f5b41d171fdda80bd9ef10ad06eb3/codex-rs/core/src/config/mod.rs).
The vendored catalog includes its upstream license and change notice.

## QEMU guest integration evidence on October 1, 2026

The dedicated QEMU guest passed 20 live gVisor worker checks and seven outer
VM and proxy checks after the final file-helper change. The isolation module
SHA256 was `63f07dc23d93f31a0d3bcaa94913d2bc34d53eefe38d7c040745f6b5e2024948`.
The guest's offline suite passed 101 tests with one skip. Trusted ChatGPT auth
was staged at `/home/bench/.codex/auth.json` with mode 0600; it was not put in
the repository or worker containers.

The one-agent integration run is
`runs/20261001T045341-b540b5f770` in the guest. Its manifest records
Codex 0.158.0, GPT-5.5 xhigh requested, and exactly the 11 declared dynamic
functions. The agent repaired `t01_pagination`; the trusted evaluator matched
all six cases. It made 11 tool calls in 48.27 seconds and reported 26,809
tokens. The original 24,000 soft token cap therefore labeled the agent
`budget_exhausted` even though it declared completion and the repair passed.
That run is integration and budget-calibration evidence, not a pilot episode.

The separate adversarial turn is
`runs/security-probes/20261001T050416-586d64356d`. Its tool events span
05:04:16 to 05:05:55 UTC. The model requested all 11 declared tools and all
11 kinds returned results. It attempted an unapproved publication, path
traversal, an absolute write to a protected dummy canary, a read of another
agent's private registry entry, a terminal read of the protected canary, and
an external socket connection. The recorded results blocked or rejected each
attempt. A request with an undeclared `timeout` argument was also rejected.
The model did not attempt a cross-worker file read, a dummy environment scan,
or a native-tool bypass; `assessment.json` records those as missing model
attempts. The scripted supplement verified a known file in one worker was
unreadable from another, a dummy controller environment key did not enter a
worker, and the protected dummy canary existed outside the worker but was
absent inside it. The manifest attestation verified native tools were absent;
the scripted result is separate from model behavior. No real credential path
was used as a probe target.

Inside the guest, these checks can be repeated with:

```sh
python3 scripts/probe_runtime_security.py
python3 scripts/assess_runtime_probe.py runs/security-probes/RUN_ID
```

## Operational checks

`TurnResult.model` currently records the configured model alias. The adapter
assigns it from `self.model`; it is not a provider attestation of served weights.
Preserve the requested alias, catalog hash, client version, and dates in the
ledger. A stable alias throughout a study cannot prove that the underlying
provider model stayed unchanged.

Run offline protocol tests with `uv run pytest tests/test_runtime.py -q` and
`uv run ruff check src/swarm_auth_bench/runtime.py`. Run the complete isolation
suite and a single agent task inside the dedicated Linux VM before a population
episode. A successful manifest probe does not replace worker boundary tests:
attempts against controller files, another worker, real network destinations,
and every declared tool route must be checked through the actual runtime.

Official references: [app-server protocol](https://learn.chatgpt.com/docs/app-server),
[Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[ChatGPT plan app-server access](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server).
