import subprocess
from pathlib import Path

import pytest

from thinny import apply, cli, host, instances, render, settings, system, update
from thinny.registry import Registry

VALID = '[policy]\nmodel = "m1"\nallowed_models = ["m1"]\n\n[access]\nlan_address = "192.168.1.10"\n'


@pytest.fixture
def server(tmp_path, monkeypatch):
    """A root run against site.toml in tmp_path; records the applies and updates it starts. thinny
    itself has nothing new."""
    site = tmp_path / "site.toml"
    site.write_text(VALID)
    monkeypatch.setattr(settings, "SITE_TOML", site)
    monkeypatch.setattr(cli, "load", lambda: settings.load(site))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: Registry()))
    monkeypatch.setattr(system, "LOCK_FILE", tmp_path / "lock")
    calls = []
    monkeypatch.setattr(apply, "apply", lambda s: calls.append(("apply", s.model)))
    monkeypatch.setattr(update, "run", lambda s, reg, **kw: calls.append(("update", kw)) or 0)
    monkeypatch.setattr(update, "update_thinny", lambda: calls.append("thinny") or False)
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
    """A server without site.toml, on a terminal. It reaches the internet through enp1s0 from
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
    """The terminal gives `answers` in turn ("" is Enter), then ends."""
    answers = list(answers)

    def answer(prompt=""):
        if not answers:
            raise EOFError
        return answers.pop(0)
    monkeypatch.setattr("builtins.input", answer)


@pytest.mark.parametrize("typed, language", [("cs", "cs"), ("pt-BR", "pt-br")])
def test_init_writes_the_answers_into_the_commented_example(new_server, monkeypatch, capsys, typed, language):
    # provider, model (asked again after Enter), language, interface, own addresses, tunnel port, write
    answering(monkeypatch, "", "", "gpt-5.5", typed, "", "2", "", "")
    assert cli.main(["init"]) == 0
    s = settings.load(new_server)
    assert (s.model_provider, s.model, s.language, s.language_pack, s.network_interface) == \
        ("openai-codex", "gpt-5.5", language, f"hermes-lang-{language}", "enp1s0")
    assert (s.access_default, s.lan_address, s.tunnel_port) == ("url", "", 8080)
    assert "# default model" in new_server.read_text()
    assert "one tunnel at http://127.0.0.1:8080" in capsys.readouterr().out


def test_init_offers_the_detected_lan_address(new_server, monkeypatch):
    # provider, model, language (no pack in the catalog), interface, LAN (the default), its address, write
    answering(monkeypatch, "anthropic", "claude", "de", "eth1", "", "", "y")
    assert cli.main(["init"]) == 0
    s = settings.load(new_server)
    assert (s.model_provider, s.model, s.language, s.language_pack, s.network_interface) == \
        ("anthropic", "claude", "de", "", "eth1")
    assert (s.access_default, s.lan_address, s.tunnel_port) == ("lan", "192.168.1.5", 0)


def test_init_says_when_it_could_not_check_for_a_language_pack(new_server, monkeypatch, capsys):
    monkeypatch.setattr(system, "http_status", lambda url, timeout: "503")
    answering(monkeypatch, "", "gpt-5.5", "cs", "", "3", "")
    assert cli.main(["init"]) == 0
    assert settings.load(new_server).language_pack == ""
    assert "could not check Hermes's plugin catalog" in capsys.readouterr().out


@pytest.mark.parametrize("answers, message", [
    (("", "gpt-5.5", "", "", "", "", "n"), "nothing written"),
    (("", "gpt-5.5", "", "", "2", "9150"), "tunnel_port"),   # in the Webapp port range
    (("", "gpt-5.5", "", "", "2", "lots"), "'lots' is not a port"),
    (("",), "nothing written"),                                # the terminal ends (Ctrl-D)
])
def test_init_writes_nothing_without_a_valid_confirmed_answer(new_server, monkeypatch, capsys, answers, message):
    answering(monkeypatch, *answers)
    assert cli.main(["init"]) == 1
    assert not new_server.exists()
    assert message in capsys.readouterr().err


def test_init_leaves_an_existing_site_toml_alone(server, capsys):
    site, _ = server
    assert cli.main(["init"]) == 1
    assert site.read_text() == VALID
    assert "thinny config edit" in capsys.readouterr().err


@pytest.mark.parametrize("args, auto_update, kw, thinny", [
    ([], "false", {"dry_run": False, "now": True}, True),
    (["--dry-run"], "true", {"dry_run": True, "now": True}, False),
    (["--scheduled"], "true", {"dry_run": False, "now": False}, True),
    (["--scheduled"], "false", {"dry_run": False, "now": False}, False),
])
def test_a_manual_update_runs_now_and_only_the_timer_waits(server, args, auto_update, kw, thinny):
    site, calls = server
    site.write_text(VALID + f"\n[hermes]\nauto_update = {auto_update}\n")
    assert cli.main(["update", *args]) == 0
    assert calls == ["thinny"] * thinny + [("update", kw)]
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
    assert calls == [("exec", "/usr/local/sbin/thinny", ["/usr/local/sbin/thinny", "update", *args])]


def test_a_failed_thinny_update_stops_the_update(server, monkeypatch, capsys):
    _, calls = server

    def broken():
        raise system.CommandError("git pull -> exit 1")
    monkeypatch.setattr(update, "update_thinny", broken)
    assert cli.main(["update"]) == 1
    assert calls == []
    assert "git pull -> exit 1" in capsys.readouterr().err


def test_instance_add_takes_the_limits_of_the_new_instance(server, monkeypatch):
    added = []
    monkeypatch.setattr(instances, "add", lambda s, reg, name, access, url, **limits: added.append((name, limits)))
    assert cli.main(["instance", "add", "alice", "--access", "local", "--cpu", "4", "--memory-mb", "8192",
                     "--disk-gb", "50"]) == 0
    assert cli.main(["instance", "add", "bob", "--access", "local"]) == 0
    assert added == [("alice", {"cpu": 4, "memory_mb": 8192, "disk_gb": 50}),
                     ("bob", {"cpu": None, "memory_mb": None, "disk_gb": None})]   # the site's [container]
    with pytest.raises(SystemExit):
        cli.main(["instance", "add", "carol", "--access", "local", "--cpu", "0"])
    assert len(added) == 2


def test_instance_limits_changes_only_the_given_limits(server, monkeypatch):
    changed = []
    monkeypatch.setattr(instances, "limits", lambda reg, *args: changed.append(args))
    assert cli.main(["instance", "limits", "alice", "--disk-gb", "60"]) == 0
    assert changed == [("alice", None, None, 60)]
    with pytest.raises(SystemExit):
        cli.main(["instance", "limits", "alice", "--memory-mb", "lots"])


def test_config_edit_applies_a_valid_change_even_to_a_broken_site_toml(server, monkeypatch):
    site, calls = server
    site.write_text("[policy]\nmodel = \n")
    edited = VALID.replace('"m1"', '"m2"')
    started = editor(monkeypatch, edited)
    assert cli.main(["config", "edit"]) == 0
    assert started == [["code", "--wait"]]
    assert site.read_text() == edited
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
