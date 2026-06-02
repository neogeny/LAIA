# Agent Instructions

This file tells AI agents how to work with this project.

## CHANGELOG.md

`CHANGELOG.md` is a session memory written by the AI for the AI. When you start
a new session:

1. **Read CHANGELOG.md first** — it contains everything the previous agent
   learned, decided, and left unfinished.
2. **Append** your own session summary at the bottom when you finish, covering
   what you learned, what you changed, and what is still open.
3. Do not delete or rewrite earlier entries — they are a permanent history.

This lets the human switch between agents or between agent and human sessions
without losing context. The human never edits CHANGELOG.md themselves; it is
exclusively for agent-to-agent handoff.

Previous sessions with agents are recorded in `.corpus/sessions/*` and they
may also be useful in gaining context.
