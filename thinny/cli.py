"""thinny command line."""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import apply, checks, host, instances, move, settings, system, telegram, trace, uninstall, update
from .registry import Registry, RegistryError, validate_name
from .settings import SettingsError, load
from .system import CommandError, lock, write_file

# The providers thinny offers by name: name -> (Hermes provider id, suggested model, text). Users set up
# others in Hermes itself.
PROVIDERS = {"openai": ("openai-codex", "gpt-5.5", "OpenAI      a ChatGPT subscription"),
             "anthropic": (instances.CLAUDE_PROVIDER, "claude-opus-5-5",
                           "Anthropic   a Claude Pro or Max subscription")}


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
    sub.add_parser("init", help="ask for the main settings and write config.toml (on a new server)")
    sub.add_parser("setup", help="set up the server, then apply config.toml")
    sub.add_parser("status", help="quick look: release, last update, containers, gateways")
    sub.add_parser("doctor", help="every isolation and health check, with fixes")
    upd = sub.add_parser("update", help="update thinny, then every instance's Hermes, now")
    upd.add_argument("--dry-run", action="store_true", help="only report what would happen")
    upd.add_argument("--scheduled", action="store_true",
                     help="as the nightly timer: respect auto_update, wait for min_release_age_hours")
    cfg = sub.add_parser("config", help="the server settings, /etc/thinny/config.toml")
    cfg = cfg.add_subparsers(dest="action", required=True, metavar="action")
    cfg.add_parser("edit", help="edit config.toml (or a group's file) in $VISUAL or $EDITOR, check it, then apply it") \
        .add_argument("--group", help="the group's Hermes settings instead, /etc/thinny/groups/<group>.toml")
    cfg.add_parser("apply", help="bring the server in line with config.toml after editing it")
    inst = sub.add_parser("instance", help="instances, one container each")
    inst = inst.add_subparsers(dest="action", required=True, metavar="action")
    inst.add_parser("list", help="instances, ports, URLs, gateway state, CPU, RAM and disk use") \
        .add_argument("--group", help="its instances only")
    add = inst.add_parser("add", help="add an instance (or finish an interrupted add)",
                          description="Limits not given follow [container] in config.toml.")
    add.add_argument("name")
    add.add_argument("--access", help="lan | tailscale | url=<URL> | local (default: asked)")
    add.add_argument("--frontend", choices=settings.FRONTENDS, help="(default: asked)")
    add.add_argument("--group", help="whose Hermes settings it gets (default: none)")
    grp = inst.add_parser("group", help="groups: instances with the Hermes settings of a group's file")
    grp = grp.add_subparsers(dest="group_action", required=True, metavar="action")
    grp.add_parser("list", help="groups and their instances")
    gset = grp.add_parser("set", help="put an instance in a group, or in none (it loses the group's settings)")
    gset.add_argument("name")
    gset.add_argument("group", metavar="GROUP|none")
    lim = inst.add_parser("limits", help="change an instance's CPU, memory or disk limits",
                          description="Limits not given stay as they are.")
    lim.add_argument("name", nargs="?")
    for parser in (add, lim):
        parser.add_argument("--cpu", type=at_least_1, help="CPUs")
        parser.add_argument("--memory-mb", type=at_least_1, help="memory in MiB")
        parser.add_argument("--disk-gb", type=at_least_1, help="disk in GiB")
    acc = inst.add_parser("access", help="change how users open an instance's Webapp")
    acc.add_argument("name")
    acc.add_argument("access", help="lan | tailscale | url=<URL> | local")
    frn = inst.add_parser("frontend", help="switch an instance between the Webapp and WebUI")
    frn.add_argument("name")
    frn.add_argument("frontend", choices=settings.FRONTENDS)
    lgn = inst.add_parser("login", help="sign an instance in to a model provider (device code)")
    lgn.add_argument("name", nargs="?")
    lgn.add_argument("--provider", choices=PROVIDERS, help="(default: asked)")
    lck = inst.add_parser("lock", help="lock an instance to models of the provider it uses, whatever the server's lock")
    lck.add_argument("name", nargs="?", help="(none with --group)")
    lck.add_argument("models", nargs="*", help="model ids it allows, the first by default (default: the one it uses)")
    unl = inst.add_parser("unlock", help="let an instance's users choose their models, whatever the server's lock")
    unl.add_argument("name", nargs="?")
    inst.add_parser("telegram", help="give an instance a Telegram bot its user creates with a link (see README)") \
        .add_argument("name")
    trc = inst.add_parser("trace-sheet", help="send an instance's access trace to its users' Google Sheet too")
    trc.add_argument("name")
    trc.add_argument("url", metavar="URL|off", help="the URL of the sheet's script (see README, Access trace)")
    for name, text in (("reset-password", "give an instance a new Webapp password"),
                       ("disable", "stop an instance's container for good, keep its data"),
                       ("remove", "delete a disabled instance's container and data for good")):
        inst.add_parser(name, help=text).add_argument("name")
    rst = inst.add_parser("restart", help="restart gateways one by one (all, or the named instances)")
    rst.add_argument("names", nargs="*")
    mov = inst.add_parser("move", help="move instances to another thinny server over SSH (experimental)")
    mov.add_argument("names", nargs="*")
    mov.add_argument("--all", action="store_true", help="every active instance")
    for parser in (lim, lgn, lck, unl, rst, mov):
        parser.add_argument("--group", help="its active instances instead of named ones")
    mov.add_argument("--to", required=True, metavar="[USER@]HOST")
    rcv = inst.add_parser("receive")  # without help, so not listed: `move` runs it on the other server
    rcv.add_argument("name")
    rcv.add_argument("--access", required=True)
    rcv.add_argument("--url", default="")
    rcv.add_argument("--frontend", choices=settings.FRONTENDS, default="webapp")
    rcv.add_argument("--release", nargs=2, required=True, metavar=("TAG", "COMMIT"))
    rcv.add_argument("--built-ins", required=True)
    rcv.add_argument("--check", action="store_true")
    sub.add_parser("uninstall", help="remove thinny and every instance, with all their data")
    sub.add_parser("trace")  # without help, so not listed: thinny-trace.service runs it
    a = ap.parse_args(argv)
    command = " ".join(filter(None, (a.command, getattr(a, "action", None))))

    if os.geteuid() != 0:
        print("thinny must run as root or with sudo", file=sys.stderr)
        return 2
    try:
        if command == "init":
            init_config()
            return 0
        if command == "trace":  # without config.toml: a broken one must not stop the trace
            return trace.run()
        if command == "uninstall":
            with lock(update.SELF_UPDATE_LOCK), lock():
                uninstall.run()
            return 0
        if command == "config edit":
            if a.group:
                validate_name(a.group)
                if a.group == "none":
                    raise CommandError("'none' means no group; choose another name")
            with lock():  # before editing: a running update says so now, not after the edit
                if edit_config(a.group):
                    apply.apply(load())
            return 0
        s = load()
        if command == "doctor":
            return checks.run(s, Registry.load())
        if command == "status":
            return checks.status(s, Registry.load())
        if command == "instance list":
            instances.list_instances(Registry.load(), a.group)
            return 0
        if command == "instance group" and a.group_action == "list":
            instances.list_groups(s, Registry.load())
            return 0
        if command == "instance telegram":   # takes thinny's lock only once its user created the bot
            telegram.connect(s, a.name)
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
                if a.group:
                    check_group(s, a.group)
                access, url = parse_access(a.access) if a.access else (
                    ask_access(f"How will users open {a.name}'s Webapp?", s.lan_address, s.access_default), "")
                if access == "url" and not url:
                    url = input("Address users will open (https://...): ").strip()
                # A resumed add keeps the one it has.
                frontend = a.frontend or (None if a.name in reg.instances else
                                          ask_frontend(f"What will {a.name}'s users open?", s.frontend))
                moved = a.name in reg.instances and reg.get(a.name).moved
                asking = not moved and sys.stdin.isatty()  # a moved instance keeps its model and line
                if asking and not s.model_lock:  # else the lock decides
                    provider, model = ask_model(f"Which model provider will {a.name} use?", s.model_provider, s.model)
                    s = dataclasses.replace(s, model_provider=provider, model=model)
                phone = ask_phone(a.name) if asking else None
                instances.add(s, reg, a.name, access, url, frontend=frontend, phone=phone, group=a.group, cpu=a.cpu,
                              memory_mb=a.memory_mb, disk_gb=a.disk_gb)
            elif command == "instance group":   # set
                if a.group != "none":
                    check_group(s, a.group)
                instances.set_group(s, reg, a.name, "" if a.group == "none" else a.group)
            elif command == "instance limits":
                for name in chosen(reg, [a.name] if a.name else [], a.group):
                    instances.limits(reg, name, a.cpu, a.memory_mb, a.disk_gb)
            elif command == "instance access":
                instances.change_access(s, reg, a.name, *parse_access(a.access))
            elif command == "instance frontend":
                instances.change_frontend(s, reg, a.name, a.frontend)
            elif command == "instance login":
                for name in chosen(reg, [a.name] if a.name else [], a.group):   # one after another
                    if a.provider:
                        provider = PROVIDERS[a.provider][0]
                    else:
                        provider, _ = instances.model_of(s, reg, name)
                        if not instances.settings_for(s, reg.get(name)).model_lock:
                            provider = ask_provider(f"Which provider should {name} sign in to?", provider)
                    instances.login(reg, name, provider)
            elif command == "instance lock":
                if a.group and a.name:   # with --group, each is a model
                    a.name, a.models = None, [a.name, *a.models]
                for name in chosen(reg, [a.name] if a.name else [], a.group):
                    instances.lock(s, reg, name, a.models)
            elif command == "instance unlock":
                for name in chosen(reg, [a.name] if a.name else [], a.group):
                    instances.unlock(s, reg, name)
            elif command == "instance trace-sheet":
                trace.connect_sheet(reg, a.name, a.url)
            elif command == "instance reset-password":
                instances.password(s, reg, a.name)
            elif command == "instance disable":
                instances.disable(s, reg, a.name)
            elif command == "instance remove":
                instances.remove(s, reg, a.name)
            elif command == "instance restart":
                targets = [reg.get(n) for n in chosen(reg, a.names, a.group)] if a.names or a.group else reg.current()
                instances.restart_all(s, [t for t in targets if t.state == "active"], reg)
            elif command == "instance move":
                if sum((bool(a.names), a.all, bool(a.group))) != 1:
                    raise CommandError("name the instances to move, or give --all or --group")
                move.move(s, reg, [] if a.all else chosen(reg, a.names, a.group), a.to)
            elif command == "instance receive":
                move.receive(s, reg, a.name, a.access, a.url, tuple(a.release), a.built_ins,
                             None if a.check else sys.stdin.buffer, frontend=a.frontend)
        return 0
    except (SettingsError, RegistryError, CommandError) as e:
        print(f"thinny: {e}", file=sys.stderr)
        return 1


def chosen(reg: Registry, names: list[str], group: str | None) -> list[str]:
    """The named instances, or the active instances in the group: one or the other."""
    if bool(names) == bool(group):
        raise CommandError("name the instances, or give --group")
    if names:
        return names
    members = [inst.name for inst in reg.current() if inst.group == group and inst.state == "active"]
    if not members:
        raise CommandError(f"no active instance is in the group {group}")
    return members


def check_group(s: settings.Settings, group: str) -> None:
    if group not in s.groups:
        raise CommandError(f"no group {group}: create it with thinny config edit --group {group}")


def edit_config(group: str | None = None) -> bool:
    """Edit a copy of config.toml, or of a group's file (a new one starts as the example), like visudo: only a
    valid change replaces it. True when it did."""
    path = settings.group_file(group) if group else settings.CONFIG_TOML
    # sudo drops the admin's $EDITOR; `editor` is Debian's and Ubuntu's default (nano).
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or shutil.which("editor")
    if not editor:
        raise CommandError(f"no editor found (set EDITOR), or edit {path}, then run: thinny config apply")
    original = (path if path.exists() or not group else host.LIB / "group.example.toml").read_text(encoding="utf-8")
    fd, name = tempfile.mkstemp(prefix="config-", suffix=".toml")
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
                raise CommandError(f"{editor} exited with {code}; {path.name} is unchanged")
            text = copy.read_text(encoding="utf-8")
            if text == original:
                print(f"{path.name} is unchanged.")
                return False
            try:
                settings.parse_group(text, str(path)) if group else settings.parse(text, source=str(path))
            except SettingsError as e:
                print(e)
                try:
                    answer = input("Edit again or discard the changes? [E/d] ")
                except EOFError:  # nobody to answer
                    answer = "d"
                if answer.strip().lower().startswith("d"):
                    print(f"Discarded; {path.name} is unchanged.")
                    return False
                continue
            write_file(path, text, 0o600)   # it may hold tokens: the Telegram manager bot's, MCP servers'
            return True
    finally:
        copy.unlink(missing_ok=True)


def init_config() -> None:
    """Ask for what a new server needs and write it into the commented config.example.toml."""
    path = settings.CONFIG_TOML
    if path.exists():
        raise CommandError(f"{path} exists; change it with: thinny config edit")
    if not sys.stdin.isatty():
        raise CommandError(f"no terminal to ask on; copy {host.LIB / 'config.example.toml'} to {path} and edit it")
    interface, address = system.default_route()
    print(f"thinny needs a few settings for {path}. Enter takes the [suggestion].\n"
          "Everything else keeps its default; change any of it later with: thinny config edit\n")
    try:
        provider, model = ask_model("Model provider", "openai-codex", "gpt-5.5")
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
        text = settings.fill((host.LIB / "config.example.toml").read_text(encoding="utf-8"), {
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
    write_file(path, text, 0o600)
    print(f"Wrote {path}.")


def ask(question: str, default: str | None = None) -> str:
    """The answer, or `default` on Enter; without a default, asks until answered."""
    while True:
        answer = input(f"{question}{f' [{default}]' if default else ''}: ").strip() or default
        if answer is not None:
            return answer


def ask_secret(question: str) -> str:
    """An answer typed without showing it, as typed; asks until answered."""
    while True:
        if answer := getpass.getpass(f"{question}: "):
            return answer


def ask_phone(name: str) -> dict[str, str] | None:
    """The phone line's settings for the instance's .env (see the hermes-3310 plugin), with Odorik's defaults;
    None without a line."""
    if not input(f"Connect a phone line (SIP) to {name}? [y/N] ").strip().lower().startswith("y"):
        return None
    server, user = ask("SIP server", "sip.odorik.cz"), ask("SIP user name")
    password = ask_secret("SIP password")
    while not re.fullmatch(r"[0-9]{4,32}", pin := ask_secret("PIN callers type before talking, 4 to 32 digits "
                                                          "(better 6 or more)")):
        print("The PIN must be 4 to 32 digits.")
    callers = ask("Numbers that may call, with the country code (e.g. 420777000111; several: comma-separated)")
    first = re.sub(r"\D", "", callers.split(",")[0]).removeprefix("00")
    return {"SIP_SERVER": server, "SIP_USER": user, "SIP_PASSWORD": password, "PHONE_PIN": pin,
            "PHONE_ALLOWED_CALLERS": callers,
            "PHONE_HOME_NUMBER": ask("Number Hermes calls you on, as your provider dials it", f"00{first}"),
            "VOICE_TOOLS_OPENAI_KEY": ask_secret("OpenAI API key, for speech recognition on calls")}


def ask_provider(question: str, default: str) -> str:
    """The Hermes provider id of OpenAI or Anthropic (`default` when not on a terminal)."""
    if not sys.stdin.isatty():
        return default
    return choose(question, [(p, text) for p, _, text in PROVIDERS.values()], default)


def ask_model(question: str, provider: str, model: str) -> tuple[str, str]:
    """A provider and model id; Enter keeps `provider` and `model`, or takes the suggestion for another provider."""
    picked = ask_provider(question, provider)
    suggested = model if picked == provider else next(m for p, m, _ in PROVIDERS.values() if p == picked)
    return picked, ask("Model id", suggested)


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


def ask_frontend(question: str, default: str) -> str:
    """The frontend question (`default` when not on a terminal)."""
    if not sys.stdin.isatty():
        return default
    return choose(question, [("webapp", "Webapp   Hermes's built-in"),
                             ("webui", "WebUI    hermes-webui (github.com/nesquena/hermes-webui)")], default)


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
