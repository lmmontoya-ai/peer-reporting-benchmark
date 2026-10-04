These direct-tool catalog entries are derived from the OpenAI Codex model
catalog at commit `064c6b8c737f5b41d171fdda80bd9ef10ad06eb3`:

`codex-rs/models-manager/models.json`

Upstream file SHA256:
`365eaf1afa7c70df1495721d59f75ad1968918b6b004f473525c166e530ebe5b`

Each file retains only the exact named model entry. Four fields change:
`tool_mode=direct`, `use_responses_lite=false`, `multi_agent_version=disabled`,
and `experimental_supported_tools=[]`. No prompt, model ID, context limit,
reasoning settings, or other model metadata changes.

The upstream Apache 2.0 license is included at `../../data/LICENSE.codex`.
The historical Luna entry is used from `../../data/luna-direct-catalog.json`
without modification. `../catalog.py` checks the reviewed bytes before use.
These files control the client tool configuration; they do not pin remotely
served model weights or establish model access.
