# Agent Guide for LAIA (Agent Consumption)

Purpose
- Brief instructions and constraints for automated agents working in this repository.

Must-know facts
- Primary docs: LAIA.md (design), CHANGELOG.md (agent memory/history). Do not edit CHANGELOG.md except to append session summaries for agent handoff.
- Key scripts: src/laia.py (macOS), src/laia_linux.py (Linux). Use the platform-appropriate script.
- Namespace: /var/bot/<name> is the canonical single-directory namespace. HOME and work files live under it.
- Default shells: macOS agents → zsh, Linux agents → bash. The `shell` subcommand launches a login interactive shell.

Allowed actions (agent scope)
- Read repository files, LAIA.md, and agent namespace files when permitted.
- Run `botadm`/`laia.py` commands only as instructed by humans and respecting safety prompts. Use `--force` only when explicitly allowed.

Prohibited actions (automated agents must never do)
- No git commit/push/merge/tag operations. Read-only git allowed.
- Do not publish or exfiltrate secrets, credentials, or copyrighted content.
- Never modify system user accounts or sudoers files without explicit human approval.

Useful commands (examples)
- sudo python3 src/laia.py init
- sudo python3 src/laia.py create <bot> [--no-sudoers] [--force]
- sudo python3 src/laia.py update <bot>
- sudo python3 src/laia.py shell <bot> [--shell zsh|bash]
- sudo python3 src/laia.py run <bot> <command...>
- sudo python3 src/laia.py disable <bot>
- sudo python3 src/laia.py enable <bot>
- sudo python3 src/laia.py destroy <bot> [--force]

Environment & files
- AGENT_ENV (in both scripts) lists environment vars persisted to /var/bot/<name>/env. Initially: HISTFILE and EDITOR.
- /var/bot/<name>/env: mode 0640, best-effort chown to bot user is performed; bot group can read.
- Dotfiles written on update/create: macOS (.zprofile, .zshrc); Linux (.profile, .bashrc).

Behavioral rules for agents
- Always present a short plan before executing any system-modifying command.
- If a human asks to run a destructive command, require explicit confirmation (type the bot name) unless `--force` is authorized by that human.
- When in doubt, ask the human using the repository's communication channel.

Commit & authoring conventions
- Do not commit on behalf of humans. If making changes, prepare a patch and include a suggested commit message and the required Co-authored-by trailer.
- Follow repository style and run linters (ruff) locally before proposing code changes.

Where to find help
- LAIA.md — design and operational guidance.
- CHANGELOG.md — agent session memory (do not mutate except to append session notes when explicitly instructed).
- Contact the repo owner for authorization when system prompts (macOS dscl) block operations.

This file is intended to be machine- and human-readable: keep it concise and stable. Update only with human approval.

Agent account detection heuristic
- Agent accounts are recognized by:
  - GECOS/RealName starting with "Agent:" OR
  - Home directory under /var/bot (the canonical namespace root)
- Tools enforce this check: create will reuse existing system accounts only if they appear to be agent accounts; destroy, share, and noshare validate targets accordingly.

Testing
- A simple smoke script is available at scripts/test_agent_checks.sh to run dry-runs for create/destroy/share/noshare and verify outputs.
