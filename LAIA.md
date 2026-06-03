# Local Agent Isolation Architecture
## Multi-Agent Sandboxing via Headless UNIX Namespaces

This document describes a local isolation strategy designed to run autonomous
agents inside strict, independent security boundaries. It leverages native UNIX
user, group, and filesystem controls combined with a hardened, dynamic sudo
configuration — bypassing the bloat and security leakages of per-GUI-account
approaches.

---

## 1. Threat Model & Sandboxing Realities

When running autonomous agents locally, the goal is to defend against each of
the following without paying the resource and complexity cost of full virtual
machines or Docker setups.

**Credential Theft.** CLI tools invoked by agents (like `git` or `pip`)
automatically traverse up to the host user's home directory (`~/`) to resolve
sensitive files: `~/.ssh/id_rsa`, `~/.aws/credentials`, shell history, and
more. An agent running as the host user has unfettered access to all of them.

**Blast Radius Containment.** If Agent A (e.g., an experimental web scraper) is
compromised via indirect prompt injection, it must be cryptographically and
permission-wise blocked from reading or writing the workspace of Agent B (e.g.,
a codebase-refactoring agent).

**Environment Variable Hygiene.** Every child process inherits the parent
shell's environment. Production API keys, database credentials, and access
tokens must never leak into untrusted agent execution contexts.

**Filesystem Boundary Enforcement.** Without per-agent filesystem isolation, a
single rogue agent can corrupt, ransom, or exfiltrate the entire project tree.

### Capability Reference

The table below shows what an agent can and cannot do once sandboxed. These
results are from live tests on a macOS system; the same principles apply on
Linux.

| Action | Result | Why |
|---|---|---|
| Write to own namespace `/var/bot/clio` | ✓ Allowed | SGID 2770, agent group |
| Write to shared project (`bot` group) | ✓ Allowed | Member of `bot` group |
| Write to `/tmp/` | ✓ Allowed | World-writable (sticky bit) |
| Write to `/opt/homebrew/` (brew) | ✗ Blocked | Owned by `root:admin` |
| Write to `/usr/local/` (brew, npm -g) | ✗ Blocked | Owned by `root:wheel` |
| Read human's `~/.ssh/` | ✗ Blocked | 0700 owned by human |
| Read human's `~/.aws/` | ✗ Blocked | 0700 owned by human |
| Read another agent's `/var/bot/codex` | ✗ Blocked | Separate UNIX group, 2770 |
| Escalate via `sudo` | ✗ Blocked | Agent user not in sudoers |
| Send network requests | ✓ Allowed | No egress restrictions |
| Read inherited environment vars | ✗ Blocked | `env -i` strips everything |

The three most relevant real-world consequences:

- **`brew install`** — fails because the agent cannot write to
  `/opt/homebrew/` or `/usr/local/`. If you need an agent to install
  packages, delegate: `botadm run clio brew install` asks `sudo` for a
  password, which the human must provide interactively.

- **`npm install -g`** — fails for the same reason. Per-project
  `npm install` (without `-g`) works fine inside a shared project or agent
  namespace directory.

- **Credential exfiltration** — the agent has no access to `~/.ssh/`,
  `~/.aws/`, `~/.config/git/`, or any other sensitive path owned by the
  human. The sanitized environment ensures nothing leaks via variables either.

---

## 2. Architecture: Headless Namespaces

Each agent gets its own lightweight, templated layout under `/var/bot`. The
structure provides a system **User**, **Group**, and namespace **Workspace**
directory — nothing more.

```
                    /var/bot/ (0755 root:root)
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼

/var/bot/clio/                    /var/bot/codex/
Owner: you:clio                   Owner: you:codex
Permissions: 2770 (SGID)          Permissions: 2770 (SGID)
```

By assigning a dedicated, unique system group to each agent (e.g., group `clio`
for user `clio`), cross-agent read/write access is eliminated at the OS level.
Every agent also belongs to a shared supplementary group `bot` for multi-agent
project collaboration.

Here is the full permission scheme in practice:

```
# Sandbox infrastructure — each agent isolated by its own group
$ ls -la /var/bot/
drwxr-xr-x   root  root    .                       # world-traversable
drwxrws---   you   clio    clio/                   # agent clio's namespace (SGID)
drwxrws---   you   codex   codex/                  # agent codex's namespace (SGID)

$ ls -la /var/bot/clio/
drwxrws---   you   clio    .                       # namespace directory
-rw-r-----   clio  clio    env                     # environment file (0640)
-rw-r----r-- clio  clio    .zshrc                  # user shell startup configs
-rw-r----r-- clio  clio    .zprofile

# Project shared with all agents via the bot group
$ ls -la /path/to/project/
drwxrwx---   you   bot     .                       # owner rwx, group rwx, SGID
-rw-rw----   you   bot     README.md               # owner rw, group rw
-rw-rw----   you   bot     main.py
```

### How files are owned and accessed

| Directory | New file owned by | Human access | Agent access |
|---|---|---|---|
| Bot Namespace `/var/bot/clio` | creator:`clio` | via group or `sudo` | via group |
| Shared project (2770, SGID) | creator:`bot` | via `bot` group | via `bot` group |

The SGID bit on the namespace directory and on shared projects ensures files
inherit the directory's group regardless of who created them — no manual
`chgrp` after every edit.

### What you need to join

For the shared project, **you must join the `bot` group** — otherwise files
agents create there are owned by `clio:bot` and you have neither ownership nor
group access:

```bash
# Linux
sudo usermod -aG bot $USER
# macOS
sudo dseditgroup -o edit -a $USER -t user bot
```

### Sharing a project with bots: `botadm share`

To share an existing directory with all agents, use the `share` command:

```bash
sudo python3 src/laia.py share ~/my-project
```

This sets the group to `bot`, applies SGID (`g+rwxs`) so new files inherit the
`bot` group, and warns about sensitive entries (`.git/`, `.env`, `.aws/`) that
would become agent-readable. Use `--recursive` (`-r`) to also fix existing
files. Pass `--dry-run` (`-n`) to preview changes without touching the
filesystem.

If you prefer to set it up manually:

```bash
sudo chgrp bot ~/my-project
sudo chmod g+rwxs ~/my-project
```

For an agent's namespace directory, you have two choices:

- **Join the agent's group** — gives you direct read/write access to
  agent-created files without `sudo`:
  ```bash
  # Linux
  sudo usermod -aG clio $USER
  # macOS
  sudo dseditgroup -o edit -a $USER -t user clio
  ```
- **Use `sudo` or `botadm run`** — read agent output by running commands as
  the agent:
  ```bash
  sudo -u clio cat /var/bot/clio/output.txt
  botadm run clio cat /var/bot/clio/output.txt
  ```

Joining an agent's group does not reduce security — you already have root
access via `sudo`. The real isolation boundary is between agents, and that
remains intact: `codex` is not in group `clio` and cannot access `clio`'s files.

---

## 3. Reference Implementation

The commands below illustrate the manual setup. In practice these steps are
automated by `botadm` (see Section 4). Linux and macOS have different user
management toolchains, so both are shown; pick the one for your OS.

### Step 1: Create the Agent Group & User

**Linux** (shadow-utils):
```bash
export BOT="clio"

sudo groupadd "$BOT"
sudo useradd --system \
             --gid "$BOT" \
             --home-dir "/var/bot/$BOT" \
             --no-create-home \
             --shell /usr/sbin/nologin \
             "$BOT"
```

**macOS** (dscl):
```bash
export BOT="clio"

# Find a free GID in the agent range and create the group
sudo dscl . -create /Groups/"$BOT"
sudo dscl . -create /Groups/"$BOT" PrimaryGroupID 450
sudo dscl . -create /Groups/"$BOT" Password "*"

# Find a free UID and create the user
sudo dscl . -create /Users/"$BOT"
sudo dscl . -create /Users/"$BOT" UniqueID 450
sudo dscl . -create /Users/"$BOT" PrimaryGroupID 450
sudo dscl . -create /Users/"$BOT" NFSHomeDirectory "/var/bot/$BOT"
sudo dscl . -create /Users/"$BOT" UserShell /usr/bin/false
sudo dscl . -create /Users/"$BOT" RealName "Agent: $BOT"
sudo dscl . -create /Users/"$BOT" Password "*"
```

### Step 2: Establish the Namespace and Permissions

Create the shared `bot` group that all agents will belong to:

**Linux:**
```bash
sudo groupadd --force bot
```

**macOS:**
```bash
sudo dscl . -create /Groups/bot
sudo dscl . -create /Groups/bot PrimaryGroupID 440
sudo dscl . -create /Groups/bot Password "*"
```

Then set up the namespace hierarchy. The SGID bit on the directory is the key
mechanism: files created by either party inherit the bot's group, so explicit
`chown` after every operation is unnecessary.

```bash
# Create the workspace namespace directory
sudo mkdir -p "/var/bot/$BOT"

# You own the workspace; the agent's group has collaborative access
sudo chown -R $USER:"$BOT" "/var/bot/$BOT"

#  2 = SGID — new files inherit the $BOT group
# 770 = rwx for you and the bot, zero for everyone else
sudo chmod 2770 "/var/bot/$BOT"
```

### Step 3: Configure Sudo Rules

Authorize the host user to run commands as any bot user without permitting root
escalation. `botadm create` automates this, but the manual equivalent is:

```bash
# /etc/sudoers.d/bot-rules (created with visudo):
devuser ALL=(clio, codex) NOPASSWD: ALL
```

Pass `--no-sudoers` to `botadm create` to skip the automation, then add the
rule yourself. The bot-specific rule can be password-gated or `NOPASSWD` —
either way, it only governs access *to* the bot user, not the bot user's
access. The agent itself has no sudo privileges.

---

## 4. Automation Suite: `botadm`

`botadm` is a Python CLI that governs the full agent lifecycle — creation,
quarantine, teardown, and sandboxed execution. Two implementations ship with
the project:

| File | Platform | User/Group API |
|---|---|---|
| `src/laia.py` | macOS | `dscl` (Directory Service command line) |
| `src/laia_linux.py` | Linux | `groupadd`, `useradd`, `groupdel`, `userdel` |

Both expose the same interface (`init`, `create`, `update`, `shell`, `disable`,
`enable`, `destroy`, `share`, `noshare`, `run`) and share the same
architectural principles. Only the system-level CRUD operations differ.

### System Configuration Lifecycle

#### `botadm init`

Initializes the root namespace at `/var/bot` with strict ownership
(`root:root`) and permissions (`0755`). Also creates the shared `bot` group
that all agents will belong to as a supplementary group. Must be run once
before any other command.

#### `botadm create [--no-sudoers] [--force]`

Provisions a new bot namespace:

1. Creates a dedicated system group and headless system user (shell:
   `/usr/sbin/nologin` on Linux, `/usr/bin/false` on macOS).
2. Adds the agent user to the shared supplementary group `bot` for
   cross-agent project collaboration.
3. Creates the agent's single-directory namespace workspace (`2770` with SGID).
4. Assigns namespace ownership to the invoking (sudo) user and the bot's
   group.
5. Writes the `.zshrc`/`.zprofile` (macOS) or `.bashrc`/`.profile` (Linux) with
   custom colored prompts and fallback PS1 settings.
6. Saves default environment file (`/var/bot/<name>/env`) and changes ownership
   to the bot user.
7. By default, writes a validated sudoers drop-in to
   `/etc/sudoers.d/bot-<name>`. Pass `--no-sudoers` to skip.

On failure, all partially-created resources (group, user, directories) are
rolled back automatically.

#### `botadm update <bot>`

Refreshes configuration files, dotfiles, prompts, and environment definitions for
an existing namespace. Attempts to update the system user's login shell and
resets permissions. Does not edit the sudoers drop-in rules.

#### `botadm shell <bot> [--shell zsh|bash]`

Launches a login interactive shell inside the bot's namespace sandbox, ensuring
login init files (`.zprofile`/`.profile`) and environment configurations are
properly read.

#### `botadm disable`

Reversibly quarantines a bot:

- **Linux:** locks the account password (`usermod -L`), forces the shell to
  `/usr/sbin/nologin`.
- **macOS:** removes the `AuthenticationAuthority` (disables all login
  methods), forces the shell to `/usr/bin/false`.
- Both platforms: masks all namespace directories with `0000` permissions.

The sudoers rule and directory contents are preserved, allowing the bot to be
re-enabled later (via `botadm enable`) by restoring permissions and unlocking
the account.

#### `botadm enable`

Reverses `disable` quarantine: unlocks the password login database records, and
restores directory permissions back to `2770` with SGID.

#### `botadm destroy [--no-sudoers] [--force]`

Permanently removes a bot:

- Deletes the sudoers drop-in (unless `--no-sudoers` is given).
- Removes the system user (`userdel -r` on Linux; `dscl . -delete` on macOS).
- Removes the system group (`groupdel` on Linux; `dscl . -delete` on macOS).
- Deletes the entire namespace directory under `/var/bot/<name>`.

**This operation is irreversible.**

#### `botadm share [--recursive] [--dry-run] <path>`

Shares an existing directory with all agents:

- Sets group ownership to `bot`.
- Applies SGID (`g+rwxs`) so new files inherit the `bot` group.
- Warns about sensitive entries (`.git/`, `.env`, `.aws/`, etc.) that would
  become agent-readable.
- With `--recursive`, also updates existing files and subdirectories.
- With `--dry-run`, prints what would be done without modifying the filesystem.

The directory must not be a system path (`/etc`, `/usr`, `/var`, etc.).
Requires `init` to have been run first.

#### `botadm noshare [--group <group>] [--dry-run] <path>`

Stops sharing a directory with the agents:
- Restores group ownership to the human user's primary login group.
- Removes the SGID bit (`g-s`) and removes write permissions for group/others on
  the target path.
- Supports `--recursive` and `--dry-run`.

### Sandboxed Execution Context

#### `botadm run <bot> <command...>`

Executes a command inside the bot's sandbox:

1. **Clears the environment** — `env -i` strips all inherited variables.
2. **Sets a minimal whitelist** — only `HOME`, `USER`, `LOGNAME`, `PATH`,
   `TERM`, and `PWD` are defined, all pointing inside the sandbox namespace.
3. **Applies `umask 007`** — files created are group-readable/writable but
   world-inaccessible.
4. **Drops privileges** — delegates to the target bot user via `sudo -u`
   (not `sudo -i`, because the bot's shell is `nologin`/`false`).

---

## 5. Design Decisions

**Per-Bot Sudoers Drop-Ins**  
Each agent gets its own file at `/etc/sudoers.d/bot-<name>` rather than sharing
a single `bot-rules` file. This avoids parsing and editing sudoers syntax,
makes per-agent cleanup a simple `rm`, and limits the blast radius of a
malformed rule to one agent.

**Sudoers Automation (Opt-Out)**  
`botadm create` writes the `NOPASSWD` sudoers rule by default because the
sandbox is unusable without sudo access to the target user. The `--no-sudoers`
flag exists for environments with pre-existing sudoers management or strict
change-control policies.

**Password-Gated Sudo (Host User)**  
The bot-specific sudoers rule only governs access *to* the agent — it's a
convenience, not a security control. The real defense-in-depth is the host
user's own sudo access. On macOS, admin accounts have passwordless sudo by
default (via `%admin` group rules). If an agent somehow achieves host-level
code execution (e.g., via a shell injection that escapes the sandbox), that
passwordless sudo becomes an escalation path.

To password-gate your own sudo:

```bash
# Remove yourself from the admin group (macOS) — every sudo will prompt
sudo dseditgroup -o edit -d $USER -t user admin

# Or use a specific sudoers rule to require a password
# /etc/sudoers.d/apalala:
apalala ALL=(ALL) ALL
```

Trade-off: every `sudo` (including `botadm run`) will prompt for your
password. This is the intended defense — a prompt the agent cannot answer.

**`0440` on Sudoers Files**  
Drop-in files must be owned by `root:root` with `0440` permissions;
`visudo(8)` enforces this. The install helper sets `0440`, then validates with
`visudo -cf` before considering the rule active.

**`sudo -u` Not `sudo -i`**  
The agent's shell is `/usr/sbin/nologin` or `/usr/bin/false`, which rejects
interactive login. Using `sudo -i` would invoke the login shell and fail
immediately. `sudo -u` bypasses the login shell entirely and delegates
environment control to `env -i`.

**Disable vs Destroy**  
These are semantically distinct. `disable` is a reversible quarantine — the
account is locked, permissions stripped to `0000`, but data and sudoers rule
preserved. `destroy` is permanent teardown — user, group, data, and sudoers are
all removed. This separation lets you suspend an agent without losing state.

**Rollback Safety**  
Provisioning is multi-step (group → user → directories → permissions →
sudoers). If any step fails after partial progress, earlier steps are undone:
directories removed, user deleted, group deleted. This prevents orphaned system
accounts.

**Platform Validation**  
Each implementation checks for its required tools before making system changes:
Linux verifies `groupadd`, `useradd`, `groupdel`, `userdel`, `visudo`; macOS
verifies `dscl` and `visudo`. This fails fast with a clear diagnostic rather
than producing opaque errors mid-operation.

**Platform-Specific Implementations**  
The architecture's principles (user/group isolation, SGID collaboration, sudo
delegation) are OS-agnostic, but the tools to manipulate users and groups are
not. Linux uses shadow-utils; macOS uses the Directory Service (`dscl`). Rather
than abstracting this behind conditionals in a single script, we provide
separate, focused implementations — one per platform. They share the same CLI
interface, directory layout, and security model, making them drop-in
replacements. A single script would couple the two API surfaces, making every
change harder to test and review. Separating them keeps each implementation
simple, auditable, and idiomatic for its platform.

**Environment Hygiene**  
`env -i` strips all inherited variables; only a minimal whitelist is set. This
prevents the host user's `PATH`, API keys, or database credentials from leaking
into agent processes.

**`umask 007`**  
Files created inside the sandbox should be group-accessible (enabling
collaboration via SGID) but world-inaccessible. `007` grants group `rwx` while
stripping world access entirely.

---

## 6. Limitations & Future Work

**Network Isolation**  
This design covers filesystem and credential containment only. A compromised
agent with network access can still exfiltrate data. A future iteration could
wrap `botadm run` with `unshare -n` and `iptables`/`nftables` rules, or use
`bpfilter` to restrict outbound connections on a per-bot basis.

**Irreversibility**  
`botadm create` cleans up partial state on failure, but `botadm destroy` is
intentionally irreversible — the namespace, user, group, and sudoers rule are
all removed in one operation.

---

## 7. Recent Implementation Notes

The implementation in src/laia.py and src/laia_linux.py has been updated to
reflect the following operational decisions and features:

- Single-directory namespace: `/var/bot/<name>` is now the authoritative layout for
  both platforms. There are no sub-nested `home/` and `work/` subfolders.
- `AGENT_ENV` dict provides defaults (`HISTFILE`, `EDITOR`) and is persisted to
  `/var/bot/<name>/env`.
- env file ownership: `update` now attempts to chown the env file to the agent
  user (best-effort) so the agent can manage its live environment.
- macOS default shell: `zsh` for agents; Linux: `bash`. The `update` subcommand
  attempts to set the system user's login shell (best-effort) and emits a
  warning if it cannot.
- `create`/`update` now write standard user dotfiles (`.zprofile`/`.zshrc` on macOS,
  `.profile`/`.bashrc` on Linux) including a colored prompt and a PS1 fallback for
  programs that expect it.
- The `create` and `destroy` commands support `--force` / `-f` to skip interactive
  prompts.
- The `shell` subcommand launches a login interactive shell (`exec {shell} -l -i`),
  so `HOME` and login init files are applied correctly.

Smoke-test checklist (recommended):

1. `sudo python3 src/laia.py create testbot`
2. `sudo python3 src/laia.py update testbot`
3. `sudo python3 src/laia.py shell testbot`    # verify HOME, PROMPT, PS1
4. `sudo python3 src/laia.py disable testbot`  # verify account locked and perms 0000
5. `sudo python3 src/laia.py enable testbot`   # verify account unlocked and perms restored
6. `sudo python3 src/laia.py destroy testbot`  # confirm deletion (use --force to skip confirmation)

These changes are backward-compatible with the previous layout and preserve the
security model described in this document.

Operational gotchas and clarifications

- macOS authorization dialogs: Some `dscl` operations may trigger an interactive
  macOS authorization prompt (or require Terminal to be granted access in
  System Settings / Security & Privacy). Approve the prompt or grant access if
  the operation appears blocked.

- `update` flag differences: `create` accepts `--no-sudoers` to skip writing
  a sudoers drop-in. `update` does not accept `--no-sudoers` — it only
  refreshes configuration files and permissions for an existing namespace.

- env ownership semantics: The env file is written under the namespace with
  mode 0640. The implementation writes the file (initially root-owned) and
  then performs a best-effort `chown` to the bot user:bot group so the agent
  can manage its live environment. If chown fails (permission or lookup
  issues), the file remains readable by root and the bot group.

- Shell behavior and login shells: macOS agents default to `zsh`, Linux agents
  default to `bash`. The `update` subcommand attempts to set the system
  user's login shell (best-effort) and warns if it cannot. The `shell`
  subcommand launches a login interactive shell (`exec {shell} -l -i`) so
  HOME and login init files are applied. Non-login `su`/`sudo` invocations may
  preserve the invoking user's HOME unless run as a login session (e.g.
  `sudo -u bot -i`).

- dscl UID/GID allocation: On macOS, dscl UID/PrimaryGroupID allocation can
  fail if the chosen ID collides or if system policies block changes. If a
  create fails during UID assignment, re-run the create (or inspect the
  directory service) and consider choosing a different UID range. The tool
  attempts best-effort cleanup on partial failures.

- visudo validation: The sudoers drop-in is validated with `visudo -cf` and
  removed if invalid. If you maintain sudoers centrally, use `--no-sudoers`
  during create and add your rule through your change-control process.

- Shared library dependencies: If a sandboxed command relies on dynamic libraries
  (such as `.dylib` files on macOS or `.so` files on Linux, e.g. Node.js binary
  dependencies), those runtime libraries and their parent paths must also grant
  world-readable and traversal (`o+rx`) permissions. If they are blocked, the OS
  loader will raise a permission denied (e.g. dyld library loading failed) or a
  missing command error.

---

  ## Quick Start

> **Platforms:** macOS — `laia.py` (dscl). Linux — `laia_linux.py`
> (shadow-utils `groupadd`/`useradd`). The principles are identical; the
> implementations differ because user and group management APIs are OS-native
> and irreducibly platform-specific.

These steps take you from zero to two collaborating agents in under a minute.

```bash
# 1. Initialize the sandbox root
sudo python3 src/laia.py init

# 2. Create two agents
sudo python3 src/laia.py create clio
sudo python3 src/laia.py create codex

# NOTE: When the tool creates the shared group ("bot") it will best-effort add
# the invoking user so you can immediately access shared paths. If you created
# the group manually or need to add yourself, run:
# macOS:
#   sudo dseditgroup -o edit -a $USER -t user bot
# Linux:
#   sudo usermod -aG bot $USER

# 3. Share your project directory with all agents (default group 'bot')
mkdir -p ~/laia-test
sudo python3 src/laia.py share ~/laia-test

# 4. Have each party create a file
echo "hello from human" > ~/laia-test/human.txt
cd /tmp && sudo -u clio sh -c 'echo "hello from clio"  > ~/laia-test/clio.txt'
cd /tmp && sudo -u codex sh -c 'echo "hello from codex" > ~/laia-test/codex.txt'

# 5. Verify everyone can read everything
cat ~/laia-test/clio.txt
cat ~/laia-test/codex.txt

# 6. Verify cross-agent isolation (clio cannot access codex's workspace)
sudo -u clio ls /var/bot/codex
# should print permission denied / Operation not permitted

# 7. Run a command inside clio's sandbox
python3 src/laia.py run clio whoami   # prints: clio

# 8. To stop sharing and remove the group (restores ownership to your primary group)
sudo python3 src/laia.py noshare ~/laia-test
```
