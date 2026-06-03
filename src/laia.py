#!/usr/bin/env python3
"""
Dynamic multi-agent sandbox controller (macOS / dscl).
Handles lifecycle management (init, create, disable, destroy) and secure
execution using the macOS Directory Service command-line tool.
"""

import sys
import os
import shutil
import subprocess
import argparse
from pathlib import Path

BOTROOT = Path("/var/bot")
SUDOERS_DIR = Path("/etc/sudoers.d")
AGENT_UID_MIN = 450
AGENT_UID_MAX = 499

# Agent environment defaults — editable dict for future learning/extensions.
AGENT_ENV = {
    "HISTFILE": "$HOME/.history",
    "EDITOR": "nvim",
}


def checkroot():
    """Ensure administrative actions are running with root privileges."""
    if os.getuid() != 0:
        print("Error: This subcommand requires root privileges (sudo).", file=sys.stderr)
        sys.exit(1)


def getuser():
    """Find the real non-root user who invoked sudo to assign workspace ownership."""
    sudouser = os.environ.get("SUDO_USER")
    if sudouser and sudouser != "root":
        return sudouser
    try:
        return os.getlogin()
    except OSError:
        return "root"


def _check_platform():
    """Verify required macOS tools exist."""
    for tool in ("dscl", "visudo"):
        if not shutil.which(tool):
            print(
                f"Error: Required tool '{tool}' not found. "
                "This implementation requires macOS with dscl.",
                file=sys.stderr,
            )
            sys.exit(1)


def _dscl(*args):
    """Run dscl with the given arguments; return CompletedProcess."""
    return subprocess.run(
        ["dscl", ".", *args],
        check=True, capture_output=True, text=True,
    )


def _dscl_quiet(*args):
    """Run dscl silently; return True on success, False on failure."""
    try:
        _dscl(*args)
        return True
    except subprocess.CalledProcessError:
        return False


def _free_id(entity, id_attr, min_id=AGENT_UID_MIN, max_id=AGENT_UID_MAX):
    """Find the next free ID in the local directory service."""
    result = subprocess.run(
        ["dscl", ".", "-list", f"/{entity}", id_attr],
        capture_output=True, text=True,
    )
    used = set()
    for line in result.stdout.strip().split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            try:
                used.add(int(parts[-1]))
            except ValueError:
                pass
    for i in range(min_id, max_id + 1):
        if i not in used:
            return i
    return max(used) + 1 if used else min_id


def _install_sudoers(bot):
    """Create a sudoers drop-in allowing the invoking user to run as the bot."""
    sudoers_path = SUDOERS_DIR / f"bot-{bot}"
    realuser = getuser()

    if sudoers_path.exists():
        sudoers_path.unlink()

    content = f"{realuser} ALL=({bot}) NOPASSWD: ALL\n"
    sudoers_path.write_text(content)
    sudoers_path.chmod(0o440)

    try:
        subprocess.run(
            ["visudo", "-cf", str(sudoers_path)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        sudoers_path.unlink()
        print(
            "Warning: sudoers validation failed; rule file removed. "
            "Add the rule manually with visudo.",
            file=sys.stderr,
        )
        return False

    print(f"-> Sudoers rule added: {sudoers_path}")
    return True


def _remove_sudoers(bot):
    """Remove the sudoers drop-in for the given bot."""
    sudoers_path = SUDOERS_DIR / f"bot-{bot}"
    if sudoers_path.exists():
        sudoers_path.unlink()
        print(f"-> Sudoers rule removed: {sudoers_path}")


def _ensure_bot_group():
    """Create the shared 'bot' group if it doesn't already exist.

    When the group is created by this tool, add the invoking human user to it
    (best-effort). The invoking user is determined via getuser(); if it is
    'root' or absent, skip adding.
    """
    import grp
    try:
        grp.getgrnam("bot")
        return True
    except KeyError:
        pass
    gid = _free_id("Groups", "PrimaryGroupID", min_id=440, max_id=449)
    ok = _dscl_quiet("-create", "/Groups/bot") and \
         _dscl_quiet("-create", "/Groups/bot", "PrimaryGroupID", str(gid)) and \
         _dscl_quiet("-create", "/Groups/bot", "Password", "*")
    if ok:
        print(f"-> Shared group 'bot' created (GID {gid}).")
        # Best-effort: add the invoking human to the group so they can access shared paths
        try:
            realuser = getuser()
            if realuser and realuser != "root":
                _add_to_bot_group(realuser)
        except Exception:
            pass
    else:
        print("Warning: could not create shared group 'bot'.", file=sys.stderr)
    return ok


def _add_to_bot_group(bot):
    """Add an agent user to the shared 'bot' supplementary group."""
    try:
        _dscl("-merge", "/Groups/bot", "GroupMembership", bot)
        print(f"-> User '{bot}' added to shared group 'bot'.")
        return True
    except subprocess.CalledProcessError as err:
        print(f"Warning: could not add '{bot}' to 'bot' group: {err}", file=sys.stderr)
        return False


def _is_agent_user(name):
    """Return True if the given username is an agent account.

    Heuristics:
    - passwd GECOS (pw_gecos) starts with 'Agent:'
    - or the user's home directory is under BOTROOT
    """
    try:
        import pwd
        p = pwd.getpwnam(name)
        gecos = (p.pw_gecos or "").strip()
        home = p.pw_dir or ""
        if gecos.startswith("Agent:"):
            return True
        if str(Path(home)).startswith(str(BOTROOT)):
            return True
    except KeyError:
        return False
    except Exception:
        return False
    return False


def _is_bot_group(name):
    """Return True if the named group appears related to bot accounts.

    Criteria:
    - group name 'bot' is always bot-related
    - group has at least one member whose home is under BOTROOT or whose GECOS starts with 'Agent:'
    - or a user with the same name exists and is an agent
    """
    if name == "bot":
        return True
    try:
        import grp
        g = grp.getgrnam(name)
        # direct user with same name
        if _is_agent_user(name):
            return True
        for member in g.gr_mem:
            if _is_agent_user(member):
                return True
    except KeyError:
        return False
    except Exception:
        return False
    return False


def cmdinit(args):
    """Initialize the master sandbox container directory at /var/bot."""
    checkroot()
    _check_platform()
    print(f"Initializing master sandbox container at {BOTROOT}...")
    BOTROOT.mkdir(parents=True, exist_ok=True)
    os.chown(str(BOTROOT), 0, 0)
    BOTROOT.chmod(0o755)
    _ensure_bot_group()
    print("Initialization complete.")


def cmdcreate(args):
    """Provision a new isolated agent namespace on macOS (single-directory layout)."""
    checkroot()
    _check_platform()
    bot = args.bot

    if not BOTROOT.exists():
        if getattr(args, 'dry_run', False):
            print(f"[DRY RUN] {BOTROOT} is not initialized. 'init' would be required.")
        else:
            print(f"Error: {BOTROOT} is not initialized. Run 'botadm init' first.", file=sys.stderr)
            sys.exit(1)

    # If the system user already exists, ensure it is an agent account
    if _dscl_quiet("-read", f"/Users/{bot}"):
        if not _is_agent_user(bot):
            print(f"Error: system user '{bot}' exists but is not an agent account.", file=sys.stderr)
            sys.exit(1)

    botdir = BOTROOT / bot

    if botdir.exists():
        print(f"Error: Agent '{bot}' already exists.", file=sys.stderr)
        sys.exit(1)

    print(f"Provisioning isolated sandbox for agent: {bot}")

    group_created = False
    user_created = False
    dirs_created = False

    # Determine invoking user and their PATH
    realuser = getuser()

    # Check if system group/user already exist — reuse when present
    group_exists = _dscl_quiet("-read", f"/Groups/{bot}")
    user_exists = _dscl_quiet("-read", f"/Users/{bot}")
    try:
        real_path = subprocess.run(
            ["sudo", "-u", realuser, "printenv", "PATH"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        real_path = os.environ.get("PATH", "")

    # Preview configuration and prompt for confirmation
    print("\nConfiguration:")
    print(f"  Agent: {bot}")
    print(f"  Namespace directory: {botdir}")
    print("  User shell: /bin/zsh")
    print(f"  Inherited PATH: {real_path or '(empty)'}")
    if not getattr(args, "force", False):
        print("\nProceed with creation? [y/N]: ", end="")
        try:
            ans = input().strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("Aborted by user.")
            sys.exit(1)
    else:
        print("Proceeding without prompting (force).")

    try:
        # Support dry-run to preview creation steps without making changes
        if getattr(args, 'dry_run', False):
            print("[DRY RUN] Would perform the following actions:")
            if not group_exists:
                print(f"  - Create group '{bot}' (GID: auto-assigned)")
            else:
                print(f"  - Reuse existing group '{bot}'")
            if not user_exists:
                print(f"  - Create user '{bot}' (UID: auto-assigned)")
            else:
                print(f"  - Reuse existing user '{bot}'")
            print(f"  - Create namespace directory: {botdir}")
            print("  - Set ownership: owner=<invoking user>, group=<bot>")
            print("  - Set directory mode: 2770 (SGID)")
            print("  - Write env and dotfiles under the namespace")
            if not args.no_sudoers:
                print(f"  - Install sudoers drop-in for '{bot}' (visudo-validated)")
            print("Dry-run: no changes will be made.")
            return

        # Create group only if it does not already exist
        if not group_exists:
            gid = _free_id("Groups", "PrimaryGroupID")
            _dscl("-create", f"/Groups/{bot}")
            _dscl("-create", f"/Groups/{bot}", "PrimaryGroupID", str(gid))
            _dscl("-create", f"/Groups/{bot}", "Password", "*")
            group_created = True
            print(f"-> Group '{bot}' created (GID {gid}).")
        else:
            print(f"-> Group '{bot}' already exists; reusing it.")
            # try to find its GID for later ownership operations
            try:
                import grp
                gid = grp.getgrnam(bot).gr_gid
            except Exception:
                gid = None

        # Create user only if it does not already exist
        if not user_exists:
            uid = _free_id("Users", "UniqueID")
            _dscl("-create", f"/Users/{bot}")
            _dscl("-create", f"/Users/{bot}", "UniqueID", str(uid))
            # if group was created above use its gid, otherwise let dscl decide
            if gid is not None:
                _dscl("-create", f"/Users/{bot}", "PrimaryGroupID", str(gid))
            _dscl("-create", f"/Users/{bot}", "NFSHomeDirectory", str(botdir))
            _dscl("-create", f"/Users/{bot}", "UserShell", "/bin/zsh")
            _dscl("-create", f"/Users/{bot}", "RealName", f"Agent: {bot}")
            _dscl("-create", f"/Users/{bot}", "Password", "*")
            user_created = True
            print(f"-> User '{bot}' created (UID {uid}).")
        else:
            print(f"-> User '{bot}' already exists; reusing it.")

        # Create namespace directory
        botdir.mkdir(parents=True, exist_ok=True)
        dirs_created = True

        import pwd
        import grp
        try:
            botuid = pwd.getpwnam(bot).pw_uid
            botgid = grp.getgrnam(bot).gr_gid
            realuid = pwd.getpwnam(realuser).pw_uid
        except KeyError as err:
            raise RuntimeError(f"System lookup failure: {err}")

        # Set ownership and permissions: owner=invoking user, group=bot, SGID
        os.chown(str(botdir), realuid, botgid)
        botdir.chmod(0o2770)

        # Persist environment (store as key=value lines)
        env_dict = AGENT_ENV.copy()
        env_dict["PATH"] = real_path
        _laia_env = botdir / "env"
        _laia_env.write_text("\n".join(f"{k}={v}" for k, v in env_dict.items()) + "\n")
        _laia_env.chmod(0o640)
        try:
            os.chown(str(_laia_env), botuid, botgid)
        except Exception:
            pass

        # Create standard zsh config files owned by the bot user
        zprofile = botdir / ".zprofile"
        zprofile_contents = "\n".join(f'export {k}="{v}"' for k, v in env_dict.items()) + "\n"
        zprofile.write_text(zprofile_contents)
        zprofile.chmod(0o644)
        os.chown(str(zprofile), botuid, botgid)

        zshrc = botdir / ".zshrc"
        zshrc_contents = '[[ -f /etc/zshrc ]] && source /etc/zshrc\n'
        zshrc_contents += "PROMPT='%F{green}%n@%m%f:%F{blue}%~%f$ '\n"
        zshrc_contents += "# Fallback for programs expecting PS1\n"
        zshrc_contents += "PS1='%F{green}%n@%m%f:%F{blue}%~%f$ '\n"
        zshrc.write_text(zshrc_contents)
        zshrc.chmod(0o644)
        os.chown(str(zshrc), botuid, botgid)

    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        if dirs_created:
            shutil.rmtree(botdir, ignore_errors=True)
        if user_created:
            _dscl_quiet("-delete", f"/Users/{bot}")
        if group_created:
            _dscl_quiet("-delete", f"/Groups/{bot}")
        sys.exit(1)

    # Add agent to the shared bot group (non-fatal if it fails)
    _ensure_bot_group()
    _add_to_bot_group(bot)

    sudoers_installed = False
    if not args.no_sudoers:
        sudoers_installed = _install_sudoers(bot)

    print(f"\nSuccess! Agent '{bot}' created.")
    print(f"-> Namespace: {botdir}")
    if sudoers_installed:
        print("-> Sudoers: automatically configured")
    else:
        realuser = getuser()
        print("\nTo enable execution, add this sudoers rule:")
        print(f"  {realuser} ALL=({bot}) NOPASSWD: ALL")


def cmdupdate(args):
    """Recreate configuration files for an existing bot namespace (macOS)."""
    checkroot()
    bot = args.bot

    botdir = BOTROOT / bot
    if not botdir.exists():
        print(f"Error: Namespace '{botdir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Ensure system user exists
    if not _dscl_quiet("-read", f"/Users/{bot}"):
        print(f"Error: System user '{bot}' not found.", file=sys.stderr)
        sys.exit(1)

    # Determine invoking user and PATH
    realuser = getuser()
    try:
        real_path = subprocess.run(
            ["sudo", "-u", realuser, "printenv", "PATH"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        real_path = os.environ.get("PATH", "")

    import pwd
    import grp
    try:
        botuid = pwd.getpwnam(bot).pw_uid
        botgid = grp.getgrnam(bot).gr_gid
        realuid = pwd.getpwnam(realuser).pw_uid
    except KeyError as err:
        print(f"Error: system lookup failed: {err}", file=sys.stderr)
        sys.exit(1)

    # Ensure directory ownership and permissions
    os.chown(str(botdir), realuid, botgid)
    botdir.chmod(0o2770)

    # Persist environment (store as key=value lines)
    env_dict = AGENT_ENV.copy()
    env_dict["PATH"] = real_path
    _laia_env = botdir / "env"
    _laia_env.write_text("\n".join(f"{k}={v}" for k, v in env_dict.items()) + "\n")
    _laia_env.chmod(0o640)
    try:
        os.chown(str(_laia_env), botuid, botgid)
    except Exception:
        pass

    # Recreate zsh configuration files owned by the bot user
    zprofile = botdir / ".zprofile"
    zprofile_contents = "\n".join(f'export {k}="{v}"' for k, v in env_dict.items()) + "\n"
    zprofile.write_text(zprofile_contents)
    zprofile.chmod(0o644)
    os.chown(str(zprofile), botuid, botgid)

    zshrc = botdir / ".zshrc"
    zshrc_contents = '[[ -f /etc/zshrc ]] && source /etc/zshrc\n'
    zshrc_contents += "PROMPT='%F{green}%n@%m%f:%F{blue}%~%f$ '\n"
    zshrc_contents += "# Fallback for programs expecting PS1\n"
    zshrc_contents += "PS1='%F{green}%n@%m%f:%F{blue}%~%f$ '\n"
    zshrc.write_text(zshrc_contents)
    zshrc.chmod(0o644)
    os.chown(str(zshrc), botuid, botgid)

    # Attempt to set the user's login shell to zsh (best-effort)
    try:
        try:
            _dscl("-create", f"/Users/{bot}", "UserShell", "/bin/zsh")
            print("-> User shell set to /bin/zsh.")
        except subprocess.CalledProcessError:
            try:
                _dscl("-change", f"/Users/{bot}", "UserShell", "/bin/bash", "/bin/zsh")
                print("-> User shell changed to /bin/zsh.")
            except subprocess.CalledProcessError:
                try:
                    _dscl("-change", f"/Users/{bot}", "UserShell", "/usr/bin/false", "/bin/zsh")
                    print("-> User shell changed to /bin/zsh.")
                except subprocess.CalledProcessError:
                    print("Warning: could not set user shell to /bin/zsh (non-fatal).", file=sys.stderr)
    except Exception:
        # Best-effort; do not fail the update if dscl changes cannot be made
        pass

    print(f"-> Configuration for '{bot}' updated.")


def cmddisable(args):
    """Quarantine an agent on macOS: lock account, strip filesystem permissions."""
    checkroot()
    bot = args.bot
    print(f"Disabling agent: {bot}")

    try:
        _dscl("-delete", f"/Users/{bot}", "AuthenticationAuthority")
        _dscl("-create", f"/Users/{bot}", "UserShell", "/usr/bin/false")
        print(f"-> Account '{bot}' locked (auth removed, shell disabled).")
    except subprocess.CalledProcessError as err:
        print(f"Warning: could not modify user '{bot}': {err}", file=sys.stderr)

    botdir = BOTROOT / bot
    if botdir.exists():
        for subdir in botdir.iterdir():
            subdir.chmod(0o000)
        botdir.chmod(0o000)
        print("-> Filesystem permissions stripped (0000 quarantine).")
    else:
        print(f"Error: Namespace for '{bot}' not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Agent '{bot}' quarantined.")


def cmdenable(args):
    """Re-enable an agent previously disabled (macOS)."""
    checkroot()
    bot = args.bot
    print(f"Enabling agent: {bot}")

    # Ensure system user exists
    if not _dscl_quiet("-read", f"/Users/{bot}"):
        print(f"Error: System user '{bot}' not found.", file=sys.stderr)
        sys.exit(1)

    # Restore shell to zsh where possible
    try:
        try:
            _dscl("-create", f"/Users/{bot}", "UserShell", "/bin/zsh")
        except subprocess.CalledProcessError:
            try:
                _dscl("-change", f"/Users/{bot}", "UserShell", "/usr/bin/false", "/bin/zsh")
            except subprocess.CalledProcessError:
                # best-effort; continue
                pass
    except Exception:
        pass

    botdir = BOTROOT / bot
    if botdir.exists():
        import pwd
        import grp
        try:
            botgid = grp.getgrnam(bot).gr_gid
            realuser = getuser()
            realuid = pwd.getpwnam(realuser).pw_uid
        except KeyError as err:
            print(f"Warning: system lookup failed: {err}", file=sys.stderr)
            realuid = 0
            botgid = 0
        try:
            os.chown(str(botdir), realuid, botgid)
            for entry in botdir.iterdir():
                try:
                    if entry.is_dir():
                        entry.chmod(0o2770)
                    else:
                        entry.chmod(0o660)
                except Exception:
                    pass
            botdir.chmod(0o2770)
            print("-> Filesystem permissions restored (best-effort).")
        except Exception as err:
            print(f"Warning: could not restore permissions: {err}", file=sys.stderr)

    print(f"Agent '{bot}' enabled.")


def cmddestroy(args):
    """Completely remove an agent on macOS: user, group, directory, sudoers."""
    checkroot()
    _check_platform()
    bot = args.bot

    print(f"Destroying agent: {bot}")
    print("This will remove the namespace directory and sudoers rule. The system user and group will be preserved.")
    if not getattr(args, 'force', False):
        print("Type the agent name to confirm: ", end="")
        try:
            ans = input().strip()
        except EOFError:
            ans = ""
        if ans != bot:
            print("Aborted.")
            return
    else:
        print("Force: skipping confirmation.")

    if not args.no_sudoers:
        _remove_sudoers(bot)

    # Do NOT delete the system user or group; leave them in place.
    # Previously we removed the user and group. That behavior is unsafe when
    # the system user may be shared or managed externally.
    
    botdir = BOTROOT / bot
    if botdir.exists():
        shutil.rmtree(botdir)
        print(f"-> Directory {botdir} removed.")
    else:
        print(f"-> Directory {botdir} did not exist.")

    print(f"Agent '{bot}' destroyed (namespace and sudoers cleaned; user/group preserved).")




def cmdshare(args):
    """Share paths with a named group (macOS).

    - Default group: 'bot'.
    - Default path: current working directory when --path omitted.
    - When --bots provided, replace group's membership with the exact list.
    """
    checkroot()
    _check_platform()
    import grp
    import pwd

    group = args.group or "bot"
    # parse targets from --path
    if getattr(args, 'path', None):
        targets = [Path(p).resolve() for p in args.path.split(",") if p.strip()]
    else:
        targets = [Path.cwd()]

    # parse bots
    bots = []
    if getattr(args, 'bots', None):
        bots = [b.strip() for b in args.bots.split(",") if b.strip()]

    try:
        grp.getgrnam(group)
        group_exists = True
    except KeyError:
        group_exists = False

    if args.create:
        if group_exists:
            print(f"Error: Group '{group}' already exists; --create must fail if group exists.", file=sys.stderr)
            sys.exit(1)
        gid = _free_id("Groups", "PrimaryGroupID")
        ok = _dscl_quiet("-create", f"/Groups/{group}") and \
             _dscl_quiet("-create", f"/Groups/{group}", "PrimaryGroupID", str(gid)) and \
             _dscl_quiet("-create", f"/Groups/{group}", "Password", "*")
        if not ok:
            print(f"Error: failed to create group '{group}'", file=sys.stderr)
            sys.exit(1)
        print(f"-> Group '{group}' created (GID {gid}).")
        group_exists = True
        # When we created the group, add the invoking user so they can access shared paths
        try:
            realuser = getuser()
            if realuser and realuser != "root":
                _add_to_bot_group(realuser)
        except Exception:
            pass

    if not group_exists and not args.dry_run:
        print(f"Error: Group '{group}' does not exist. Use --create to create it.", file=sys.stderr)
        sys.exit(1)

    # Validate that the group is bot-related
    if not _is_bot_group(group):
        # Allow creation case: if --create was used we accept the new group as bot-related
        if not args.create:
            print(f"Error: Group '{group}' does not appear to be bot-related.", file=sys.stderr)
            sys.exit(1)

    # validate targets
    system_roots = {"/etc", "/usr", "/var", "/System", "/bin", "/sbin",
                    "/opt", "/Library", "/Network", "/home"}
    for path in targets:
        if not path.is_dir():
            print(f"Error: '{path}' is not a directory.", file=sys.stderr)
            sys.exit(1)
        if str(path) in system_roots:
            print(f"Error: Refusing to share system root '{path}'.", file=sys.stderr)
            sys.exit(1)
        for parent in path.parents:
            if str(parent) in system_roots:
                print(f"Error: Refusing to share '{path}' under system path '{parent}'.", file=sys.stderr)
                sys.exit(1)
        if str(path) == str(BOTROOT):
            print(f"Error: Refusing to share bot root '{BOTROOT}'. Share individual agent workdirs instead.", file=sys.stderr)
            sys.exit(1)

    # confirmation
    if not args.force and not args.dry_run:
        print(f"About to operate on group='{group}' for paths: {', '.join(map(str, targets))}")
        if bots:
            print(f"Bots: {', '.join(bots)}")
        ok = input("Proceed? [y/N]: ").strip().lower()
        if ok != "y":
            print("Aborting.")
            sys.exit(1)

    # set exact membership when --bots provided
    if bots:
        # Verify each provided bot is an agent account
        for m in bots:
            if not _dscl_quiet("-read", f"/Users/{m}"):
                print(f"Error: specified bot '{m}' does not exist.", file=sys.stderr)
                sys.exit(1)
            if not _is_agent_user(m):
                print(f"Error: specified bot '{m}' exists but is not recognized as an agent account.", file=sys.stderr)
                sys.exit(1)

        if args.dry_run:
            print(f"[DRY RUN] Would set group '{group}' membership to: {', '.join(bots)}")
        else:
            try:
                result = subprocess.run(["dscl", ".", "-read", f"/Groups/{group}", "GroupMembership"], capture_output=True, text=True)
                current = []
                if result.returncode == 0:
                    parts = result.stdout.strip().split()
                    if len(parts) >= 2:
                        current = parts[1:]
            except subprocess.CalledProcessError:
                current = []

            # remove members not desired
            for m in list(current):
                if m not in bots:
                    _dscl_quiet("-delete", f"/Groups/{group}", "GroupMembership", m)
                    print(f"-> Removed '{m}' from '{group}'.")
            # add desired members
            for m in bots:
                if m not in current:
                    try:
                        _dscl("-merge", f"/Groups/{group}", "GroupMembership", m)
                        print(f"-> Added '{m}' to '{group}'.")
                    except subprocess.CalledProcessError as err:
                        print(f"Warning: could not add '{m}' to '{group}': {err}", file=sys.stderr)

    # apply chgrp and mode to targets
    chmod_mode = args.mode or "g+rwxs"
    for path in targets:
        if args.dry_run:
            print(f"[DRY RUN] Would set group={group} and mode={chmod_mode} on '{path}'")
        else:
            subprocess.run(["chgrp", group, str(path)], check=True)
            subprocess.run(["chmod", chmod_mode, str(path)], check=True)
            print(f"-> '{path}' group set to '{group}' and mode applied.")

    return


def cmdnoshare(args):
    """Restore group ownership to invoking user's primary group and delete the named group (macOS)."""
    checkroot()
    _check_platform()
    import grp
    import pwd

    group = args.group or "bot"
    if getattr(args, 'path', None):
        targets = [Path(p).resolve() for p in args.path.split(",") if p.strip()]
    else:
        targets = [Path.cwd()]

    try:
        grp.getgrnam(group)
    except KeyError:
        print(f"Error: Group '{group}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Verify group is bot-related
    if not _is_bot_group(group):
        print(f"Error: Group '{group}' does not appear to be bot-related.", file=sys.stderr)
        sys.exit(1)

    # confirmation
    if not args.force and not args.dry_run:
        print(f"About to remove sharing for group='{group}' on: {', '.join(map(str, targets))}")
        ok = input("Proceed? [y/N]: ").strip().lower()
        if ok != "y":
            print("Aborting.")
            sys.exit(1)

    mygid = pwd.getpwuid(os.getuid()).pw_gid
    mygroup = grp.getgrgid(mygid).gr_name
    for path in targets:
        if args.dry_run:
            print(f"[DRY RUN] Would restore group of '{path}' to '{mygroup}'")
        else:
            subprocess.run(["chgrp", mygroup, str(path)], check=True)
            print(f"-> Restored group of '{path}' to '{mygroup}'.")

    if args.dry_run:
        print(f"[DRY RUN] Would delete group '{group}'")
        return

    if _dscl_quiet("-delete", f"/Groups/{group}"):
        print(f"-> Group '{group}' deleted.")
    else:
        print(f"Error: failed to delete group '{group}'", file=sys.stderr)
        sys.exit(1)

    """Restore group ownership to invoking user's primary group and delete the named group (macOS)."""
    checkroot()
    _check_platform()
    import grp
    import pwd

    group = args.group or "bot"
    if getattr(args, 'path', None):
        targets = [Path(p).resolve() for p in args.path.split(",") if p.strip()]
    else:
        targets = [Path.cwd()]

    try:
        grp.getgrnam(group)
    except KeyError:
        print(f"Error: Group '{group}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # confirmation
    if not args.force and not args.dry_run:
        print(f"About to remove sharing for group='{group}' on: {', '.join(map(str, targets))}")
        ok = input("Proceed? [y/N]: ").strip().lower()
        if ok != "y":
            print("Aborting.")
            sys.exit(1)

    mygid = pwd.getpwuid(os.getuid()).pw_gid
    mygroup = grp.getgrgid(mygid).gr_name
    for path in targets:
        if args.dry_run:
            print(f"[DRY RUN] Would restore group of '{path}' to '{mygroup}'")
        else:
            subprocess.run(["chgrp", mygroup, str(path)], check=True)
            print(f"-> Restored group of '{path}' to '{mygroup}'.")

    if args.dry_run:
        print(f"[DRY RUN] Would delete group '{group}'")
        return

    if _dscl_quiet("-delete", f"/Groups/{group}"):
        print(f"-> Group '{group}' deleted.")
    else:
        print(f"Error: failed to delete group '{group}'", file=sys.stderr)
        sys.exit(1)


def cmdrun(args):
    """Wipe environment and execute a command inside the bot's sandbox."""
    bot = args.bot
    command = args.command

    botroot = BOTROOT / bot
    bothome = botroot
    botwork = botroot

    if not botroot.is_dir():
        print(f"Error: Sandbox '{botroot}' does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        cwd = os.getcwd()
    except OSError:
        cwd = str(botwork)

    _laia_env = botroot / "env"
    if _laia_env.exists():
        content = _laia_env.read_text()
        env_from_file = {}
        for line in content.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                env_from_file[k] = v
        _stored_path = env_from_file.get("PATH", "").strip()
        if not _stored_path:
            _stored_path = "/usr/local/bin:/usr/bin:/bin"
    else:
        _stored_path = "/usr/local/bin:/usr/bin:/bin"

    envargs = [
        f"HOME={bothome}",
        f"USER={bot}",
        f"LOGNAME={bot}",
        f"PATH={_stored_path}",
        "TERM=xterm-256color",
        f"PWD={cwd}",
        "PS1=\\[\\033[1;32m\\]\\u@{bot}\\[\\033[0m\\]:\\[\\033[1;34m\\]\\w\\[\\033[0m\\]\\$ ",
    ]

    sudocmd = [
        "sudo", "-u", bot,
        "env", "-i", *envargs,
        "zsh", "-c", f"cd '{cwd}' && umask 007 && exec \"$@\"",
        "--", *command,
    ]

    try:
        result = subprocess.run(sudocmd, check=True)
        sys.exit(result.returncode)
    except subprocess.CalledProcessError as err:
        sys.exit(err.returncode)
    except KeyboardInterrupt:
        sys.exit(130)


def cmdshell(args):
    """Launch an interactive shell inside the bot's sandbox."""
    bot = args.bot
    shell = args.shell

    botroot = BOTROOT / bot
    bothome = botroot
    botwork = botroot

    if not botroot.is_dir():
        print(f"Error: Sandbox '{botroot}' does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        cwd = os.getcwd()
    except OSError:
        cwd = str(botwork)

    _laia_env = botroot / "env"
    if _laia_env.exists():
        content = _laia_env.read_text()
        env_from_file = {}
        for line in content.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                env_from_file[k] = v
        _stored_path = env_from_file.get("PATH", "").strip()
        if not _stored_path:
            _stored_path = "/usr/local/bin:/usr/bin:/bin"
    else:
        _stored_path = "/usr/local/bin:/usr/bin:/bin"

    envargs = [
        f"HOME={bothome}",
        f"USER={bot}",
        f"LOGNAME={bot}",
        f"PATH={_stored_path}",
        "TERM=xterm-256color",
        f"PWD={cwd}",
        "PS1=\\[\\033[1;32m\\]\\u@{bot}\\[\\033[0m\\]:\\[\\033[1;34m\\]\\w\\[\\033[0m\\]\\$ ",
    ]

    sudocmd = [
        "sudo", "-u", bot,
        "env", "-i", *envargs,
        shell, "-c", f"cd '{cwd}' && exec {shell} -l -i",
    ]

    try:
        subprocess.run(sudocmd, check=True)
    except subprocess.CalledProcessError as err:
        sys.exit(err.returncode)
    except KeyboardInterrupt:
        sys.exit(130)


def main():
    parser = argparse.ArgumentParser(
        description="LAIA sandbox controller (macOS / dscl)."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("init", help="Initialize master /var/bot directory infrastructure.")

    parser_create = subparsers.add_parser("create", help="Provision a new isolated bot environment.")
    parser_create.add_argument("bot", help="Short name of the bot to provision.")
    parser_create.add_argument(
        "--no-sudoers", action="store_true",
        help="Skip automatic sudoers drop-in configuration.",
    )
    parser_create.add_argument(
        "--force", "-f", action="store_true",
        help="Skip interactive prompts (force).",
    )

    parser_update = subparsers.add_parser("update", help="Recreate configuration for an existing bot namespace.")
    parser_update.add_argument("bot", help="Short name of the bot to update configuration for.")

    parser_enable = subparsers.add_parser("enable", help="Re-enable a previously disabled bot namespace.")
    parser_enable.add_argument("bot", help="Short name of the bot to enable.")

    parser_disable = subparsers.add_parser("disable", help="Quarantine and lock an existing bot.")
    parser_disable.add_argument("bot", help="Short name of the bot to lock.")

    parser_destroy = subparsers.add_parser(
        "destroy", help="Completely remove a bot (user, group, dirs, sudoers)."
    )
    parser_destroy.add_argument("bot", help="Short name of the bot to destroy.")
    parser_destroy.add_argument(
        "--no-sudoers", action="store_true",
        help="Skip removing the sudoers drop-in rule.",
    )
    parser_destroy.add_argument(
        "--force", "-f", action="store_true",
        help="Skip interactive prompts (force).",
    )

    parser_share = subparsers.add_parser(
        "share", help="Share a directory or paths with a named group (default group 'bot')."
    )
    parser_share.add_argument(
        "--group", "-g", default="bot",
        help="Group name to operate on (default: 'bot'). Use the name exactly as provided.",
    )
    # Mutually exclusive: create OR bots
    mutex = parser_share.add_mutually_exclusive_group()
    mutex.add_argument("--create", action="store_true", help="Create the named group. Fails if the group already exists.")
    mutex.add_argument("--bots", "-b", help="Comma-separated list of bot user names; when provided set membership to exactly this list.")

    parser_share.add_argument(
        "--path", help="Comma-separated list of target paths to operate on (default: current working directory).",
    )
    parser_share.add_argument(
        "--mode", help="Filesystem mode to apply to targets (symbolic chmod string, default: g+rwxs).",
    )
    parser_share.add_argument(
        "--dry-run", "-n", action="store_true", help="Show actions without making changes.",
    )
    parser_share.add_argument("--force", "-f", action="store_true", help="Skip interactive confirmation prompts.")
    parser_share.add_argument("--verbose", action="store_true", help="Print detailed action logs.")

    # 'noshare' — inverse of share: restore ownership and delete a group
    parser_noshare = subparsers.add_parser("noshare", help="Stop sharing: restore group ownership to invoking user and delete a group.")
    parser_noshare.add_argument("--group", "-g", default="bot", help="Group name to remove (default: bot). Use the name exactly as provided.")
    parser_noshare.add_argument("--path", help="Comma-separated list of target paths (default: current working directory).")
    parser_noshare.add_argument("--dry-run", "-n", action="store_true", help="Show actions without making changes.")
    parser_noshare.add_argument("--force", "-f", action="store_true", help="Skip interactive confirmation prompts.")
    parser_noshare.add_argument("--verbose", action="store_true", help="Print detailed action logs.")

    parser_run = subparsers.add_parser("run", help="Run a command securely inside a bot's sandbox.")
    parser_run.add_argument("bot", help="Name of the bot container to execute in.")
    parser_run.add_argument("command", nargs=argparse.REMAINDER, help="Command and arguments to run.")

    parser_shell = subparsers.add_parser(
        "shell", help="Launch an interactive shell inside a bot's sandbox."
    )
    parser_shell.add_argument("bot", help="Name of the bot container to enter.")
    parser_shell.add_argument(
        "--shell", "-s", default="zsh",
        help="Shell to launch (default: zsh).",
    )

    args = parser.parse_args()

    commands = {
        "init": cmdinit,
        "create": cmdcreate,
        "update": cmdupdate,
        "enable": cmdenable,
        "disable": cmddisable,
        "destroy": cmddestroy,
        "share": cmdshare,
        "noshare": cmdnoshare,
        "run": cmdrun,
        "shell": cmdshell,
    }

    commands[args.action](args)


if __name__ == "__main__":
    main()
