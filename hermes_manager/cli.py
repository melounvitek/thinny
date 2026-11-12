"""hermes-manager command line."""

from __future__ import annotations

import argparse
import os
import sys

from . import apply, checks, host, people, update
from .registry import Registry, RegistryError
from .settings import SettingsError, load
from .system import CommandError, lock


def parse_access(value: str) -> tuple[str, str]:
    method, _, url = value.partition("=")
    return method, url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-manager", description="Shared Hermes Agent server, one isolated account per person.")
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("install", help="install Hermes and everything around it, then apply")
    sub.add_parser("apply", help="bring the server in line with site.toml")
    add = sub.add_parser("add-person", help="add a person (or finish an interrupted add)")
    add.add_argument("name")
    add.add_argument("--access", help="lan | tailscale | url=<URL> | local (default: asked)")
    acc = sub.add_parser("access", help="change how a person opens their Webapp")
    acc.add_argument("name")
    acc.add_argument("access", help="lan | tailscale | url=<URL> | local")
    for name, text in (("login", "sign a person in to the model provider (device code)"),
                       ("password", "give a person a new Webapp password"),
                       ("remove-person", "disable a person: stop everything, lock the account, keep the data")):
        sub.add_parser(name, help=text).add_argument("name")
    sub.add_parser("list", help="people, ports, URLs, gateway state")
    sub.add_parser("check", help="isolation and health checks")
    upd = sub.add_parser("update", help="nightly Hermes update (run by hermes-manager-update.timer)")
    upd.add_argument("--check", action="store_true", help="only report what would happen")
    upd.add_argument("--now", action="store_true",
                     help="update now: ignore the minimum release age and auto_update = false")
    rst = sub.add_parser("restart", help="restart gateways one by one (all, or the named people)")
    rst.add_argument("names", nargs="*")
    sub.add_parser("firewall", help=argparse.SUPPRESS)  # run by systemd units
    a = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("hermes-manager must run as root", file=sys.stderr)
        return 2
    try:
        s = load()
        if a.command == "check":
            return checks.run(s, Registry.load())
        if a.command == "list":
            people.list_people(Registry.load())
            return 0
        if a.command == "firewall":  # no lock: runs while other commands wait for a user manager
            host.firewall(s)
            return 0
        with lock():
            reg = Registry.load()
            if a.command == "install":
                apply.install(s)
            elif a.command == "apply":
                apply.apply(s)
            elif a.command == "add-person":
                access, url = parse_access(a.access) if a.access else (ask_access(s, a.name), "")
                if access == "url" and not url:
                    url = input("Address people will open (https://...): ").strip()
                people.add(s, reg, a.name, access, url)
            elif a.command == "access":
                people.change_access(s, reg, a.name, *parse_access(a.access))
            elif a.command == "login":
                people.login(s, reg, a.name)
            elif a.command == "password":
                people.password(reg, a.name)
            elif a.command == "remove-person":
                people.remove(s, reg, a.name)
            elif a.command == "update":
                return update.run(s, reg, check=a.check, now=a.now)
            elif a.command == "restart":
                targets = [reg.get(n) for n in a.names] if a.names else reg.current()
                people.restart_all([p for p in targets if p.state == "active"], reg)
        return 0
    except (SettingsError, RegistryError, CommandError) as e:
        print(f"hermes-manager: {e}", file=sys.stderr)
        return 1


def ask_access(s, name: str) -> str:
    """The access question of add-person (default from site.toml when not on a terminal)."""
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
