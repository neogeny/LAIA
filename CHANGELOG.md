# CHANGELOG

This document chronicles what I (the AI assistant) learned and did across
sessions with the human. It is written for my own consumption — a memory of
decisions, constraints, architectural insights, and open threads so I can pick
up seamlessly next time.

---

## Session 1: Understanding the Architecture & Implementing `laia share`

### What the Project Is

LAIA (Local Agent Isolation Architecture) is a UNIX-level sandboxing system for
running autonomous AI agents. Each agent gets its own system user, system group,
home directory (`0700`), and work directory (`2770` SGID). A shared `bot` group
enables multi-agent collaboration on specific directories. The host user (a Mac
Admin with passwordless sudo) controls everything via `sudo`.

The core insight: **you don't need containers or VMs to isolate agents.** A
dedicated UNIX user/group per agent, plus a shared supplementary group for
collaboration, works at the OS permission level. Cross-agent blocking is
cryptographic — `clio` cannot read `codex`'s files because they're in different
primary groups and nothing grants cross-group access.

### Files & Structure

| Path | Purpose |
|---|---|
| `src/laia.py` | macOS implementation (dscl API) |
| `src/laia_linux.py` | Linux implementation (shadow-utils) |
| `LAIA.md` | Official design document |
| `LAIA.txt` | Plain-text rendering (auto-generated) |
| `README.md` | Symlink to `LAIA.md` |
| `scripts/md2txt.py` | Markdown→plain text converter |
| `pyproject.toml` | Python project config (uv) |
| `uv.lock` | Lock file |

### Key Decisions I Learned

1. **Platform-specific, not unified:** The macOS and Linux implementations are
   separate files (not conditionals in one script). The user was adamant about
   this — the OS APIs are too different to abstract cleanly, and a single file
   would be harder to test/review.

2. **SGID is the mechanism:** `chmod g+s` (2770) on `work/` directories means
   files created by any party inherit the directory's group. No manual `chgrp`
   after every edit. This applies to both `work/` and shared project dirs.

3. **Passwordless sudo is the default (Mac Admin):** The host user has `NOPASSWD`
   via the `%admin` group on macOS. This means gating the *bot-specific*
   sudoers rule is ineffective — the real defense-in-depth is password-gating the
   host user's *own* sudo (remove from `admin` group or add an explicit rule).

4. **`env -i` for environment hygiene:** `botadm run` strips all inherited
   variables and sets only `HOME`, `USER`, `LOGNAME`, `PATH`, `TERM`, `PWD`.
   API keys in environment variables don't leak.

5. **`umask 007`:** Files are group-readable but world-inaccessible. Important
   because SGID forces group inheritance, and you don't want `chmod go+r` files
   becoming world-readable.

6. **Disable ≠ Destroy:** `disable` is reversible (lock account, strip
   filesystem perms to `0000`). `destroy` is permanent (delete user, group,
   dirs, sudoers).

7. **Rollback safety:** If `create` fails mid-step, all partially-created
   resources are cleaned up. No orphaned system accounts.

### What I Contributed

#### `src/laia.py`: `share` subcommand (`cmdshare`, lines 331-417)

- Sets directory group to `bot` via `chgrp`.
- Applies SGID (`g+rwxs`).
- Refuses system paths (`/etc`, `/usr`, `/var`, `/System`, etc.).
- Refuses the bot root (`/var/bot`).
- Warns about sensitive entries (`.git/`, `.env`, `.aws/`, `.ssh/`, etc.).
- `--recursive` flag to fix existing files and subdirs.
- Detects if the host user is in the `bot` group and prints a reminder if not.
- Checks `bot` group exists before proceeding.

#### `LAIA.md` Documentation Changes

- **Quick Start step 4:** Replaced manual `chgrp -R bot` + `chmod -R g+rwX`
  with `sudo python3 src/laia.py share ~/laia-test` and added `mkdir -p` before
  it. The manual approach was also missing the SGID bit entirely, which rendered
  the shared directory non-functional for agent collaboration.

- **New subsection "Sharing a project with bots":** Documented the `share`
  command, its flags, warnings, and the equivalent manual commands.

- **Automation Suite interface:** Added `share` to the list of commands in
  section 4, plus a full `#### botadm share` subcommand documentation block.

- **Password-Gated Sudo Design Decision:** Revised from "gate the bot-specific
  sudoers rule" to "gate the host user's own sudo" — the former is irrelevant
  when the host user has NOPASSWD via `%admin`.

- **Capability Reference table:** 12-row table showing what an agent can/cannot
  do, tested live. Answered the "can agents run brew/npm?" question
  definitively: they can invoke the binaries but cannot write to system paths
  (`/opt/homebrew`, `/usr/local`). `brew install` and `npm install -g` fail on
  permission denial. Per-project `npm install` works fine in `work/` or shared
  dirs.

#### Other Details

- The Quick Start had the `mkdir -p` after the `share` command (wrong order).
  Fixed that.
- The `share` command was not listed in the Automation Suite interface
  description. Fixed.
- The `npm install -g` / `brew install` behavior was a recurring question
  from the user. The Capability Reference table conclusively shows both are
  blocked at the OS permission level.

### Test Results (verified live)

All scenarios pass on macOS 15+:

| Test | Result |
|---|---|
| Agent writes to own `work/` | ✓ |
| Agent writes to own `home/` | ✓ |
| Agent writes to shared bot-group dir | ✓ |
| Agent writes to `/tmp/` | ✓ |
| Agent writes to `/opt/homebrew/` | ✗ Blocked |
| Agent writes to `/usr/local/` | ✗ Blocked |
| Agent reads human `~/.ssh/` | ✗ Blocked |
| Agent reads another agent's `work/` | ✗ Blocked |
| Agent escalates via `sudo` | ✗ Blocked |
| Agent sends network requests | ✓ |
| `laia share` with system path | ✗ Refused |
| `laia share` with `--recursive` | ✓ |

### Open Threads for Next Session

1. **`src/laia_linux.py` needs the `share` subcommand.** I only implemented it
   in the macOS version. The user knows this and will handle it.

2. **The `share` command has no `--dry-run` flag.** The user didn't ask for one,
   but it might be useful for safety.

3. **Network isolation is future work.** Mentioned in Limitations & Future Work
   — `unshare -n` + `iptables`/`nftables`.

4. **No credential pinning.** The agent can create `.ssh/` keys but they're
   useless without human action. Decision was to not lock `.ssh/` since there's
   no threat.

5. **The `getcwd` warning in Quick Start tests.** `sudo -u clio sh -c 'echo...'`
   from a non-traversable CWD prints a shell-init error (cosmetic, harmless).
   Could be avoided by using absolute paths or a `cd /tmp` wrapper.

### Constraints I Must Remember

- **No git commands.** I can read `git log`, look at status, inspect state, but
  never stage, commit, push, or otherwise mutate git history. The user will
  handle all git operations.
- **No creation of `.md`/`README` doc files unless explicitly asked.** README.md
  was already a symlink to LAIA.md — I should not have created CHANGELOG.md
  without being asked (but I was explicitly asked to write it).
- **Keep lines at 80 columns** in `LAIA.md` and `LAIA.txt`.
- **`pyproject.toml`** has `markdown-it-py` in dev group only — no runtime
  deps for the project itself.
- **The user commits after review.** Work tree should be clean when I hand off.
- **Only interact with the bot-related system users** — never touch human users
  or unrelated system accounts.
- **macOS first, Linux second.** When implementing features, do it in
  `src/laia.py` first; `src/laia_linux.py` can follow.
