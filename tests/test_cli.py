import subprocess
import sys
from pathlib import Path

import pytest

from thinny import (apply, cli, dashboard, export, host, instances, mail, move, render, settings, system, telegram, trace,
                    update)
from thinny.registry import Instance, Registry

VALID = '[policy]\nmodel = "m1"\nallowed_models = ["m1"]\n\n[access]\nlan_address = "192.168.1.10"\n'


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A root run against config.toml in tmp_path; records the applies and updates it starts. thinny
    itself has nothing new, and no account or Incus container has an instance's name."""
    site = tmp_path / "config.toml"
    site.write_text(VALID)
    monkeypatch.setattr(settings, "CONFIG_TOML", site)
    monkeypatch.setattr(cli, "load", lambda: settings.load(site))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: Registry()))
    monkeypatch.setattr(system, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(instances, "uid_of", lambda name: None)
    monkeypatch.setattr(instances, "container_state", lambda name: None)
    calls = []
    monkeypatch.setattr(apply, "apply", lambda s: calls.append(("apply", s.model)))
    monkeypatch.setattr(update, "run", lambda s, reg, **kw: calls.append(("update", kw)) or 0)
    monkeypatch.setattr(update, "update_thinny", lambda: calls.append("thinny") or False)
    monkeypatch.setattr(dashboard, "renew_key", lambda: calls.append("Grafana's key"))
    return site, calls


def editor(monkeypatch, *texts, code=0):
    """$VISUAL saves `texts` in turn; returns the commands it was started with."""
    texts, started = list(texts), []
    monkeypatch.setenv("VISUAL", "code --wait")

    def run(cmd, **kw):
        started.append(cmd[:-1])
        Path(cmd[-1]).write_text(texts.pop(0))
        return subprocess.CompletedProcess(cmd, code)
    monkeypatch.setattr(cli.subprocess, "run", run)
    return started


@pytest.fixture
def new_server(server, monkeypatch):
    """A server without config.toml, on a terminal. It reaches the internet through enp1s0 from
    192.168.1.5, Hermes's catalog has a Czech and a Brazilian Portuguese language pack, and Tailscale
    is off."""
    site, _ = server
    site.unlink()
    monkeypatch.setattr(system, "default_route", lambda: ("enp1s0", "192.168.1.5"))
    monkeypatch.setattr(system, "http_status",
                        lambda url, timeout: "200" if url.endswith(("/hermes-lang-cs.yaml", "/hermes-lang-pt-br.yaml"))
                        else "404")
    monkeypatch.setattr(host, "tailscale_status", lambda: None)
    monkeypatch.setattr(cli.sys, "stdin", type("Terminal", (), {"isatty": lambda self: True})())
    return site


def answering(monkeypatch, *answers):
    """The terminal gives `answers` in turn ("" is Enter), then ends. Returns the prompts whose
    answers it did not show."""
    answers, hidden = list(answers), []

    def answer(prompt=""):
        if not answers:
            raise EOFError
        return answers.pop(0)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": hidden.append(prompt) or answer(prompt))
    return hidden


@pytest.mark.parametrize("typed, language", [("cs", "cs"), ("pt-BR", "pt-br")])
def test_init_writes_the_answers_into_the_commented_example(new_server, monkeypatch, capsys, typed, language):
    # language, interface, own addresses, tunnel port, write: no model, each instance starts on Hermes's default
    answering(monkeypatch, typed, "", "2", "", "")
    assert cli.main(["init"]) == 0
    s = settings.load(new_server)
    assert (s.model_provider, s.model, s.language, s.language_pack, s.network_interface) == \
        ("openai-codex", "", language, f"hermes-lang-{language}", "enp1s0")
    assert (s.access_default, s.lan_address, s.tunnel_port) == ("url", "", 8080)
    assert "# with model_lock: where new chats start" in new_server.read_text()
    assert new_server.stat().st_mode & 0o777 == 0o600   # it may hold the Telegram manager bot's token
    assert "one tunnel at http://127.0.0.1:8080" in capsys.readouterr().out


def test_init_offers_the_detected_lan_address(new_server, monkeypatch):
    # language (no pack in the catalog), interface, LAN (the default), its address, write
    answering(monkeypatch, "de", "eth1", "", "", "y")
    assert cli.main(["init"]) == 0
    s = settings.load(new_server)
    assert (s.language, s.language_pack, s.network_interface) == ("de", "", "eth1")
    assert (s.access_default, s.lan_address, s.tunnel_port) == ("lan", "192.168.1.5", 0)


@pytest.mark.parametrize("typed, pack", [("", "hermes-lang-cs"), ("-", "")])
def test_init_asks_for_the_language_pack_when_it_could_not_check_the_catalog(new_server, monkeypatch, capsys,
                                                                              typed, pack):
    monkeypatch.setattr(system, "http_status", lambda url, timeout: "000")
    # language, its pack, interface, local only, write
    answering(monkeypatch, "cs", typed, "", "3", "")
    assert cli.main(["init"]) == 0
    assert settings.load(new_server).language_pack == pack
    assert "Could not check Hermes's plugin catalog for hermes-lang-cs (HTTP status 000)" in capsys.readouterr().out


@pytest.mark.parametrize("answers, message", [
    (("", "", "", "", "n"), "nothing written"),
    (("", "", "2", "9150"), "tunnel_port"),   # in the Webapp port range
    (("", "", "2", "lots"), "'lots' is not a port"),
    (("",), "nothing written"),                                # the terminal ends (Ctrl-D)
])
def test_init_writes_nothing_without_a_valid_confirmed_answer(new_server, monkeypatch, capsys, answers, message):
    answering(monkeypatch, *answers)
    assert cli.main(["init"]) == 1
    assert not new_server.exists()
    assert message in capsys.readouterr().err


def test_without_root_thinny_runs_itself_with_sudo_and_config_edit_keeps_the_editor(monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.sys, "argv", ["/usr/local/sbin/thinny"])
    monkeypatch.setenv("VISUAL", "code --wait")
    monkeypatch.delenv("EDITOR", raising=False)
    started = []

    def execvp(file, args):
        started.append(args)
        raise SystemExit(0)
    monkeypatch.setattr(cli.os, "execvp", execvp)
    for args in ("update", "--dry-run"), ("config", "edit"):
        with pytest.raises(SystemExit):
            cli.main(list(args))
    assert started == [["sudo", "/usr/local/sbin/thinny", "update", "--dry-run"],
                       ["sudo", "VISUAL=code --wait", "/usr/local/sbin/thinny", "config", "edit"]]


def test_init_leaves_an_existing_site_toml_alone(server, capsys):
    site, _ = server
    assert cli.main(["init"]) == 1
    assert site.read_text() == VALID
    assert "thinny config edit" in capsys.readouterr().err


# Grafana's key first, whatever auto_update says: install.sh's apt-get update would fail on an expired one.
@pytest.mark.parametrize("args, auto_update, kw, thinny", [
    ([], "false", {"dry_run": False, "manual": True, "now": True}, True),
    (["--dry-run"], "true", {"dry_run": True, "manual": True, "now": True}, False),
    (["--scheduled"], "true", {"dry_run": False, "manual": False, "now": False}, True),
    (["--scheduled"], "false", {"dry_run": False, "manual": False, "now": False}, False),
])
def test_a_manual_update_runs_now_and_only_the_timer_waits(server, args, auto_update, kw, thinny):
    site, calls = server
    site.write_text(VALID + f"\n[hermes]\nauto_update = {auto_update}\n")
    assert cli.main(["update", *args]) == 0
    assert calls == ["Grafana's key"] * (not kw["dry_run"]) + ["thinny"] * thinny + [("update", kw)]
    assert "thinny update --scheduled\n" in render.UPDATE_SERVICE


@pytest.mark.parametrize("args", [[], ["--scheduled"]])
def test_after_installing_a_new_thinny_the_update_goes_on_with_it(server, monkeypatch, args):
    _, calls = server
    monkeypatch.setattr(update, "update_thinny", lambda: True)

    def execv(path, argv):
        calls.append(("exec", path, argv))
        raise SystemExit(0)
    monkeypatch.setattr(cli.os, "execv", execv)
    with pytest.raises(SystemExit):
        cli.main(["update", *args])
    assert calls == ["Grafana's key", ("exec", "/usr/local/sbin/thinny", ["/usr/local/sbin/thinny", "update", *args])]


def test_a_failed_thinny_update_stops_the_update(server, monkeypatch, capsys):
    _, calls = server

    def broken():
        raise system.CommandError("git pull -> exit 1")
    monkeypatch.setattr(update, "update_thinny", broken)
    assert cli.main(["update"]) == 1
    assert calls == ["Grafana's key"]
    assert "git pull -> exit 1" in capsys.readouterr().err


def test_instance_add_takes_the_limits_of_the_new_instance(server, monkeypatch):
    added = []
    monkeypatch.setattr(instances, "add", lambda s, reg, name, access, url, phone, frontend, group, **limits:
                        added.append((name, limits)))
    assert cli.main(["instance", "add", "alice", "--access", "local", "--cpu", "4", "--memory-mb", "8192",
                     "--disk-gb", "50"]) == 0
    assert cli.main(["instance", "add", "bob", "--access", "local"]) == 0
    assert added == [("alice", {"cpu": 4, "memory_mb": 8192, "disk_gb": 50}),
                     ("bob", {"cpu": None, "memory_mb": None, "disk_gb": None})]   # the site's [container]
    with pytest.raises(SystemExit):
        cli.main(["instance", "add", "carol", "--access", "local", "--cpu", "0"])
    assert len(added) == 2


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", type("Terminal", (), {"isatty": lambda self: True})())


@pytest.fixture
def added(monkeypatch):
    """The provider of each instance `instance add` adds."""
    added = []
    monkeypatch.setattr(instances, "add",
                        lambda s, reg, name, access, url, **kw: added.append(s.model_provider))
    return added


@pytest.mark.parametrize("answers, provider", [
    (["", "", ""], "openai-codex"),   # the site's frontend, the site's provider, no phone line
    (["", "2", ""], "claude-subscription-directsdk-experimental"),
])
def test_instance_add_asks_for_the_instances_provider_but_no_model(server, terminal, added, monkeypatch, answers,
                                                                    provider):
    answering(monkeypatch, *answers)
    assert cli.main(["instance", "add", "alice", "--access", "local"]) == 0
    assert added == [provider]


def test_instance_add_leaves_the_provider_to_the_lock(server, terminal, added, monkeypatch):
    site, _ = server
    site.write_text(VALID.replace('model = "m1"', 'model = "m1"\nmodel_lock = true'))
    answering(monkeypatch, "", "")   # the site's frontend, no phone line
    assert cli.main(["instance", "add", "alice", "--access", "local"]) == 0
    assert added == ["openai-codex"]


@pytest.fixture
def frontends(monkeypatch):
    """The frontend `instance add` gives each instance (None: the one it has, or the site's)."""
    frontends = []
    monkeypatch.setattr(instances, "add", lambda s, reg, name, access, url, frontend, **kw: frontends.append(frontend))
    return frontends


@pytest.mark.parametrize("site_frontend, answer, frontend", [
    ("webapp", "", "webapp"), ("webapp", "2", "webui"), ("webui", "", "webui"), ("webui", "1", "webapp"),
])
def test_instance_add_asks_which_frontend_a_new_instance_gets(server, terminal, frontends, monkeypatch,
                                                              site_frontend, answer, frontend):
    site, _ = server
    site.write_text(VALID + f'\n[webapp]\nfrontend = "{site_frontend}"\n')
    answering(monkeypatch, answer, "", "")   # the frontend, the site's provider, no phone line
    assert cli.main(["instance", "add", "alice", "--access", "local"]) == 0
    assert frontends == [frontend]


def test_instance_add_asks_no_frontend_when_given_one_or_when_resuming(server, terminal, frontends, monkeypatch):
    answering(monkeypatch, "", "")   # the site's provider, no phone line
    assert cli.main(["instance", "add", "alice", "--access", "local", "--frontend", "webui"]) == 0
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: Registry(
        {"bob": Instance("bob", 9120, "local", frontend="webui")})))   # its add stopped halfway
    answering(monkeypatch, "", "")
    assert cli.main(["instance", "add", "bob", "--access", "local"]) == 0
    assert frontends == ["webui", None]
    with pytest.raises(SystemExit):
        cli.main(["instance", "add", "carol", "--access", "local", "--frontend", "open-webui"])


@pytest.mark.parametrize("name, error", [
    ("vitek_test", "invalid name 'vitek_test': 2-32 characters"),
    ("vitek", "'vitek' is a system account name; choose another name"),
    ("taken", "an Incus container 'taken' already exists; choose another name"),
    ("alice", "alice already exists (use: thinny instance access alice ...)"),
    ("bob", "bob was disabled; to reuse its name: thinny instance remove bob --purge"),
])
def test_instance_add_refuses_a_name_it_cannot_take_before_asking_anything(server, terminal, added, monkeypatch,
                                                                          capsys, name, error):
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: Registry(
        {"alice": Instance("alice", 9120, "local", state="active"),
         "bob": Instance("bob", 9121, "local", state="disabled")})))
    monkeypatch.setattr(instances, "uid_of", lambda name: 1000 if name == "vitek" else None)
    monkeypatch.setattr(instances, "container_state", lambda name: "RUNNING" if name == "taken" else None)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    assert cli.main(["instance", "add", name]) == 1
    assert error in capsys.readouterr().err
    assert asked == [] and added == []


def test_instance_frontend_switches_an_instance_to_the_other_frontend(server, monkeypatch):
    switched = []
    monkeypatch.setattr(instances, "change_frontend", lambda s, reg, name, frontend: switched.append((name, frontend)))
    assert cli.main(["instance", "frontend", "alice", "webui"]) == 0
    assert cli.main(["instance", "frontend", "alice", "webapp"]) == 0
    assert switched == [("alice", "webui"), ("alice", "webapp")]
    with pytest.raises(SystemExit):
        cli.main(["instance", "frontend", "alice", "open-webui"])


@pytest.fixture
def phones(monkeypatch):
    """The phone line of each instance `instance add` adds."""
    phones = []
    monkeypatch.setattr(instances, "add", lambda s, reg, name, access, url, phone, **kw: phones.append(phone))
    return phones


def test_instance_add_can_connect_a_phone_line_with_odoriks_defaults(server, terminal, phones, monkeypatch):
    # the site's frontend and provider; a line at Odorik, a password as typed, PINs too short and in other digits
    # than the plugin's, callers, the number Hermes calls
    hidden = answering(monkeypatch, "", "", "y", "", "794000", " secret", "12", "１２３４", "1234",
                       "420777000111,420777000222", "", "sk-key")
    assert cli.main(["instance", "add", "alice", "--access", "local"]) == 0
    assert phones == [{"SIP_SERVER": "sip.odorik.cz", "SIP_USER": "794000", "SIP_PASSWORD": " secret",
                       "PHONE_PIN": "1234", "PHONE_ALLOWED_CALLERS": "420777000111,420777000222",
                       "PHONE_HOME_NUMBER": "00420777000111", "VOICE_TOOLS_OPENAI_KEY": "sk-key"}]
    assert [prompt.split()[0] for prompt in hidden] == ["SIP", "PIN", "PIN", "PIN", "OpenAI"]   # the secrets


def test_instance_add_connects_no_phone_line_unless_asked_to(server, terminal, phones, monkeypatch):
    answering(monkeypatch, "", "", "")
    assert cli.main(["instance", "add", "alice", "--access", "local"]) == 0
    assert phones == [None]


def test_what_a_failing_command_in_a_container_said_reaches_the_admin_without_control_characters(
        server, monkeypatch, capsys):
    # Its users are root in it: every command there sets the terminal's title, clears it and turns the text around.
    said = ("import sys; print('\\x1b]0;owned\\x07\\x1b[2Jcannot write\\u202e'); "
            "print('no\\tspace\\r', file=sys.stderr); sys.exit(1)")
    monkeypatch.setattr(system, "as_user_cmd", lambda name, cmd: [sys.executable, "-c", said])
    alice = Instance("alice", 9120, "local", state="active")
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: Registry({"alice": alice})))
    assert cli.main(["instance", "reset-password", "alice"]) == 1
    out, err = capsys.readouterr()
    assert err.endswith(" -> exit 1\n]0;owned[2Jcannot write\nno\tspace\n")
    assert all(c.isprintable() or c in "\n\t" for c in out + err)


@pytest.fixture
def login(server, terminal, monkeypatch):
    """`instance login` on a terminal, for alice who uses the Claude subscription; records the logins."""
    alice = Instance("alice", 9120, "local", state="active")
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: Registry({"alice": alice})))
    monkeypatch.setattr(instances, "model_of", lambda s, reg, name: (instances.CLAUDE_PROVIDER, "claude-opus-5-5"))
    logins = []
    monkeypatch.setattr(instances, "login", lambda reg, name, provider: logins.append((name, provider)))
    return logins, alice


@pytest.mark.parametrize("args, answers, provider", [
    ([], [""], "claude-subscription-directsdk-experimental"),   # the one it uses
    ([], ["1"], "openai-codex"),
    (["--provider", "openai"], [], "openai-codex"),
    (["--provider", "anthropic"], [], "claude-subscription-directsdk-experimental"),
])
def test_instance_login_signs_in_to_the_chosen_provider(login, monkeypatch, args, answers, provider):
    answering(monkeypatch, *answers)
    assert cli.main(["instance", "login", "alice", *args]) == 0
    assert login[0] == [("alice", provider)]


def test_instance_login_offers_only_openai_and_anthropic(login, monkeypatch):
    # others are set up in Hermes itself
    answering(monkeypatch, "3", "1")
    assert cli.main(["instance", "login", "alice"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["instance", "login", "alice", "--provider", "nous"])
    assert login[0] == [("alice", "openai-codex")]


@pytest.mark.parametrize("site_lock, own_lock", [(True, None), (False, ["gpt-5.5"])])
def test_instance_login_signs_a_locked_instance_in_to_its_provider_unasked(server, login, monkeypatch, site_lock,
                                                                         own_lock):
    site, _ = server
    logins, alice = login
    if site_lock:
        site.write_text(VALID.replace('model = "m1"', 'model = "m1"\nmodel_lock = true'))
    alice.lock, alice.lock_provider = own_lock, instances.CLAUDE_PROVIDER
    answering(monkeypatch)
    assert cli.main(["instance", "login", "alice"]) == 0
    assert logins == [("alice", "claude-subscription-directsdk-experimental")]


def test_instance_lock_takes_the_models_and_unlock_the_name(server, monkeypatch):
    calls = []
    monkeypatch.setattr(instances, "lock", lambda s, reg, name, models: calls.append(("lock", name, models)))
    monkeypatch.setattr(instances, "unlock", lambda s, reg, name: calls.append(("unlock", name)))
    assert cli.main(["instance", "lock", "alice", "m2", "m3"]) == 0
    assert cli.main(["instance", "lock", "bob"]) == 0
    assert cli.main(["instance", "unlock", "alice"]) == 0
    assert calls == [("lock", "alice", ["m2", "m3"]), ("lock", "bob", []), ("unlock", "alice")]


def test_instance_model_shows_or_sets_the_default_model_of_an_instance(server, monkeypatch):
    calls = []
    monkeypatch.setattr(instances, "default_model",
                        lambda s, reg, name, model, provider: calls.append((name, model, provider)))
    assert cli.main(["instance", "model", "alice"]) == 0
    assert cli.main(["instance", "model", "alice", "gpt-5.5"]) == 0
    assert cli.main(["instance", "model", "alice", "claude-opus-5-5", "--provider", "anthropic"]) == 0
    assert calls == [("alice", None, None), ("alice", "gpt-5.5", None),
                     ("alice", "claude-opus-5-5", instances.CLAUDE_PROVIDER)]


def test_instance_resize_changes_only_the_given_limits(server, monkeypatch):
    changed = []
    monkeypatch.setattr(instances, "limits", lambda reg, *args: changed.append(args))
    assert cli.main(["instance", "resize", "alice", "--disk-gb", "60"]) == 0
    assert changed == [("alice", None, None, 60)]
    with pytest.raises(SystemExit):
        cli.main(["instance", "resize", "alice", "--memory-mb", "lots"])


def test_instance_usage_takes_every_active_instance_or_one_named(server, capsys):
    assert cli.main(["instance", "usage"]) == 0
    assert capsys.readouterr().out == "NAME  PROVIDER  5H  WEEK\n"   # none yet
    assert cli.main(["instance", "usage", "nobody"]) == 1
    assert "no instance named 'nobody'" in capsys.readouterr().err


def test_instance_telegram_waits_without_thinnys_lock(server, monkeypatch):
    connected = []

    def connect(s, name):
        with system.lock():   # free for other runs, e.g. the nightly update
            connected.append(name)
    monkeypatch.setattr(telegram, "connect", connect)
    assert cli.main(["instance", "telegram", "alice"]) == 0
    assert connected == ["alice"]


def test_instance_email_takes_who_may_write_and_runs_with_thinnys_lock(server, monkeypatch):
    connected = []

    def connect(s, reg, name, senders, new_password):
        with pytest.raises(system.CommandError, match="in progress"):
            with system.lock():
                pass
        connected.append((name, senders, new_password))
    monkeypatch.setattr(mail, "connect", connect)
    assert cli.main(["instance", "email", "alice", "anna@example.com", "ben@example.com"]) == 0
    assert cli.main(["instance", "email", "alice", "--new-password"]) == 0
    assert connected == [("alice", ["anna@example.com", "ben@example.com"], False), ("alice", [], True)]


def test_instance_export_runs_with_thinnys_lock(server, monkeypatch):
    exported = []

    def export_(reg, name):
        with pytest.raises(system.CommandError, match="in progress"):
            with system.lock():
                pass
        exported.append(name)
    monkeypatch.setattr(export, "export", export_)
    assert cli.main(["instance", "export", "alice"]) == 0
    assert exported == ["alice"]


def test_instance_trace_sheet_takes_a_url_or_off(server, monkeypatch):
    connected = []
    monkeypatch.setattr(trace, "connect_sheet", lambda reg, name, url: connected.append((name, url)))
    assert cli.main(["instance", "trace-sheet", "alice", "https://script.google.com/macros/s/x/exec"]) == 0
    assert cli.main(["instance", "trace-sheet", "alice", "off"]) == 0
    assert connected == [("alice", "https://script.google.com/macros/s/x/exec"), ("alice", "off")]


def test_instance_move_takes_names_or_all(server, monkeypatch, capsys):
    moved = []
    monkeypatch.setattr(move, "move", lambda s, reg, names, to: moved.append((names, to)))
    assert cli.main(["instance", "move", "alice", "bob", "--to", "admin@new"]) == 0
    assert cli.main(["instance", "move", "--all", "--to", "new"]) == 0
    assert moved == [(["alice", "bob"], "admin@new"), ([], "new")]
    assert cli.main(["instance", "move", "alice", "--all", "--to", "new"]) == 1
    assert "name the instances to move, or give --all" in capsys.readouterr().err
    assert len(moved) == 2


def test_the_other_server_takes_the_archive_from_stdin_or_only_checks(server, monkeypatch):
    received = []
    monkeypatch.setattr(move, "receive", lambda s, reg, *args, frontend, mailbox:
                        received.append((*args, frontend, mailbox)))
    monkeypatch.setattr(cli.sys, "stdin", type("Pipe", (), {"buffer": "the archive"})())
    args = ["instance", "receive", "bob", "--access", "url", "--url", "https://bob.example.org/",
            "--release", "v2", "c0ffee", "--built-ins", ""]   # added by a thinny without built-ins
    assert cli.main(args) == 0
    assert cli.main([*args, "--check", "--frontend", "webui", "--mailbox", "bob@example.org"]) == 0
    assert received == [
        ("bob", "url", "https://bob.example.org/", ("v2", "c0ffee"), "", "the archive", "webapp", ""),
        ("bob", "url", "https://bob.example.org/", ("v2", "c0ffee"), "", None, "webui", "bob@example.org")]


def test_the_trace_runs_even_with_a_broken_site_toml(server, monkeypatch):
    site, _ = server
    site.write_text("[policy]\nmodel = \n")
    monkeypatch.setattr(trace, "run", lambda: 1)
    assert cli.main(["trace"]) == 1


def test_config_edit_applies_a_valid_change_even_to_a_broken_site_toml(server, monkeypatch):
    site, calls = server
    site.write_text("[policy]\nmodel = \n")
    edited = VALID.replace('"m1"', '"m2"')
    started = editor(monkeypatch, edited)
    assert cli.main(["config", "edit"]) == 0
    assert started == [["code", "--wait"]]
    assert site.read_text() == edited
    assert site.stat().st_mode & 0o777 == 0o600   # it may hold the Telegram manager bot's token
    assert calls == [("apply", "m2")]


def test_config_edit_offers_another_try_and_can_discard(server, monkeypatch, capsys):
    site, calls = server
    broken = VALID + "[nonsense]\nkey = 1\n"
    started = editor(monkeypatch, broken, broken)
    answers = iter(["", "d"])  # edit again, then discard
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert cli.main(["config", "edit"]) == 0
    assert len(started) == 2
    assert site.read_text() == VALID
    assert calls == []
    assert "unknown setting nonsense.key" in capsys.readouterr().out


def test_config_edit_discards_an_invalid_change_when_nobody_can_answer(server, monkeypatch):
    site, calls = server
    editor(monkeypatch, VALID + "[nonsense]\nkey = 1\n")

    def no_terminal(prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", no_terminal)
    assert cli.main(["config", "edit"]) == 0
    assert site.read_text() == VALID
    assert calls == []


@pytest.mark.parametrize("text, code, exit", [(VALID, 0, 0),           # nothing changed
                                              ("[policy]\n", 1, 1)])  # the editor quit with an error
def test_config_edit_changes_nothing_without_a_saved_change(server, monkeypatch, text, code, exit):
    site, calls = server
    editor(monkeypatch, text, code=code)
    assert cli.main(["config", "edit"]) == exit
    assert site.read_text() == VALID
    assert calls == []


def test_config_edit_refuses_before_editing_while_another_run_holds_the_lock(server, monkeypatch):
    site, calls = server
    started = editor(monkeypatch, VALID.replace('"m1"', '"m2"'))
    with system.lock():  # e.g. the nightly update
        assert cli.main(["config", "edit"]) == 1
    assert started == []
    assert site.read_text() == VALID


def test_config_edit_with_a_missing_editor_fails(server, monkeypatch, capsys):
    monkeypatch.setenv("VISUAL", "no-such-editor --wait")
    assert cli.main(["config", "edit"]) == 1
    assert "cannot start no-such-editor --wait" in capsys.readouterr().err


def test_config_edit_without_an_editor_says_what_to_do(server, monkeypatch, capsys):
    site, calls = server
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["config", "edit"]) == 1
    assert f"edit {site}, then run: thinny config apply" in capsys.readouterr().err
    assert calls == []


def test_config_edit_starts_a_new_group_from_the_example_and_applies_it(server, monkeypatch, tmp_path):
    _, calls = server
    example = (host.LIB / "group.example.toml").read_text()
    started = editor(monkeypatch, example + 'display.language = "cs"\n')
    assert cli.main(["config", "edit", "--group", "sales"]) == 0
    group = tmp_path / "groups/sales.toml"
    assert group.read_text().endswith('\ndisplay.language = "cs"\n')
    assert group.stat().st_mode & 0o777 == 0o600   # its MCP servers may hold tokens
    assert settings.load(tmp_path / "config.toml").groups == {"sales": {"display": {"language": "cs"}}}
    assert calls == [("apply", "m1")]

    editor(monkeypatch, example)   # a new group left as the example
    assert cli.main(["config", "edit", "--group", "support"]) == 0
    assert not (tmp_path / "groups/support.toml").exists() and len(calls) == 1


def test_config_edit_secrets_starts_from_the_example_and_applies_nothing(server, monkeypatch):
    _, calls = server
    example = (host.LIB / "secrets.example.env").read_text()
    editor(monkeypatch, example)   # left as the example
    assert cli.main(["config", "edit", "--secrets"]) == 0
    assert not settings.SECRETS_ENV.exists()

    editor(monkeypatch, example + "THINNY_PURELYMAIL_API_TOKEN=pm-live-x\n")
    assert cli.main(["config", "edit", "--secrets"]) == 0
    assert settings.secret("THINNY_PURELYMAIL_API_TOKEN") == "pm-live-x"
    assert settings.SECRETS_ENV.stat().st_mode & 0o777 == 0o600
    assert calls == []   # commands read it when they need a token


def test_config_edit_secrets_refuses_a_line_that_is_no_token_without_showing_it(server, monkeypatch, capsys):
    editor(monkeypatch, "pm-live-hush\n")
    monkeypatch.setattr("builtins.input", lambda prompt: "d")
    assert cli.main(["config", "edit", "--secrets"]) == 0
    out = capsys.readouterr().out
    assert "secrets.env: line 1 is not NAME=value" in out and "hush" not in out
    assert not settings.SECRETS_ENV.exists()


def test_config_edit_refuses_an_invalid_group(server, monkeypatch, capsys, tmp_path):
    _, calls = server
    editor(monkeypatch, "[webapp]\nhost = \"0.0.0.0\"\n")
    monkeypatch.setattr("builtins.input", lambda prompt: "d")
    assert cli.main(["config", "edit", "--group", "sales"]) == 0
    assert "webapp is thinny's" in capsys.readouterr().out
    assert cli.main(["config", "edit", "--group", "../sales"]) == 1
    assert "invalid name '../sales'" in capsys.readouterr().err
    assert not (tmp_path / "groups").exists() and calls == []


def test_instances_join_and_leave_groups_that_exist(server, monkeypatch, tmp_path, capsys):
    (tmp_path / "groups").mkdir()
    (tmp_path / "groups/sales.toml").write_text('display.language = "cs"\n')
    calls = []
    monkeypatch.setattr(instances, "add", lambda s, reg, name, access, url, group, **kw: calls.append(("add", group)))
    monkeypatch.setattr(instances, "set_group", lambda s, reg, name, group: calls.append(("set", name, group)))
    monkeypatch.setattr(instances, "list_groups", lambda s, reg: calls.append(("groups", list(s.groups))))
    monkeypatch.setattr(instances, "list_instances", lambda reg, group: calls.append(("list", group)))
    assert cli.main(["instance", "add", "alice", "--access", "local", "--group", "sales"]) == 0
    assert cli.main(["instance", "add", "bob", "--access", "local"]) == 0
    assert cli.main(["instance", "group", "set", "alice", "sales"]) == 0
    assert cli.main(["instance", "group", "set", "alice", "none"]) == 0
    assert cli.main(["instance", "group", "list"]) == 0
    assert cli.main(["instance", "list", "--group", "sales"]) == 0
    assert cli.main(["instance", "list"]) == 0
    assert calls == [("add", "sales"), ("add", None), ("set", "alice", "sales"), ("set", "alice", ""),
                     ("groups", ["sales"]), ("list", "sales"), ("list", None)]
    for args in (["instance", "add", "carol", "--access", "local", "--group", "support"],
                 ["instance", "group", "set", "alice", "support"]):
        assert cli.main(args) == 1
        assert "no group support: create it with thinny config edit --group support" in capsys.readouterr().err
    assert cli.main(["config", "edit", "--group", "none"]) == 1
    assert "'none' means no group" in capsys.readouterr().err
    assert len(calls) == 7


@pytest.fixture
def sales(server, monkeypatch):
    """Instances in the group sales, in port order: alice and dave active, bob disabled, carol in no group."""
    reg = Registry({"dave": Instance("dave", 9123, "local", state="active", group="sales"),
                    "alice": Instance("alice", 9120, "local", state="active", group="sales"),
                    "bob": Instance("bob", 9121, "local", state="disabled", group="sales"),
                    "carol": Instance("carol", 9122, "local", state="active")})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    done = []
    monkeypatch.setattr(instances, "restart_all",
                        lambda s, insts, reg: done.append(("restart", [inst.name for inst in insts])))
    monkeypatch.setattr(instances, "lock", lambda s, reg, name, models: done.append(("lock", name, models)))
    monkeypatch.setattr(instances, "unlock", lambda s, reg, name: done.append(("unlock", name)))
    monkeypatch.setattr(instances, "default_model",
                        lambda s, reg, name, model, provider: done.append(("model", name, model, provider)))
    monkeypatch.setattr(instances, "limits", lambda reg, name, *limits: done.append(("limits", name, *limits)))
    monkeypatch.setattr(instances, "login", lambda reg, name, provider: done.append(("login", name, provider)))
    monkeypatch.setattr(move, "move", lambda s, reg, names, to: done.append(("move", names, to)))
    return done


def test_commands_take_the_active_instances_of_a_group(sales):
    for args in (["restart"], ["lock", "m2", "m3"], ["lock"], ["unlock"], ["model", "gpt-5.5", "--provider", "openai"],
                 ["model"], ["resize", "--cpu", "4"], ["login", "--provider", "openai"], ["move", "--to", "new"]):
        assert cli.main(["instance", *args, "--group", "sales"]) == 0
    assert sales == [("restart", ["alice", "dave"]),
                     ("lock", "alice", ["m2", "m3"]), ("lock", "dave", ["m2", "m3"]),   # models, not names
                     ("lock", "alice", []), ("lock", "dave", []),
                     ("unlock", "alice"), ("unlock", "dave"),
                     ("model", "alice", "gpt-5.5", "openai-codex"),   # a model, not a name
                     ("model", "dave", "gpt-5.5", "openai-codex"),
                     ("model", "alice", None, None), ("model", "dave", None, None),
                     ("limits", "alice", 4, None, None), ("limits", "dave", 4, None, None),
                     ("login", "alice", "openai-codex"), ("login", "dave", "openai-codex"),
                     ("move", ["alice", "dave"], "new")]


@pytest.mark.parametrize("args, message", [
    (["restart", "carol", "--group", "sales"], "name the instances, or give --group"),   # not both
    (["restart", "--group", ""], "name the instances, or give --group"),   # not every instance
    (["model", "carol", "gpt-5.5", "--group", "sales"], "name the instances, or give --group"),
    (["move", "carol", "--group", "sales", "--to", "new"], "name the instances to move, or give --all or --group"),
    (["move", "--all", "--group", "sales", "--to", "new"], "name the instances to move, or give --all or --group"),
])
def test_a_group_and_names_do_not_mix(sales, capsys, args, message):
    assert cli.main(["instance", *args]) == 1
    assert message in capsys.readouterr().err
    assert sales == []


def test_a_group_without_active_instances_selects_nothing(sales, capsys):
    for args in (["restart"], ["move", "--to", "new"]):   # not every instance
        assert cli.main(["instance", *args, "--group", "support"]) == 1
        assert "no active instance is in the group support" in capsys.readouterr().err
    assert sales == []


@pytest.mark.parametrize("command, args, error", [
    ([], [], "the following arguments are required: command"),
    ([], ["bogus"], "argument command: invalid choice: 'bogus'"),
    (["instance", "group"], [], "the following arguments are required: action"),
    (["instance", "telegram"], [], "the following arguments are required: name"),
    (["instance", "email"], [], "the following arguments are required: name"),
    (["instance", "frontend"], ["alice", "x"], "argument frontend: invalid choice: 'x'"),
    (["instance", "telegram"], ["alice", "--bogus"], "unrecognized arguments: --bogus"),
    (["instance", "group", "list"], ["extra"], "unrecognized arguments: extra"),
    (["instance", "login"], [], "name the instances, or give --group"),
    (["instance", "resize"], ["--cpu", "4"], "name the instances, or give --group"),
    (["instance", "lock"], [], "name the instances, or give --group"),
    (["instance", "unlock"], ["--group", ""], "name the instances, or give --group"),
    (["instance", "model"], [], "name the instances, or give --group"),
    (["instance", "move"], ["--to", "new"], "name the instances to move, or give --all or --group"),
])
def test_a_usage_error_shows_the_whole_help_of_its_command(monkeypatch, capsys, command, args, error):
    # without root, and before sudo would ask for a password
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.os, "execvp", lambda file, args: pytest.fail("ran sudo"))
    with pytest.raises(SystemExit):
        cli.main([*command, "-h"])
    help = capsys.readouterr().out
    with pytest.raises(SystemExit) as exit:
        cli.main([*command, *args])
    assert exit.value.code == 2
    assert capsys.readouterr().err.startswith(f"{help}{' '.join(['thinny', *command])}: error: {error}")


def tab(capsys, *words):
    """What Tab offers after `thinny`, the last of `words` being the one typed."""
    assert cli.main(["complete", *words]) == 0
    return capsys.readouterr().out.splitlines()


@pytest.mark.parametrize("words, offered", [
    ([""], ["init", "setup", "status", "doctor", "update", "config", "instance", "uninstall"]),   # not trace
    (["instance", "re"], ["resize", "reset-password", "remove", "restart"]),                      # not receive
    (["instance", "e"], ["email", "export"]),
    (["instance", "group", ""], ["list", "set"]),
    (["update", "--d"], ["--dry-run"]),
    (["instance", "restart", "--"], ["--help", "--group"]),
    (["instance", "login", "--provider", ""], ["openai", "anthropic"]),
    (["instance", "login", "--provider", "="], ["openai", "anthropic"]),   # bash's words for --provider=
    (["instance", "login", "--provider", "=", "op"], ["openai"]),
    (["instance", "model", "--provider", ""], ["openai", "anthropic"]),
    (["instance", "add", "alice", "--frontend", "webu"], ["webui"]),
    (["instance", "frontend", "alice", ""], ["webapp", "webui"]),
    (["instance", "login", ""], []),   # only root reads the instances
])
def test_tab_completes_commands_and_options_without_root(monkeypatch, capsys, words, offered):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    assert tab(capsys, *words) == offered


def test_tab_completes_the_names_of_instances_for_root(monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active"),
                    "bob": Instance("bob", 9121, "local", state="disabled"),
                    "carol": Instance("carol", 9122, "local", state="removed")})
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    assert tab(capsys, "instance", "login", "") == ["alice", "bob"]
    assert tab(capsys, "instance", "resize", "--cpu", "4", "") == ["alice", "bob"]
    assert tab(capsys, "instance", "usage", "") == ["alice", "bob"]
    assert tab(capsys, "instance", "login", "--provider", "=", "openai", "") == ["alice", "bob"]
    assert tab(capsys, "instance", "restart", "alice", "") == ["alice", "bob"]
    assert tab(capsys, "instance", "group", "set", "a") == ["alice"]
    assert tab(capsys, "instance", "export", "") == ["alice", "bob"]
    assert tab(capsys, "instance", "remove", "--purge", "") == ["alice", "bob", "carol"]
    assert tab(capsys, "instance", "lock", "alice", "") == []   # models
    assert tab(capsys, "instance", "model", "") == ["alice", "bob"]
    assert tab(capsys, "instance", "model", "alice", "") == []  # a model
    assert tab(capsys, "instance", "add", "") == []             # a new name
