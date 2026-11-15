import dataclasses

import pytest
from test_instances import S, gateways, incus  # noqa: F401 - the fake Incus and gateway fixtures

from thinny import instances, system, telegram
from thinny.registry import Registry, RegistryError
from thinny.system import CommandError

MANAGER = "1000:" + "M" * 35
SITE = dataclasses.replace(S, telegram_manager_token=MANAGER)
USER = {"id": 8428394378, "is_bot": False, "first_name": "Vítek", "last_name": "Meloun", "username": "vitek"}


def created(update_id, bot, user=USER):
    """The update Telegram sends the manager bot when `user` creates the bot `bot` through it."""
    return {"update_id": update_id, "managed_bot": {"user": user, "bot": {"id": 900 + update_id, "is_bot": True,
                                                                          "username": bot}}}


def token_of(bot_id):
    return f"{bot_id}:" + "B" * 35


class Telegram:
    """A fake Bot API with the manager bot @acme_hermes_bot, whose `updates` wait for it. A wait for more
    ends as the admin's Ctrl+C would; `while_waiting` runs at each wait."""

    def __init__(self, updates):
        self.updates, self.calls, self.confirmed = list(updates), [], 0   # every update below `confirmed` is done
        self.while_waiting = lambda: None

    def api(self, token, method, **params):
        self.calls.append((token, method, params))
        if method == "getMe":
            return {"id": 1000, "is_bot": True, "username": "acme_hermes_bot"}
        if method == "getUpdates":
            self.confirmed = max(self.confirmed, params.get("offset", 0))
            waiting = [u for u in self.updates if u["update_id"] >= self.confirmed]
            if params.get("timeout"):
                self.while_waiting()
                if not waiting:
                    raise KeyboardInterrupt
            return waiting
        if method == "getManagedBotToken":
            return token_of(params["user_id"])
        if method == "sendMessage":
            return {"message_id": 1}
        raise AssertionError(method)

    def sent(self):
        return [(token, params) for token, method, params in self.calls if method == "sendMessage"]


@pytest.fixture
def alice(incus, gateways, monkeypatch, tmp_path):
    """alice, active, with her Webapp sign-in in her .env; thinny's locks in tmp_path."""
    reg = Registry()
    reg.add("alice", "local", (9120, 9199)).state = "active"
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "account": True, "hermes": True, "unit": True,
                                 "files": {"/home/alice/.hermes/.env": "HERMES_WEBAPP_AUTH_USERNAME=alice\n"}}
    monkeypatch.setattr(Registry, "load", classmethod(lambda cls, path=None: reg))
    monkeypatch.setattr(system, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(telegram, "LOCK", tmp_path / "telegram.lock")
    return reg


@pytest.fixture
def bot_api(monkeypatch):
    """Installs a fake Bot API with these updates waiting."""
    def install(*updates):
        fake = Telegram(updates)
        monkeypatch.setattr(telegram, "api", fake.api)
        return fake
    return install


def answering(monkeypatch, *answers):
    """The terminal gives `answers` in turn; returns the questions asked."""
    answers, asked = list(answers), []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or answers.pop(0))
    return asked


def env(incus):
    return incus.containers["alice"]["files"]["/home/alice/.hermes/.env"]


def test_the_bot_its_user_creates_connects_to_the_instance(alice, incus, gateways, bot_api, monkeypatch, capsys):
    api = bot_api(created(7, "acme_hermes_alice_bot"))
    asked = answering(monkeypatch, "y")
    telegram.connect(SITE, "alice")
    out = capsys.readouterr().out
    assert "https://t.me/newbot/acme_hermes_bot/acme_hermes_alice_bot?name=Hermes" in out
    assert asked == ["Vítek Meloun (@vitek, 8428394378) created @acme_hermes_alice_bot. Is it alice's? [y/N] "]
    # only its creator may chat, and gets what Hermes would send elsewhere (no home channel to ask about)
    token = token_of(907)
    assert env(incus) == (f'HERMES_WEBAPP_AUTH_USERNAME=alice\nTELEGRAM_BOT_TOKEN="{token}"\n'
                          'TELEGRAM_ALLOWED_USERS="8428394378"\nTELEGRAM_HOME_CHANNEL="8428394378"\n')
    assert not any(token in call or MANAGER in call for call in incus.calls)
    assert incus.ran("hermes config set platforms.telegram.enabled true")
    restarted, _ = gateways
    assert restarted == ["alice"]
    assert api.confirmed == 8   # done with its update
    assert api.sent() == [(token, {"chat_id": 8428394378, "text": "Hermes is ready. Write me anything."})]
    assert "Vítek Meloun (@vitek, 8428394378) can now chat with alice at https://t.me/acme_hermes_alice_bot" in out


def test_the_bot_greets_its_user_in_the_sites_language(alice, bot_api, monkeypatch):
    api = bot_api(created(7, "acme_hermes_alice_bot"))
    answering(monkeypatch, "y")
    telegram.connect(dataclasses.replace(SITE, language="cs"), "alice")
    assert api.sent()[0][1]["text"] == "Hermes je připravený. Napište mi cokoli."


# Anyone who knows the manager bot can create a bot through it, also with the name the link suggests.
def test_only_a_bot_the_admin_confirms_connects(alice, incus, bot_api, monkeypatch):
    api = bot_api(created(5, "acme_hermes_alice_bot", user={"id": 666, "first_name": "Eve"}),
                  created(6, "jirkas_alice_bot"))
    asked = answering(monkeypatch, "", "y")
    telegram.connect(SITE, "alice")
    assert asked[0] == "Eve (666) created @acme_hermes_alice_bot. Is it alice's? [y/N] "
    assert [params for _, method, params in api.calls if method == "getManagedBotToken"] == [{"user_id": 906}]
    assert f'TELEGRAM_BOT_TOKEN="{token_of(906)}"' in env(incus)


def test_a_failed_connection_leaves_the_bot_for_the_next_try(alice, incus, gateways, bot_api, monkeypatch):
    _, broken = gateways
    broken.add("alice")
    api = bot_api(created(7, "acme_hermes_alice_bot"))
    answering(monkeypatch, "y", "y")
    with pytest.raises(CommandError, match="gateway not active"):
        telegram.connect(SITE, "alice")
    assert api.confirmed < 8 and api.sent() == []

    broken.clear()
    telegram.connect(SITE, "alice")
    assert api.confirmed == 8 and len(api.sent()) == 1


def test_the_wait_leaves_thinny_free_for_other_runs_but_not_for_another_wait(alice, bot_api, monkeypatch):
    api = bot_api(created(7, "acme_hermes_alice_bot"))
    answering(monkeypatch, "y")
    free = []

    def other_runs():
        with system.lock():   # e.g. the nightly update
            free.append(True)
        with pytest.raises(CommandError, match="in progress"):
            with system.lock(telegram.LOCK):
                pass
    api.while_waiting = other_runs
    telegram.connect(SITE, "alice")
    assert free


def test_an_instance_disabled_during_the_wait_gets_no_bot(alice, incus, gateways, bot_api, monkeypatch):
    api = bot_api(created(7, "acme_hermes_alice_bot"))
    answering(monkeypatch, "y")

    def disable():
        alice.get("alice").state = "disabled"
    api.while_waiting = disable
    with pytest.raises(RegistryError, match="alice is disabled"):
        telegram.connect(SITE, "alice")
    assert "TELEGRAM" not in env(incus)
    assert api.confirmed < 8


def test_stopping_the_wait_says_how_to_wait_again(alice, bot_api):
    bot_api()
    with pytest.raises(CommandError, match="thinny instance telegram alice"):
        telegram.connect(SITE, "alice")


def test_an_instance_needs_the_sites_manager_bot(alice, bot_api):
    api = bot_api()
    with pytest.raises(CommandError, match="telegram.manager_token"):
        telegram.connect(S, "alice")
    assert api.calls == []


@pytest.mark.parametrize("manager, name, username", [
    ("acme_hermes_bot", "alice", "acme_hermes_alice_bot"),
    ("AcmeHermesBot", "office-accounting", "AcmeHermes_office_accounting_bot"),
    ("acme_bot", "a" * 32, "acme_" + "a" * 23 + "_bot"),   # Telegram takes up to 32 characters
    ("m" * 28 + "_bot", "alice", "m" * 20 + "_alice_bot"),   # not the manager's own
])
def test_the_link_suggests_a_username_after_the_manager_and_the_instance(manager, name, username):
    assert telegram.suggested_username(manager, name) == username


def test_a_bot_connects_to_a_member_of_a_group_that_turns_telegram_on(alice, incus, bot_api, monkeypatch):
    bot_api(created(7, "acme_hermes_alice_bot"))
    answering(monkeypatch, "y")
    alice.get("alice").group = "sales"
    telegram.connect(dataclasses.replace(SITE, groups={"sales": {"platforms": {"telegram": {"enabled": True}}}}),
                     "alice")
    assert not incus.ran("hermes config set platforms.telegram.enabled")   # Hermes refuses what the policy pins
    assert "TELEGRAM_BOT_TOKEN" in env(incus)
