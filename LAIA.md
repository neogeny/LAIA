# Local Agent Isolation Architecture
## Multi-Agent Sandboxing via Headless UNIX Namespaces

> **Platform:** Linux with shadow-utils (`groupadd`, `useradd`, `visudo`). Not
> compatible with macOS or BSD out of the box.

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

---

## 2. Architecture: Headless Namespaces

Each agent gets its own lightweight, templated layout under `/var/bot`. The
structure provides a system **User**, **Group**, **Home**, and shared
**Work** directory — nothing more.

```
                    /var/bot/ (0755 root:root)
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼

/var/bot/clio/                    /var/bot/codex/
Owner: clio:clio                  Owner: codex:codex
├── home/ (0700)                  ├── home/ (0700)
└── work/ (2770, SGID)            └── work/ (2770, SGID)
```

By assigning a dedicated, unique system group to each agent (e.g., group `clio`
for user `clio`), cross-agent read/write access is eliminated at the OS level.
The SGID bit on `work/` ensures files created by either the host user or the
agent inherit the bot's group ownership, making collaboration seamless without
world-readable permissions.

---

## 3. Reference Implementation

The commands below illustrate the manual setup. In practice these steps are
automated by `botadm` (see Section 4).

### Step 1: Create the Agent Group & User

```bash
export BOT="clio"

# Create a dedicated system group for this agent
sudo groupadd "$BOT"

# Create a headless system user tied to that group
sudo useradd --system \
             --gid "$BOT" \
             --home-dir "/var/bot/$BOT/home" \
             --create-home \
             --shell /usr/sbin/nologin \
             "$BOT"
```

### Step 2: Establish the Namespace and Permissions

The SGID bit on `work/` is the key mechanism: files created by either party
inherit the bot's group, so explicit `chown` after every operation is
unnecessary.

```bash
# Create the workspace and home hierarchy
sudo mkdir -p "/var/bot/$BOT/work"

# You own the workspace; the agent's group has collaborative access
sudo chown -R $USER:"$BOT" "/var/bot/$BOT/work"

#  2 = SGID — new files inherit the $BOT group
# 770 = rwx for you and the bot, zero for everyone else
sudo chmod 2770 "/var/bot/$BOT/work"

# Lock down the agent's private home directory
sudo chmod 700 "/var/bot/$BOT/home"
sudo chown "$BOT":"$BOT" "/var/bot/$BOT/home"
```

### Step 3: Configure Sudo Rules

Authorize the host user to run commands as any bot user without permitting root
escalation. This step is automated by `botadm create` (Section 4), but the
manual equivalent is:

```bash
# /etc/sudoers.d/bot-rules (created with visudo):
devuser ALL=(clio, codex) NOPASSWD: ALL
```

`botadm create` writes a per-bot drop-in to `/etc/sudoers.d/bot-<name>` and
validates it with `visudo -cf`. Pass `--no-sudoers` to skip.

---

## 4. Automation Suite: `botadm`

`botadm` is a Python CLI that governs the full agent lifecycle — creation,
quarantine, teardown, and sandboxed execution.

### System Configuration Lifecycle

#### `botadm init`

Initializes the root namespace at `/var/bot` with strict ownership
(`root:root`) and permissions (`0755`). Must be run once before any other
command.

#### `botadm create [--no-sudoers]`

Provisions a new bot namespace:

1. Creates a dedicated system group and headless system user (shell:
   `/usr/sbin/nologin`).
2. Creates the agent's home directory (`0700`, permission-locked) and
   collaborative workspace (`2770` with SGID).
3. Assigns workspace ownership to the invoking (sudo) user and the bot's
   group.
4. By default, writes a validated sudoers drop-in to
   `/etc/sudoers.d/bot-<name>`. Pass `--no-sudoers` to skip.

On failure, all partially-created resources (group, user, directories) are
rolled back automatically.

#### `botadm disable`

Reversibly quarantines a bot:

- Locks the account password (`usermod -L`).
- Forces the shell to `/usr/sbin/nologin`.
- Masks all namespace directories with `0000` permissions.

The sudoers rule and directory contents are preserved, allowing the bot to be
re-enabled later by restoring permissions and unlocking the account.

#### `botadm destroy [--no-sudoers]`

Permanently removes a bot:

- Deletes the sudoers drop-in (unless `--no-sudoers` is given).
- Removes the system user (`userdel -r`, which also deletes the home
  directory).
- Removes the system group (`groupdel`).
- Deletes the entire namespace tree under `/var/bot/<name>`.

**This operation is irreversible.**

### Sandboxed Execution Context

#### `botadm run <bot> <command...>`

Executes a command inside the bot's sandbox:

1. **Clears the environment** — `env -i` strips all inherited variables.
2. **Sets a minimal whitelist** — only `HOME`, `USER`, `LOGNAME`, `PATH`,
   `TERM`, and `PWD` are defined, all pointing inside the sandbox.
3. **Applies `umask 007`** — files created are group-readable/writable but
   world-inaccessible.
4. **Drops privileges** — delegates to the target bot user via `sudo -u`
   (not `sudo -i`, because the bot's shell is `nologin`).

---

## 5. Design Decisions

**Per-Bot Sudoers Drop-Ins**  
Each agent gets its own file at `/etc/sudoers.d/bot-<name>` rather than sharing
a single `bot-rules` file. This avoids parsing and editing sudoers syntax,
makes per-agent cleanup a simple `rm`, and limits the blast radius of a
malformed rule to one agent.

**Sudoers Automation (Opt-Out)**  
`botadm create` writes the sudoers rule by default because the sandbox is
unusable without sudo access to the target user. The `--no-sudoers` flag
exists for environments with pre-existing sudoers management or strict
change-control policies.

**`0440` on Sudoers Files**  
Drop-in files must be owned by `root:root` with `0440` permissions;
`visudo(8)` enforces this. The install helper sets `0440`, then validates with
`visudo -cf` before considering the rule active.

**`sudo -u` Not `sudo -i`**  
The agent's shell is `/usr/sbin/nologin`, which rejects interactive login. Using
`sudo -i` would invoke `nologin` and fail immediately. `sudo -u` bypasses the
login shell entirely and delegates environment control to `env -i`.

**Disable vs Destroy**  
These are semantically distinct. `disable` is a reversible quarantine — the
account is locked, permissions stripped, but data and sudoers rule preserved.
`destroy` is permanent teardown — user, group, data, and sudoers are all
removed. This separation lets you suspend an agent without losing state.

**Rollback Safety**  
Provisioning is multi-step (group → user → directories → permissions →
sudoers). If any step fails after partial progress, earlier steps are undone:
directories removed, user deleted, group deleted. This prevents orphaned system
accounts.

**Platform Validation**  
Commands check for required tools (`groupadd`, `useradd`, `groupdel`,
`userdel`, `visudo`) before making any system changes. This fails fast with a
clear diagnostic rather than producing opaque errors mid-operation.

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
