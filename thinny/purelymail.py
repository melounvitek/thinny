"""Purelymail, where instances' mailboxes live: its API, and where Hermes reaches them."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .settings import Settings, secret
from .system import CommandError

API = "https://purelymail.com/api/v0/"
# Hermes's default ports are Purelymail's: IMAP over TLS on 993, SMTP with STARTTLS on 587.
IMAP_HOST, SMTP_HOST = "imap.purelymail.com", "smtp.purelymail.com"
# Purelymail's servers sign the Authentication-Results they add to incoming mail so. Hermes trusts a sender only
# through that header, and drops all mail when it does not know whose to read.
AUTHSERV_ID = "purelymail.com"


def api(method: str, **params):
    """The result of a Purelymail API call, with the account's token."""
    token = secret("THINNY_PURELYMAIL_API_TOKEN")
    if not token:
        raise CommandError("set THINNY_PURELYMAIL_API_TOKEN first: thinny config edit --secrets (see README, Email)")
    request = urllib.request.Request(API + method, json.dumps(params).encode(),
                                     {"Content-Type": "application/json", "Purelymail-Api-Token": token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as e:
        raise CommandError(f"Purelymail refused {method}: HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise CommandError(f"Purelymail did not answer {method}: {e}") from None
    if answer.get("type") != "success":   # Purelymail's errors come with HTTP 200
        raise CommandError(f"Purelymail refused {method}: {answer.get('message') or answer.get('code')}")
    return answer["result"]


def check_domain(s: Settings) -> None:
    """Refuse a domain that Purelymail does not deliver mail for as thinny needs."""
    domain = next((d for d in api("listDomains")["domains"] if d["name"] == s.email_domain), None)
    if domain is None:
        raise CommandError(f"add {s.email_domain} in Purelymail first (see README, Email)")
    failing = [check.removeprefix("passes").upper() for check, passes in domain["dnsSummary"].items() if not passes]
    if failing:
        raise CommandError(f"{s.email_domain}'s DNS records do not pass Purelymail's checks: {', '.join(failing)} "
                           "(see README, Email)")
    if domain["symbolicSubaddressing"]:   # it cuts addresses at symbols, '-' maybe too: office-sales@ to office@
        raise CommandError(f"turn off symbolic subaddressing for {s.email_domain} in Purelymail (see README, Email)")


def delete(address: str) -> None:
    """Delete a mailbox thinny created, with all its mail; one already gone counts as deleted."""
    if address in api("listUser")["users"]:
        api("deleteUser", userName=address)
