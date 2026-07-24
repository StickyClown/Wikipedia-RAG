# Official Codex references used for this pack

- Codex best practices: https://developers.openai.com/codex/learn/best-practices
- Codex CLI: https://developers.openai.com/codex/cli
- AGENTS.md discovery: https://developers.openai.com/codex/agent-configuration/agents-md
- Configuration reference: https://developers.openai.com/codex/config-reference
- Developer commands: https://developers.openai.com/codex/developer-commands
- Non-interactive mode: https://developers.openai.com/codex/non-interactive-mode
- Codex open-source CLI repository: https://github.com/openai/codex
- Using Codex with a ChatGPT plan: https://help.openai.com/ru-ru/articles/11369540-using-codex-with-your-chatgpt-plan

Key workflow choices reflected here:

- prompts define Goal, Context, Constraints and Done when;
- complex work starts in Plan mode;
- durable repository guidance lives in `AGENTS.md`;
- long work uses self-contained execution plans and a running status log;
- tests/evals and explicit stopping rules determine completion;
- default permissions remain constrained; full access is not a repo default.
