"""thinny command line."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import apply, checks, host, people, settings, update
from .registry import Registry, RegistryError
from .settings import SettingsError, load
from .system import CommandError, lock, write_file


def parse_access(value: str) -> tuple[str, str]:
    method, _, url = value.partition("=")
    return method, url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="thinny", description="Shared Hermes Agent server, one isolated container per person.")
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("setup", help="set up the server, then apply site.toml")
    sub.add_parser("status", help="quick look: release, last update, containers, gateways")
    sub.add_parser("doctor", help="every isolation and health check, with fixes")
    upd = sub.add_parser("update", help="update everyone's Hermes now")
    upd.add_argument("--dry-run", action="store_true", help="only report what would happen")
    upd.add_argument("--scheduled", action="store_true",
                     help="as the nightly timer: wait for min_release_age_hours, respect auto_update")
    cfg = sub.add_parser("config", help="the site settings, /etc/thinny/site.toml")
    cfg = cfg.add_subparsers(dest="action", required=True, metavar="action")
    cfg.add_parser("edit", help="edit site.toml in $VISUAL or $EDITOR, check it, then apply it")
    cfg.add_parser("apply", help="bring the server in line with site.toml after editing it")
    inst = sub.add_parser("instance", help="people's instances, one container each")
    inst = inst.add_subparsers(dest="action", required=True, metavar="action")
    inst.add_parser("list", help="people, ports, URLs, gateway state")
    add = inst.add_parser("add", help="add a person (or finish an interrupted add)")
    add.add_argument("name")
    add.add_argument("--access", help="lan | tailscale | url=<URL> | local (default: asked)")
    acc = inst.add_parser("access", help="change how a person opens their Webapp")
    acc.add_argument("name")
    acc.add_argument("access", help="lan | tailscale | url=<URL> | local")
    for name, text in (("login", "sign a person in to the model provider (device code)"),
                       ("reset-password", "give a person a new Webapp password"),
                       ("disable", "stop a person's container for good, keep their data")):
        inst.add_parser(name, help=text).add_argument("name")
    rst = inst.add_parser("restart", help="restart gateways one by one (all, or the named people)")
    rst.add_argument("names", nargs="*")
    a = ap.parse_args(argv)
    command = " ".join(filter(None, (a.command, getattr(a, "action", None))))

    if os.geteuid() != 0:
        print("thinny must run as root", file=sys.stderr)
        return 2
    try:
        if command == "config edit":
            with lock():  # before editing: a running update says so now, not after the edit
                if edit_config():
                    apply.apply(load())
            return 0
        s = load()
        if command == "doctor":
            return checks.run(s, Registry.load())
        if command == "status":
            return checks.status(s, Registry.load())
        if command == "instance list":
            people.list_people(Registry.load())
            return 0
        with lock():
            reg = Registry.load()
            if command == "setup":
                apply.setup(s)
            elif command == "config apply":
                apply.apply(s)
            elif command == "update":
                return update.run(s, reg, dry_run=a.dry_run, now=not a.scheduled)
            elif command == "instance add":
                access, url = parse_access(a.access) if a.access else (ask_access(s, a.name), "")
                if access == "url" and not url:
                    url = input("Address people will open (https://...): ").strip()
                people.add(s, reg, a.name, access, url)
            elif command == "instance access":
                people.change_access(s, reg, a.name, *parse_access(a.access))
            elif command == "instance login":
                people.login(s, reg, a.name)
            elif command == "instance reset-password":
                people.password(reg, a.name)
            elif command == "instance disable":
                people.disable(s, reg, a.name)
            elif command == "instance restart":
                targets = [reg.get(n) for n in a.names] if a.names else reg.current()
                people.restart_all([p for p in targets if p.state == "active"], reg)
        return 0
    except (SettingsError, RegistryError, CommandError) as e:
        print(f"thinny: {e}", file=sys.stderr)
        return 1


def edit_config() -> bool:
    """Edit a copy of site.toml, like visudo: only a valid change replaces it. True when it did."""
    path = settings.SITE_TOML
    # sudo drops the admin's $EDITOR; `editor` is Debian's and Ubuntu's default (nano).
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("editor")
    if not editor:
        raise CommandError(f"no editor found (set EDITOR), or edit {path}, then run: thinny config apply")
    original = path.read_text(encoding="utf-8")
    fd, name = tempfile.mkstemp(prefix="site-", suffix=".toml")
    copy = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(original)
        while True:
            try:
                code = subprocess.run([*shlex.split(editor), str(copy)]).returncode
            except OSError as e:
                raise CommandError(f"cannot start {editor}: {e.strerror}") from e
            if code != 0:
                raise CommandError(f"{editor} exited with {code}; site.toml is unchanged")
            text = copy.read_text(encoding="utf-8")
            if text == original:
                print("site.toml is unchanged.")
                return False
            try:
                settings.parse(text, source=str(path))
            except SettingsError as e:
                print(e)
                if input("Edit again or discard the changes? [E/d] ").strip().lower().startswith("d"):
                    print("Discarded; site.toml is unchanged.")
                    return False
                continue
            write_file(path, text)
            return True
    finally:
        copy.unlink(missing_ok=True)


def ask_access(s, name: str) -> str:
    """The access question of `instance add` (default from site.toml when not on a terminal)."""
    if not sys.stdin.isatty():
        return s.access_default
    tailscale = host.tailscale_status() is not None
    options = [("lan", f"Office network (LAN)  https://{s.lan_address or '<lan_address>'}:<port>/  built-in HTTPS proxy"),
               ("tailscale", "Tailscale             https://<machine>.<tailnet>.ts.net:<port>/") if tailscale else None,
               ("url", "Own address           you enter the URL (Cloudflare, your proxy, ...)"),
               ("local", "Local only            no network access; open via an SSH tunnel")]
    options = [o for o in options if o]
    print(f"How will {name} open their Webapp?")
    for i, (_, text) in enumerate(options, 1):
        print(f"  {i}) {text}")
    default = next((i for i, (m, _) in enumerate(options, 1) if m == s.access_default), 1)
    answer = input(f"Choice [{default}]: ").strip() or str(default)
    if not answer.isdigit() or not 1 <= int(answer) <= len(options):
        raise CommandError("no such choice")
    return options[int(answer) - 1][0]
