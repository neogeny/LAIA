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
    """Provision a new isolated agent namespace: user, group, dirs, and sudoers.
    Uses a single-directory layout and prompts for confirmation.
    """
    checkroot()
    _check_platform()
    bot = args.bot

    if not BOTROOT.exists():
        print(f"Error: {BOTROOT} is not initialized. Run 'botadm init' first.", file=sys.stderr)
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
    print("  User shell: /bin/bash")
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
        subprocess.run(["groupadd", bot], check=True, capture_output=True)
        group_created = True

        subprocess.run(
            ["useradd", "--system", "--gid", bot,
             "--home-dir", str(botdir), "--shell", "/bin/bash", bot],
            check=True, capture_output=True,
        )
        user_created = True

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

        # Create standard bash config files owned by the bot user
        profile = botdir / ".profile"
        profile.write_text("\n".join(f'export {k}="{v}"' for k, v in env_dict.items()) + "\n")
        profile.chmod(0o644)
        os.chown(str(profile), botuid, botgid)

        bashrc = botdir / ".bashrc"
        bashrc_contents = '[[ -f /etc/bash.bashrc ]] && source /etc/bash.bashrc\n'
        bashrc_contents += "PS1='\\[\\e[1;32m\\]\\u@{bot}\\[\\e[0m\\]:\\[\\e[1;34m\\]\\w\\[\\e[0m\\]\\$ '\n"
        bashrc.write_text(bashrc_contents)
        bashrc.chmod(0o644)
        os.chown(str(bashrc), botuid, botgid)

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
    print(f"-> Namespace: {botdir}")
    if sudoers_installed:
        print("-> Sudoers: automatically configured")
    else:
        realuser = getuser()
        print("\nTo enable execution, add this sudoers rule:")
        print(f"  {realuser} ALL=({bot}) NOPASSWD: ALL")


def cmdupdate(args):
    """Recreate configuration files for an existing bot namespace (Linux)."""
    checkroot()
    bot = args.bot

    botdir = BOTROOT / bot
    if not botdir.exists():
        print(f"Error: Namespace '{botdir}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Ensure system user exists
    import pwd
    try:
        pwd.getpwnam(bot)
    except KeyError:
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

    # Recreate bash configuration files owned by the bot user
    profile = botdir / ".profile"
    profile.write_text("\n".join(f'export {k}="{v}"' for k, v in env_dict.items()) + "\n")
    profile.chmod(0o644)
    os.chown(str(profile), botuid, botgid)

    bashrc = botdir / ".bashrc"
    bashrc.write_text('[[ -f /etc/bash.bashrc ]] && source /etc/bash.bashrc\n')
    bashrc.chmod(0o644)
    os.chown(str(bashrc), botuid, botgid)

    # Attempt to set the user's login shell to bash (best-effort)
    try:
        subprocess.run(["usermod", "-s", "/bin/bash", bot], check=True, capture_output=True)
        print("-> User shell set to /bin/bash.")
    except subprocess.CalledProcessError:
        print("Warning: could not set user shell to /bin/bash (non-fatal).", file=sys.stderr)

    print(f"-> Configuration for '{bot}' updated.")


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
        print("-> Filesystem permissions stripped (0000 quarantine).")
    else:
        print(f"Error: Namespace for '{bot}' not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Agent '{bot}' quarantined.")


def cmdenable(args):
    """Re-enable an agent previously disabled (Linux)."""
    checkroot()
    bot = args.bot
    print(f"Enabling agent: {bot}")

    # Ensure system user exists
    import pwd
    try:
        pwd.getpwnam(bot)
    except KeyError:
        print(f"Error: System user '{bot}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        subprocess.run(["usermod", "-U", "-s", "/bin/bash", bot], check=True, capture_output=True)
        print(f"-> Account '{bot}' unlocked, shell set to /bin/bash.")
    except subprocess.CalledProcessError as err:
        print(f"Warning: could not modify user: {err.stderr.decode().strip()}", file=sys.stderr)

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
    """Completely remove an agent: user, group, directory, and sudoers rule."""
    checkroot()
    _check_platform()
    bot = args.bot

    print(f"Destroying agent: {bot}")
    print("This is permanent and will delete the system user, group, and namespace directory.")
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


def cmdshare(args):
    """Share paths with a named group (Linux).

    Same rules as macOS: defaults to group 'bot' and current directory when --paths omitted.
    Only supplementary group membership and group ownership are changed.
    """
    checkroot()

    import grp
    import pwd

    group = args.group or "bot"
    if args.paths:
        targets = [Path(p).resolve() for p in args.paths.split(",") if p.strip()]
    else:
        targets = [Path.cwd()]

    bots = []
    if args.bots:
        bots = [b.strip() for b in args.bots.split(",") if b.strip()]

    # Check existence
    try:
        grp.getgrnam(group)
        group_exists = True
    except KeyError:
        group_exists = False

    if args.create:
        if group_exists:
            print(f"Error: Group '{group}' already exists; --create must fail if group exists.", file=sys.stderr)
            sys.exit(1)
        try:
            subprocess.run(["groupadd", group], check=True, capture_output=True)
            print(f"-> Group '{group}' created.")
            group_exists = True
        except subprocess.CalledProcessError as err:
            print(f"Error: could not create group '{group}': {err}", file=sys.stderr)
            sys.exit(1)

    if not group_exists and not args.dry_run:
        print(f"Error: Group '{group}' does not exist. Use --create to create it.", file=sys.stderr)
        sys.exit(1)

    # Validate targets
    system_roots = {"/etc", "/usr", "/var", "/bin", "/sbin",
                    "/opt", "/proc", "/sys", "/dev", "/run", "/root"}
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

    # Confirmation
    if not args.force and not args.dry_run:
        print(f"About to operate on group='{group}' for paths: {', '.join(map(str, targets))}")
        if bots:
            print(f"Bots: {', '.join(bots)}")
        ok = input("Proceed? [y/N]: ").strip().lower()
        if ok != "y":
            print("Aborting.")
            sys.exit(1)

    # Add/remove membership (Linux usermod / gpasswd)
    if bots and args.add:
        for bot in bots:
            if args.dry_run:
                print(f"[DRY RUN] Would add user '{bot}' to group '{group}' (usermod -aG)")
            else:
                try:
                    subprocess.run(["usermod", "-aG", group, bot], check=True)
                    print(f"-> User '{bot}' added to group '{group}'.")
                except subprocess.CalledProcessError as err:
                    print(f"Warning: could not add '{bot}' to '{group}': {err}", file=sys.stderr)

    if bots and args.remove:
        for bot in bots:
            if args.dry_run:
                print(f"[DRY RUN] Would remove user '{bot}' from group '{group}' (gpasswd -d)")
            else:
                try:
                    subprocess.run(["gpasswd", "-d", bot, group], check=True)
                    print(f"-> User '{bot}' removed from group '{group}'.")
                except subprocess.CalledProcessError:
                    print(f"Note: user '{bot}' was not a member of '{group}'.")

    # Apply chgrp and mode to targets
    chmod_mode = args.mode or "g+rwxs"
    for path in targets:
        if args.dry_run:
            print(f"[DRY RUN] Would set group={group} and mode={chmod_mode} on '{path}'")
        else:
            subprocess.run(["chgrp", group, str(path)], check=True)
            subprocess.run(["chmod", chmod_mode, str(path)], check=True)
            print(f"-> '{path}' group set to '{group}' and mode applied.")

    # Optionally remove group entirely
    if args.remove_group:
        mygid = pwd.getpwuid(os.getuid()).pw_gid
        mygroup = grp.getgrgid(mygid).gr_name
        for path in targets:
            if args.dry_run:
                print(f"[DRY RUN] Would restore group of '{path}' to '{mygroup}'")
            else:
                subprocess.run(["chgrp", mygroup, str(path)], check=True)
                print(f"-> Restored group of '{path}' to '{mygroup}'.")
        # Delete the group
        if args.dry_run:
            print(f"[DRY RUN] Would delete group '{group}'")
        else:
            try:
                subprocess.run(["groupdel", group], check=True)
                print(f"-> Group '{group}' deleted.")
            except subprocess.CalledProcessError as err:
                print(f"Error: failed to delete group '{group}': {err}", file=sys.stderr)
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
        f"PWD={botwork}",
        f"PS1=\\[\\033[1;32m\\]\\u@{bot}\\[\\033[0m\\]:\\[\\033[1;34m\\]\\w\\[\\033[0m\\]\\$ ",
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
    """Launch an interactive shell inside the bot's sandbox (Linux)."""
    bot = args.bot
    shell = args.shell

    botroot = BOTROOT / bot
    bothome = botroot
    botwork = botroot

    if not botroot.is_dir():
        print(f"Error: Sandbox '{botroot}' does not exist.", file=sys.stderr)
        sys.exit(1)

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
        f"PWD={botwork}",
        f"PS1=\\[\\e[1;32m\\]\\u@{bot}\\[\\e[0m\\]:\\[\\e[1;34m\\]\\w\\[\\e[0m\\]\\$ ",
    ]

    sudocmd = [
        "sudo", "-u", bot,
        "env", "-i", *envargs,
        shell, "-c", f"cd '{botwork}' && exec {shell} -l -i",
    ]

    try:
        subprocess.run(sudocmd, check=True)
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

    parser_shell = subparsers.add_parser(
        "shell", help="Launch an interactive shell inside a bot's sandbox."
    )
    parser_shell.add_argument("bot", help="Name of the bot container to enter.")
    parser_shell.add_argument(
        "--shell", "-s", default="bash",
        help="Shell to launch (default: bash).",
    )

    parser_run = subparsers.add_parser("run", help="Run a command securely inside a bot's sandbox.")
    parser_run.add_argument("bot", help="Name of the bot container to execute in.")
    parser_run.add_argument("command", nargs=argparse.REMAINDER, help="Command and arguments to run.")

    args = parser.parse_args()

    commands = {
        "init": cmdinit,
        "create": cmdcreate,
        "update": cmdupdate,
        "enable": cmdenable,
        "disable": cmddisable,
        "destroy": cmddestroy,
        "share": cmdshare,
        "run": cmdrun,
    }

    commands[args.action](args)


if __name__ == "__main__":
    main()
