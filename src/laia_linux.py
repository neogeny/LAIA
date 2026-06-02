#!/usr/bin/env python3
"""
Dynamic multi-agent sandbox controller.
Handles lifecycle management (init, create, disable, destroy) and secure execution.
"""

import sys
import os
import shutil
import subprocess
import argparse
from pathlib import Path

BOTROOT = Path("/var/bot")
SUDOERS_DIR = Path("/etc/sudoers.d")


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
    """Verify required system tools exist (Linux shadow-utils)."""
    missing = []
    for tool in ("groupadd", "useradd", "groupdel", "userdel", "visudo"):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        print(
            f"Error: Required tools not found: {', '.join(missing)}. "
            "LAIA requires Linux with shadow-utils installed.",
            file=sys.stderr,
        )
        sys.exit(1)


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
    result = subprocess.run(
        ["groupadd", "--force", "bot"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("-> Shared group 'bot' created.")
    else:
        # groupadd --force with existing group returns 0, so non-zero is a real error
        import grp
        try:
            grp.getgrnam("bot")
        except KeyError:
            print(f"Warning: could not create shared group 'bot': {result.stderr.strip()}", file=sys.stderr)


def _add_to_bot_group(bot):
    """Add an agent user to the shared 'bot' supplementary group."""
    try:
        subprocess.run(["usermod", "-aG", "bot", bot], check=True, capture_output=True)
        print(f"-> User '{bot}' added to shared group 'bot'.")
    except subprocess.CalledProcessError as err:
        print(f"Warning: could not add '{bot}' to 'bot' group: {err.stderr.decode().strip()}", file=sys.stderr)


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
    """Provision a new isolated agent namespace: user, group, dirs, and sudoers."""
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
        subprocess.run(["groupadd", bot], check=True, capture_output=True)
        group_created = True

        subprocess.run(
            ["useradd", "--system", "--gid", bot,
             "--home-dir", str(bothome), "--shell", "/usr/sbin/nologin", bot],
            check=True, capture_output=True,
        )
        user_created = True

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

    except Exception as err:
        print(f"Error: {err}", file=sys.stderr)
        if dirs_created:
            shutil.rmtree(botdir, ignore_errors=True)
        if user_created:
            subprocess.run(["userdel", "-r", bot], capture_output=True)
        if group_created:
            subprocess.run(["groupdel", bot], capture_output=True)
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
        print(f"-> Sudoers: automatically configured")
    else:
        realuser = getuser()
        print(f"\nTo enable execution, add this sudoers rule:")
        print(f"  {realuser} ALL=({bot}) NOPASSWD: ALL")


def cmddisable(args):
    """Quarantine an agent: lock account, strip filesystem permissions."""
    checkroot()
    bot = args.bot
    print(f"Disabling agent: {bot}")

    try:
        subprocess.run(
            ["usermod", "-L", "-s", "/usr/sbin/nologin", bot],
            check=True, capture_output=True,
        )
        print(f"-> Account '{bot}' locked, shell disabled.")
    except subprocess.CalledProcessError as err:
        print(
            f"Warning: could not modify user: {err.stderr.decode().strip()}",
            file=sys.stderr,
        )

    botdir = BOTROOT / bot
    if botdir.exists():
        for subdir in botdir.iterdir():
            subdir.chmod(0o000)
        botdir.chmod(0o000)
        print(f"-> Filesystem permissions stripped (0000 quarantine).")
    else:
        print(f"Error: Namespace for '{bot}' not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Agent '{bot}' quarantined.")


def cmddestroy(args):
    """Completely remove an agent: user, group, directory, and sudoers rule."""
    checkroot()
    _check_platform()
    bot = args.bot

    print(f"Destroying agent: {bot}")

    if not args.no_sudoers:
        _remove_sudoers(bot)

    try:
        subprocess.run(["userdel", "-r", bot], check=True, capture_output=True)
        print(f"-> System user '{bot}' removed.")
    except subprocess.CalledProcessError as err:
        msg = err.stderr.decode().strip()
        if "does not exist" in msg:
            print(f"-> System user '{bot}' did not exist.")
        else:
            print(f"Warning: could not remove user '{bot}': {msg}", file=sys.stderr)

    try:
        subprocess.run(["groupdel", bot], check=True, capture_output=True)
        print(f"-> System group '{bot}' removed.")
    except subprocess.CalledProcessError as err:
        msg = err.stderr.decode().strip()
        if "does not exist" in msg:
            print(f"-> System group '{bot}' did not exist.")
        else:
            print(f"Warning: could not remove group '{bot}': {msg}", file=sys.stderr)

    botdir = BOTROOT / bot
    if botdir.exists():
        shutil.rmtree(botdir)
        print(f"-> Directory {botdir} removed.")
    else:
        print(f"-> Directory {botdir} did not exist.")

    print(f"Agent '{bot}' destroyed.")


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

    envargs = [
        f"HOME={bothome}",
        f"USER={bot}",
        f"LOGNAME={bot}",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "TERM=xterm-256color",
        f"PWD={botwork}",
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


def main():
    parser = argparse.ArgumentParser(
        description="Unified local agent sandbox administration and runtime container tool."
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

    parser_run = subparsers.add_parser("run", help="Run a command securely inside a bot's sandbox.")
    parser_run.add_argument("bot", help="Name of the bot container to execute in.")
    parser_run.add_argument("command", nargs=argparse.REMAINDER, help="Command and arguments to run.")

    args = parser.parse_args()

    commands = {
        "init": cmdinit,
        "create": cmdcreate,
        "disable": cmddisable,
        "destroy": cmddestroy,
        "run": cmdrun,
    }

    commands[args.action](args)


if __name__ == "__main__":
    main()
