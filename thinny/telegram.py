"""Telegram for an instance: a bot of its user's own, created through the site's manager bot (Telegram's
managed bots). The user opens a link and taps Create; the manager bot learns who created which bot and can
get its token. Only that user may then chat with the instance. The manager's token stays on the server: it
gets the token of every bot created through it."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import instances
from .registry import Registry, RegistryError
from .settings import Settings
from .system import CommandError, lock

LOCK = Path("/run/thinny-telegram.lock")   # one wait at a time: Telegram gives the manager's updates to one reader
# What the new bot first says to its user, in the site's language.
READY = {"cs": "Hermes je připravený. Napište mi cokoli.", "en": "Hermes is ready. Write me anything."}


def api(token: str, method: str, **params):
    """The result of a Bot API call; getUpdates waits up to `timeout` seconds for one."""
    data = urllib.parse.urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()})
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data.encode(),
                                    timeout=params.get("timeout", 0) + 30) as response:
            return json.load(response)["result"]
    except urllib.error.HTTPError as e:   # not str(e): errors must not show the URL, with the token
        try:
            reason = json.load(e)["description"]
        except (ValueError, KeyError):
            reason = f"HTTP {e.code}"
        raise CommandError(f"Telegram refused {method}: {reason}") from None
    except OSError as e:
        raise CommandError(f"Telegram did not answer {method}: {e}") from None


def suggested_username(manager: str, name: str) -> str:
    """The new bot's username: the manager's without its "bot", then the instance's name, at most 32 characters."""
    stem = re.sub(r"_?bot$", "", manager, flags=re.IGNORECASE) + "_" + name.replace("-", "_")
    return stem[:28].rstrip("_") + "_bot"


def connect(s: Settings, name: str) -> None:
    """Print the link that creates the instance's bot, wait until its user creates it and connect it. The wait
    leaves thinny's lock to other runs, such as the nightly update; the update Telegram sent about the bot is
    done only once the bot is connected, so a failure leaves it for the next try."""
    if not s.telegram_manager_token:
        raise CommandError("set telegram.manager_token in site.toml first (see README, Telegram)")
    manager = s.telegram_manager_token
    _active(name)
    with lock(LOCK):
        username = api(manager, "getMe")["username"]
        print(f"Send {name}'s user this link. They open it in Telegram and tap Create:\n"
              f"  https://t.me/newbot/{username}/{suggested_username(username, name)}?name=Hermes\n"
              "Waiting for their bot (Ctrl+C to stop)...", flush=True)
        try:
            update_id, user, bot = _wait(manager, name)
        except (KeyboardInterrupt, EOFError):
            raise CommandError(f"stopped waiting; to wait again: thinny instance telegram {name}") from None
        token = api(manager, "getManagedBotToken", user_id=bot["id"])
        with lock():
            inst = _active(name)   # again: it may have changed during the wait
            instances.mark_restart([name])
            instances.set_env(name, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ALLOWED_USERS": str(user["id"]),
                                     "TELEGRAM_HOME_CHANNEL": str(user["id"])})
            instances.hermes(name, ["config", "set", "platforms.telegram.enabled", "true"])
            instances.start_or_restart_gateway(s, inst)
        api(manager, "getUpdates", offset=update_id + 1)
    api(token, "sendMessage", chat_id=user["id"], text=READY.get(s.language, READY["en"]))
    print(f"{_who(user)} can now chat with {name} at https://t.me/{bot['username']}")


def _active(name: str):
    inst = Registry.load().get(name)
    if inst.state != "active":
        raise RegistryError(f"{name} is {inst.state}")
    return inst


def _wait(manager: str, name: str) -> tuple[int, dict, dict]:
    """The first bot created through the manager that the admin says is `name`'s: the id of Telegram's update
    about it, its creator and the bot. Anyone who knows the manager can create one, so the admin checks who."""
    offset = 0
    while True:
        for update in api(manager, "getUpdates", offset=offset, timeout=50, allowed_updates=["managed_bot"]):
            offset = update["update_id"] + 1
            created = update.get("managed_bot")
            if created and input(f"{_who(created['user'])} created @{created['bot']['username']}. "
                                 f"Is it {name}'s? [y/N] ").strip().lower().startswith("y"):
                return update["update_id"], created["user"], created["bot"]


def _who(user: dict) -> str:
    """A Telegram user, by name, username and id."""
    full_name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
    return f"{full_name} ({'@' + user['username'] + ', ' if user.get('username') else ''}{user['id']})"
