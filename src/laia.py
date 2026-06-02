#!/usr/bin/env python3
"""
Dynamic multi-agent sandbox controller (macOS / dscl).
Handles lifecycle management (init, create, disable, destroy) and secure
execution using the macOS Directory Service command-line tool.
"""

import sys
import os
import shutil
import re
import subprocess
import argparse
from pathlib import Path

BOTROOT = Path("/var/bot")
SUDOERS_DIR = Path("/etc/sudoers.d")
AGENT_UID_MIN = 450
AGENT_UID_MAX = 499


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
    """Create the shared 'bot' group if it doesn't already exist."""
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
    """Provision a new isolated agent namespace on macOS."""
    checkroot()
    _check_platform()
    bot = args.bot

    if not BOTROOT.exists():
        print(f"Error: {BOTROOT} is not initialized. Run 'botadm init' first.", file=sys.stderr)
        sys.exit(1)

    botdir = BOTROOT / bot
    bothome = botdir / "home"
    botwork = botdir / "work"

    if botdir.exists():
        print(f"Error: Agent '{bot}' already exists.", file=sys.stderr)
        sys.exit(1)

    print(f"Provisioning isolated sandbox for agent: {bot}")

    group_created = False
    user_created = False
    dirs_created = False

    try:
        gid = _free_id("Groups", "PrimaryGroupID")
        _dscl("-create", f"/Groups/{bot}")
        _dscl("-create", f"/Groups/{bot}", "PrimaryGroupID", str(gid))
        _dscl("-create", f"/Groups/{bot}", "Password", "*")
        group_created = True
        print(f"-> Group '{bot}' created (GID {gid}).")

        uid = _free_id("Users", "UniqueID")
        _dscl("-create", f"/Users/{bot}")
        _dscl("-create", f"/Users/{bot}", "UniqueID", str(uid))
        _dscl("-create", f"/Users/{bot}", "PrimaryGroupID", str(gid))
        _dscl("-create", f"/Users/{bot}", "NFSHomeDirectory", str(bothome))
        _dscl("-create", f"/Users/{bot}", "UserShell", "/usr/bin/false")
        _dscl("-create", f"/Users/{bot}", "RealName", f"Agent: {bot}")
        _dscl("-create", f"/Users/{bot}", "Password", "*")
        user_created = True
        print(f"-> User '{bot}' created (UID {uid}).")

        bothome.mkdir(parents=True, exist_ok=True)
        botwork.mkdir(parents=True, exist_ok=True)
        dirs_created = True

        import pwd
        import grp
        try:
            botuid = pwd.getpwnam(bot).pw_uid
            botgid = grp.getgrnam(bot).gr_gid
            realuser = getuser()
            realuid = pwd.getpwnam(realuser).pw_uid
        except KeyError as err:
            raise RuntimeError(f"System lookup failure: {err}")

        os.chown(str(bothome), botuid, botgid)
        os.chown(str(botwork), realuid, botgid)
        os.chown(str(botdir), realuid, botgid)

        bothome.chmod(0o700)
        botwork.chmod(0o2770)
        botdir.chmod(0o750)

        _laia_env = botdir / "env"
        _laia_env.write_text(os.environ.get("PATH", ""))
        _laia_env.chmod(0o640)

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
    print(f"-> Home: {bothome}")
    print(f"-> Work: {botwork}")
    if sudoers_installed:
        print("-> Sudoers: automatically configured")
    else:
        realuser = getuser()
        print(f"\nTo enable execution, add this sudoers rule:")
        print(f"  {realuser} ALL=({bot}) NOPASSWD: ALL")


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


def cmddestroy(args):
    """Completely remove an agent on macOS: user, group, directory, sudoers."""
    checkroot()
    _check_platform()
    bot = args.bot

    print(f"Destroying agent: {bot}")

    if not args.no_sudoers:
        _remove_sudoers(bot)

    if _dscl_quiet("-delete", f"/Users/{bot}"):
        print(f"-> System user '{bot}' removed.")
    else:
        # Check if the user exists at all
        result = subprocess.run(
            ["dscl", ".", "-read", f"/Users/{bot}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"-> System user '{bot}' did not exist.")

    if _dscl_quiet("-delete", f"/Groups/{bot}"):
        print(f"-> System group '{bot}' removed.")
    else:
        result = subprocess.run(
            ["dscl", ".", "-read", f"/Groups/{bot}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"-> System group '{bot}' did not exist.")

    botdir = BOTROOT / bot
    if botdir.exists():
        shutil.rmtree(botdir)
        print(f"-> Directory {botdir} removed.")
    else:
        print(f"-> Directory {botdir} did not exist.")

    print(f"Agent '{bot}' destroyed.")


def cmdshare(args):
    """Share a directory with all bots via the shared 'bot' group."""
    checkroot()
    _check_platform()

    import grp
    try:
        grp.getgrnam("bot")
    except KeyError:
        print(
            "Error: Shared group 'bot' does not exist. Run 'init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    path = Path(args.path).resolve()

    if not path.is_dir():
        print(f"Error: '{path}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    # Refuse system paths
    system_roots = {"/etc", "/usr", "/var", "/System", "/bin", "/sbin",
                    "/opt", "/Library", "/Network", "/home"}
    if str(path) in system_roots:
        print(f"Error: Refusing to share system root '{path}'.", file=sys.stderr)
        sys.exit(1)

    for parent in path.parents:
        if str(parent) in system_roots:
            print(f"Error: Refusing to share '{path}' under system path '{parent}'.",
                  file=sys.stderr)
            sys.exit(1)

    if str(path) == str(BOTROOT):
        print(f"Error: Refusing to share bot root '{BOTROOT}'. "
              "Share individual agent workdirs instead.", file=sys.stderr)
        sys.exit(1)

    # Apply group and SGID
    if args.dry_run:
        print(f"[DRY RUN] Would set group=bot, SGID on '{path}'")
    else:
        subprocess.run(["chgrp", "bot", str(path)], check=True)
        subprocess.run(["chmod", "g+rwxs", str(path)], check=True)
        print(f"-> '{path}' is now shared with all bots (group=bot, SGID).")

    # Optionally fix existing files
    if args.recursive:
        if args.dry_run:
            print(f"[DRY RUN] Would recursively update files under '{path}'")
        else:
            for root, dirs, files in os.walk(str(path)):
                for name in files:
                    fp = os.path.join(root, name)
                    try:
                        subprocess.run(["chgrp", "bot", fp], check=True, capture_output=True)
                        subprocess.run(["chmod", "g+rw", fp], check=True, capture_output=True)
                    except subprocess.CalledProcessError:
                        pass
                for name in dirs:
                    dp = os.path.join(root, name)
                    try:
                        subprocess.run(["chgrp", "bot", dp], check=True, capture_output=True)
                        subprocess.run(["chmod", "g+rwxs", dp], check=True, capture_output=True)
                    except subprocess.CalledProcessError:
                        pass
            print("-> Existing files updated (recursive).")
    elif not args.dry_run:
        print("  (existing files not modified; use --recursive to update them)")

    if args.dry_run:
        return

    # Warn about sensitive files
    sensitive = {".git", ".env", ".aws", ".ssh", ".config", ".gnupg"}
    found = []
    for entry in path.iterdir():
        if entry.name in sensitive:
            found.append(entry.name)
    if found:
        print(f"  Warning: sensitive entries found: {', '.join(found)}")
        print("  Agents in the 'bot' group will be able to read these.")

    # Check if human is in bot group
    realuser = getuser()
    try:
        members = subprocess.run(
            ["dscl", ".", "-read", "/Groups/bot", "GroupMembership"],
            capture_output=True, text=True,
        )
        if realuser not in members.stdout:
            print(f"\n  Note: add yourself to the 'bot' group to access agent files:")
            print(f"    sudo dseditgroup -o edit -a {realuser} -t user bot")
    except subprocess.CalledProcessError:
        pass


def cmdrun(args):
    """Wipe environment and execute a command inside the bot's sandbox."""
    bot = args.bot
    command = args.command

    botroot = BOTROOT / bot
    bothome = botroot / "home"
    botwork = botroot / "work"

    if not botroot.is_dir():
        print(f"Error: Sandbox '{botroot}' does not exist.", file=sys.stderr)
        sys.exit(1)

    _laia_env = botroot / "env"
    if _laia_env.exists():
        _stored_path = _laia_env.read_text().strip()
    else:
        _stored_path = "/usr/local/bin:/usr/bin:/bin"

    envargs = [
        f"HOME={bothome}",
        f"USER={bot}",
        f"LOGNAME={bot}",
        f"PATH={_stored_path}",
        "TERM=xterm-256color",
        f"PWD={botwork}",
        "PS1=\\[\\033[1;32m\\]\\u@bot\\[\\033[0m\\]:\\[\\033[1;34m\\]\\w\\[\\033[0m\\]\\$ ",
    ]

    sudocmd = [
        "sudo", "-u", bot,
        "env", "-i", *envargs,
        "bash", "-c", f"cd '{botwork}' && umask 007 && exec \"$@\"",
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
    bothome = botroot / "home"
    botwork = botroot / "work"

    if not botroot.is_dir():
        print(f"Error: Sandbox '{botroot}' does not exist.", file=sys.stderr)
        sys.exit(1)

    _laia_env = botroot / "env"
    if _laia_env.exists():
        _stored_path = _laia_env.read_text().strip()
    else:
        _stored_path = "/usr/local/bin:/usr/bin:/bin"

    envargs = [
        f"HOME={bothome}",
        f"USER={bot}",
        f"LOGNAME={bot}",
        f"PATH={_stored_path}",
        "TERM=xterm-256color",
        f"PWD={botwork}",
        "PS1=\\[\\033[1;32m\\]\\u@bot\\[\\033[0m\\]:\\[\\033[1;34m\\]\\w\\[\\033[0m\\]\\$ ",
    ]

    sudocmd = [
        "sudo", "-u", bot,
        "env", "-i", *envargs,
        shell, "-c", f"cd '{botwork}' && exec {shell} -i",
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

    parser_share = subparsers.add_parser(
        "share", help="Share a directory with all bots via the shared 'bot' group."
    )
    parser_share.add_argument("path", help="Directory path to share.")
    parser_share.add_argument(
        "--recursive", "-r", action="store_true",
        help="Recursively update existing files and directories.",
    )
    parser_share.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Print what would be done without making changes.",
    )

    parser_run = subparsers.add_parser("run", help="Run a command securely inside a bot's sandbox.")
    parser_run.add_argument("bot", help="Name of the bot container to execute in.")
    parser_run.add_argument("command", nargs=argparse.REMAINDER, help="Command and arguments to run.")

    parser_shell = subparsers.add_parser(
        "shell", help="Launch an interactive shell inside a bot's sandbox."
    )
    parser_shell.add_argument("bot", help="Name of the bot container to enter.")
    parser_shell.add_argument(
        "--shell", "-s", default="bash",
        help="Shell to launch (default: bash).",
    )

    args = parser.parse_args()

    commands = {
        "init": cmdinit,
        "create": cmdcreate,
        "disable": cmddisable,
        "destroy": cmddestroy,
        "share": cmdshare,
        "run": cmdrun,
        "shell": cmdshell,
    }

    commands[args.action](args)


if __name__ == "__main__":
    main()
