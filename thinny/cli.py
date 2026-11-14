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

from . import apply, checks, host, instances, move, settings, system, uninstall, update
from .registry import Registry, RegistryError
from .settings import SettingsError, load
from .system import CommandError, lock, write_file


def parse_access(value: str) -> tuple[str, str]:
    method, _, url = value.partition("=")
    return method, url


def at_least_1(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of at least 1")
    return int(value)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="thinny", description="Shared Hermes Agent server, one isolated container per instance.")
    sub = ap.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("init", help="ask for the main settings and write site.toml (on a new server)")
    sub.add_parser("setup", help="set up the server, then apply site.toml")
    sub.add_parser("status", help="quick look: release, last update, containers, gateways")
    sub.add_parser("doctor", help="every isolation and health check, with fixes")
    upd = sub.add_parser("update", help="update thinny, then every instance's Hermes, now")
    upd.add_argument("--dry-run", action="store_true", help="only report what would happen")
    upd.add_argument("--scheduled", action="store_true",
                     help="as the nightly timer: respect auto_update, wait for min_release_age_hours")
    cfg = sub.add_parser("config", help="the site settings, /etc/thinny/site.toml")
    cfg = cfg.add_subparsers(dest="action", required=True, metavar="action")
    cfg.add_parser("edit", help="edit site.toml in $VISUAL or $EDITOR, check it, then apply it")
    cfg.add_parser("apply", help="bring the server in line with site.toml after editing it")
    inst = sub.add_parser("instance", help="instances, one container each")
    inst = inst.add_subparsers(dest="action", required=True, metavar="action")
    inst.add_parser("list", help="instances, ports, URLs, gateway state")
    add = inst.add_parser("add", help="add an instance (or finish an interrupted add)",
                          description="Limits not given follow [container] in site.toml.")
    add.add_argument("name")
    add.add_argument("--access", help="lan | tailscale | url=<URL> | local (default: asked)")
    lim = inst.add_parser("limits", help="change an instance's CPU, memory or disk limits",
                          description="Limits not given stay as they are.")
    lim.add_argument("name")
    for parser in (add, lim):
        parser.add_argument("--cpu", type=at_least_1, help="CPUs")
        parser.add_argument("--memory-mb", type=at_least_1, help="memory in MiB")
        parser.add_argument("--disk-gb", type=at_least_1, help="disk in GiB")
    acc = inst.add_parser("access", help="change how users open an instance's Webapp")
    acc.add_argument("name")
    acc.add_argument("access", help="lan | tailscale | url=<URL> | local")
    lgn = inst.add_parser("login", help="sign an instance in to the model provider (device code)")
    lgn.add_argument("name")
    lgn.add_argument("--claude", action="store_true", help="sign it in to a Claude subscription instead")
    for name, text in (("reset-password", "give an instance a new Webapp password"),
                       ("disable", "stop an instance's container for good, keep its data")):
        inst.add_parser(name, help=text).add_argument("name")
    rst = inst.add_parser("restart", help="restart gateways one by one (all, or the named instances)")
    rst.add_argument("names", nargs="*")
    mov = inst.add_parser("move", help="move instances to another thinny server over SSH (experimental)")
    mov.add_argument("names", nargs="*")
    mov.add_argument("--all", action="store_true", help="every active instance")
    mov.add_argument("--to", required=True, metavar="[USER@]HOST")
    rcv = inst.add_parser("receive")  # without help, so not listed: `move` runs it on the other server
    rcv.add_argument("name")
    rcv.add_argument("--access", required=True)
    rcv.add_argument("--url", default="")
    rcv.add_argument("--release", nargs=2, required=True, metavar=("TAG", "COMMIT"))
    rcv.add_argument("--built-ins", required=True)
    rcv.add_argument("--check", action="store_true")
    sub.add_parser("uninstall", help="remove thinny and every instance, with all their data")
    a = ap.parse_args(argv)
    command = " ".join(filter(None, (a.command, getattr(a, "action", None))))

    if os.geteuid() != 0:
        print("thinny must run as root or with sudo", file=sys.stderr)
        return 2
    try:
        if command == "init":
            init_config()
            return 0
        if command == "uninstall":
            with lock(update.SELF_UPDATE_LOCK), lock():
                uninstall.run()
            return 0
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
            instances.list_instances(Registry.load())
            return 0
        # Before the lock: the install.sh of a new thinny runs `thinny setup`.
        if command == "update" and not a.dry_run and (s.auto_update or not a.scheduled) and update.update_thinny():
            # on with the new code
            os.execv(host.THINNY, [host.THINNY, "update", *(["--scheduled"] if a.scheduled else [])])
        with lock():
            reg = Registry.load()
            if command == "setup":
                apply.setup(s)
            elif command == "config apply":
                apply.apply(s)
            elif command == "update":
                return update.run(s, reg, dry_run=a.dry_run, now=not a.scheduled)
            elif command == "instance add":
                access, url = parse_access(a.access) if a.access else (
                    ask_access(f"How will users open {a.name}'s Webapp?", s.lan_address, s.access_default), "")
                if access == "url" and not url:
                    url = input("Address users will open (https://...): ").strip()
                instances.add(s, reg, a.name, access, url, cpu=a.cpu, memory_mb=a.memory_mb, disk_gb=a.disk_gb)
            elif command == "instance limits":
                instances.limits(reg, a.name, a.cpu, a.memory_mb, a.disk_gb)
            elif command == "instance access":
                instances.change_access(s, reg, a.name, *parse_access(a.access))
            elif command == "instance login":
                instances.login(s, reg, a.name, claude=a.claude)
            elif command == "instance reset-password":
                instances.password(s, reg, a.name)
            elif command == "instance disable":
                instances.disable(s, reg, a.name)
            elif command == "instance restart":
                targets = [reg.get(n) for n in a.names] if a.names else reg.current()
                instances.restart_all(s, [t for t in targets if t.state == "active"], reg)
            elif command == "instance move":
                if bool(a.names) == a.all:
                    raise CommandError("name the instances to move, or give --all")
                move.move(s, reg, a.names, a.to)
            elif command == "instance receive":
                move.receive(s, reg, a.name, a.access, a.url, tuple(a.release), a.built_ins,
                             None if a.check else sys.stdin.buffer)
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
                try:
                    answer = input("Edit again or discard the changes? [E/d] ")
                except EOFError:  # nobody to answer
                    answer = "d"
                if answer.strip().lower().startswith("d"):
                    print("Discarded; site.toml is unchanged.")
                    return False
                continue
            write_file(path, text)
            return True
    finally:
        copy.unlink(missing_ok=True)


def init_config() -> None:
    """Ask for what a new server needs and write it into the commented site.example.toml."""
    path = settings.SITE_TOML
    if path.exists():
        raise CommandError(f"{path} exists; change it with: thinny config edit")
    if not sys.stdin.isatty():
        raise CommandError(f"no terminal to ask on; copy {host.LIB / 'site.example.toml'} to {path} and edit it")
    interface, address = system.default_route()
    print(f"thinny needs a few settings for {path}. Enter takes the [suggestion].\n"
          "Everything else keeps its default; change any of it later with: thinny config edit\n")
    try:
        kind = choose("Model provider", [("openai", "OpenAI      a ChatGPT subscription"),
                                         ("anthropic", "Anthropic   a Claude Pro or Max subscription"),
                                         ("other", "Other       another Hermes provider")], "openai")
        if kind == "openai":
            provider, model = "openai-codex", ask("Model id", "gpt-5.5")
        elif kind == "anthropic":
            provider, model = instances.CLAUDE_PROVIDER, ask("Model id", "claude-opus-5-5")
        else:
            provider, model = ask("Hermes provider id"), ask("Model id")
        language = ask("Webapp language (en, cs, de, ...)", "en").lower()  # Hermes's codes, e.g. pt-br
        interface = ask("Internet interface, containers go out through it", interface or "eth0")
        print()
        access = ask_access("How will users open the Webapps? (default for new instances)", address, "lan")
        lan_address = ask("Server address office PCs open", address or None) if access == "lan" else ""
        port = ask("Port of one tunnel to every instance (0: none)", "8080") if access == "url" else "0"
        if not port.isdigit():
            raise CommandError(f"{port!r} is not a port; nothing written")
        # A language Hermes lacks comes as a catalog plugin.
        pack = f"hermes-lang-{language}"
        found = system.http_status(host.CATALOG_URL.format(pack), timeout=20) if language != "en" else ""
        pack = pack if found == "200" else ""
        text = settings.fill((host.LIB / "site.example.toml").read_text(encoding="utf-8"), {
            ("policy", "model_provider"): provider, ("policy", "model"): model, ("policy", "language"): language,
            ("policy", "language_pack"): pack, ("network", "interface"): interface, ("access", "default"): access,
            ("access", "lan_address"): lan_address, ("access", "tunnel_port"): int(port)})
        settings.parse(text, source=str(path))
        where = {"lan": f"office network, https://{lan_address}:<port>/", "tailscale": "Tailscale",
                 "url": "own addresses" + (f", one tunnel at http://127.0.0.1:{port}" if int(port) else ""),
                 "local": "local only, through an SSH tunnel"}[access]
        pack_note = (f", with the language pack {pack}" if pack else "" if found in ("", "404") else
                     " (could not check Hermes's plugin catalog for a language pack; set policy.language_pack if needed)")
        print(f"\n  model      {provider} / {model}\n  language   {language}{pack_note}\n"
              f"  interface  {interface}\n  access     {where}\n")
        if input(f"Write {path}? [Y/n] ").strip().lower().startswith("n"):
            raise CommandError("nothing written")
    except (EOFError, KeyboardInterrupt):
        print()
        raise CommandError("nothing written") from None
    write_file(path, text)
    print(f"Wrote {path}.")


def ask(question: str, default: str | None = None) -> str:
    """The answer, or `default` on Enter; without a default, asks until answered."""
    while True:
        answer = input(f"{question}{f' [{default}]' if default else ''}: ").strip() or default
        if answer is not None:
            return answer


def ask_access(question: str, lan_address: str, default: str) -> str:
    """The access question (`default` when not on a terminal)."""
    if not sys.stdin.isatty():
        return default
    tailscale = host.tailscale_status() is not None
    options = [("lan", f"Office network (LAN)  https://{lan_address or '<lan_address>'}:<port>/  built-in HTTPS proxy"),
               ("tailscale", "Tailscale             https://<machine>.<tailnet>.ts.net:<port>/") if tailscale else None,
               ("url", "Own address           you enter the URL (Cloudflare, your proxy, ...)"),
               ("local", "Local only            no network access; open via an SSH tunnel")]
    return choose(question, [o for o in options if o], default)


def choose(question: str, options: list[tuple[str, str]], default: str) -> str:
    """The value of the option picked by its number, or `default` on Enter; asks until it gets one."""
    print(question)
    for i, (_, text) in enumerate(options, 1):
        print(f"  {i}) {text}")
    default = next((i for i, (value, _) in enumerate(options, 1) if value == default), 1)
    while True:
        answer = input(f"Choice [{default}]: ").strip() or str(default)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
