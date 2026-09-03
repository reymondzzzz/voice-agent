# voice-agent

A standalone voice agent: one agent hands a live call to another, and the voice changes with it.
Built on self-hosted LiveKit for media and LangGraph for reasoning.

Start with [AGENTS.md](AGENTS.md) for the rules and the one command that gates a commit, then
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit and
[docs/RUNBOOKS.md](docs/RUNBOOKS.md) to run it.

```bash
uv sync && cp .env.example .env.local
uv run src/agent.py console
```
