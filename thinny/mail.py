"""Mail for an instance: a mailbox of its own, <name>@<domain> at Purelymail, which its Hermes reads and sends
from (Hermes's email gateway). Only the addresses the admin names may write to it: the agent runs commands. The
API token stays on the server, in config.toml: it manages every mailbox of the Purelymail account.

The instance's .env holds the mailbox's password, and every run sets Purelymail's to it, so running it again
finishes a run that failed halfway."""

from __future__ import annotations

import re
import secrets

from . import instances, purelymail
from .registry import Registry, RegistryError
from .settings import Settings
from .system import CommandError

# Role addresses (RFC 2142) and their usual companions: mail to them is for whoever runs the domain.
RESERVED = {"abuse", "admin", "administrator", "hostmaster", "mailer-daemon", "noc", "no-reply", "noreply",
            "postmaster", "root", "security", "webmaster"}
ADDRESS_RE = re.compile(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+")


def connect(s: Settings, reg: Registry, name: str, senders: list[str], new_password: bool = False) -> None:
    """Give the instance its mailbox, which `senders` may write to (the first also gets its scheduled jobs' mail):
    create it, or change who may write, keeping its password unless `new_password`. No `senders` keeps those it
    has."""
    if not s.email_api_token:
        raise CommandError("set email.domain and email.api_token in config.toml first (see README, Email)")
    inst = reg.get(name)
    if inst.state != "active":
        raise RegistryError(f"{name} is {inst.state}")
    address = inst.mailbox or f"{name}@{s.email_domain}"   # the one it has, whatever the domain is now
    if not inst.mailbox and name in RESERVED:
        raise CommandError(f"{address} is reserved for whoever runs {s.email_domain}; {name} gets no mailbox")
    env = instances.env_values(name)
    senders = senders or [sender for sender in env.get("EMAIL_ALLOWED_USERS", "").split(",") if sender]
    if not senders:
        raise CommandError(f"name who may write to {name}: thinny instance email {name} <address>...")
    for sender in senders:
        if not ADDRESS_RE.fullmatch(sender):
            raise CommandError(f"{sender!r} is not an address; name whole addresses, like anna@example.com "
                               f"(whoever may write to {name} can have it run commands)")

    purelymail.check_domain(s)
    exists = address in purelymail.api(s.email_api_token, "listUser")["users"]
    if exists and not inst.mailbox:
        raise CommandError(f"{address} exists in Purelymail, but thinny did not create it for {name}, so it stays as "
                           "it is (if an earlier run stopped without Purelymail's answer, delete it there, then run "
                           "this again)")
    keep = env.get("EMAIL_ADDRESS") == address and not new_password
    password = (keep and env.get("EMAIL_PASSWORD")) or secrets.token_urlsafe(24)
    instances.mark_restart([name])
    instances.set_env(name, {"EMAIL_ADDRESS": address, "EMAIL_PASSWORD": password,
                             "EMAIL_IMAP_HOST": purelymail.IMAP_HOST, "EMAIL_SMTP_HOST": purelymail.SMTP_HOST,
                             "EMAIL_AUTHSERV_ID": purelymail.AUTHSERV_ID,
                             "EMAIL_ALLOWED_USERS": ",".join(senders), "EMAIL_HOME_ADDRESS": senders[0]})
    if exists:
        purelymail.api(s.email_api_token, "modifyUser", userName=address, newPassword=password)
    else:
        local, _, domain = address.partition("@")
        purelymail.api(s.email_api_token, "createUser", userName=local, domainName=domain, password=password,
            enablePasswordReset=False, sendWelcomeEmail=False)
        inst.mailbox = address
        reg.save()   # only now: a refusal may mean that someone else created it meanwhile
    instances.start_or_restart_gateway(s, inst)
    print(f"{name} reads and sends mail as {address}. {', '.join(senders)} may write to it.")
