The Luna model catalog is derived from OpenAI Codex 0.158.0,
commit 064c6b8c737f5b41d171fdda80bd9ef10ad06eb3,
`codex-rs/models-manager/models.json`.

Source: https://github.com/openai/codex/blob/064c6b8c737f5b41d171fdda80bd9ef10ad06eb3/codex-rs/models-manager/models.json

Copyright OpenAI. Licensed under Apache License 2.0.
The accompanying LICENSE.codex file contains the upstream license.

We selected the gpt-6-luna entry and changed four client settings:
tool_mode to direct, use_responses_lite to false, multi_agent_version to
disabled, and experimental_supported_tools to an empty list. These settings
remove native execution and communication tools. The model identifier and all
other catalog values are preserved. The experiment supplies its own task
instructions through thread/start.
