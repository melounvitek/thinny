"""Mail for an instance: a mailbox of its own, which its Hermes reads and sends from (Hermes's email gateway). Only
the addresses the admin names may write to it: the agent runs commands.

With email.provider custom, the admin creates the mailbox at any provider, and thinny only connects it, with the
password the admin types. With purelymail, thinny creates <name>@<domain> there; its API token stays on the server,
in secrets.env: it manages every mailbox of the Purelymail account. The instance's .env holds the mailbox's
password, and every run sets Purelymail's to it, so running it again finishes a run that failed halfway."""

from __future__ import annotations

import imaplib
import re
import secrets
import smtplib
import ssl
from collections.abc import Callable

from . import instances, purelymail
from .registry import Registry, RegistryError
from .settings import Settings
from .system import CommandError

# Role addresses (RFC 2142) and their usual companions: mail to them is for whoever runs the domain.
RESERVED = {"abuse", "admin", "administrator", "hostmaster", "mailer-daemon", "noc", "no-reply", "noreply",
            "postmaster", "root", "security", "webmaster"}
ADDRESS_RE = re.compile(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+")


def servers(s: Settings) -> tuple[str, str, str]:
    """The IMAP and SMTP hosts of the server's mailboxes (Hermes's default ports: 993 with TLS, 587 with STARTTLS),
    and the authserv-id their provider's Authentication-Results start with."""
    if s.email_provider == "custom":
        return s.email_imap_host, s.email_smtp_host, s.email_authserv_id
    return purelymail.IMAP_HOST, purelymail.SMTP_HOST, purelymail.AUTHSERV_ID


def connect(s: Settings, reg: Registry, name: str, senders: list[str], new_password: bool = False,
            address: str = "", ask: Callable[[str], str] | None = None) -> None:
    """Give the instance its mailbox, which `senders` may write to (the first also gets its scheduled jobs' mail):
    with email.provider custom, the admin's at `address`, whose password `ask` gets; at Purelymail, one thinny
    creates. Run again, it changes who may write, keeping the password unless `new_password`. No `senders` keeps
    those it has."""
    if not s.email_provider:
        raise CommandError("set email.provider first: thinny config edit (see README, Email)")
    inst = reg.get(name)
    if inst.state != "active":
        raise RegistryError(f"{name} is {inst.state}")
    own = s.email_provider == "custom"
    if inst.mailbox and (inst.own_mailbox != own or address not in ("", inst.mailbox)):
        raise CommandError(f"{name} has the mailbox {inst.mailbox}, "
                           f"{'your own' if inst.own_mailbox else 'which thinny created at Purelymail'}; "
                           "thinny does not give an instance another mailbox")
    if own:
        address = inst.mailbox or address
        if not address:
            raise CommandError(f"name the mailbox you created for {name}: thinny instance email {name} "
                               "--address <its address> ...")
        if not ADDRESS_RE.fullmatch(address):
            raise CommandError(f"{address!r} is not an address")
        others = {i.mailbox.lower(): i.name for i in reg.instances.values() if i.mailbox and i is not inst}
        if address.lower() in others:
            raise CommandError(f"{address} is {others[address.lower()]}'s mailbox")
    elif address:
        raise CommandError("--address is for mailboxes you create (email.provider custom); at Purelymail, thinny "
                           f"creates {name}@{s.email_domain}")
    else:
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

    imap, smtp, authserv_id = servers(s)
    keep = env.get("EMAIL_ADDRESS") == address and not new_password
    if own:
        password = (keep and env.get("EMAIL_PASSWORD")) or ask(f"Password of {address}")
        if re.search(r"\$\{[^}]*\}", password):   # Hermes expands them in its .env, even in quotes
            raise CommandError("Hermes would read ${...} in this password as a variable: give the mailbox another one")
        check_login(address, password, imap, smtp)
    else:
        purelymail.check_domain(s)
        exists = address in purelymail.api("listUser")["users"]
        if exists and not inst.mailbox:
            raise CommandError(f"{address} exists in Purelymail, but thinny did not create it for {name}, so it stays "
                               "as it is (if an earlier run stopped without Purelymail's answer, delete it there, "
                               "then run this again)")
        password = (keep and env.get("EMAIL_PASSWORD")) or secrets.token_urlsafe(24)
    instances.mark_restart([name])
    instances.set_env(name, {"EMAIL_ADDRESS": address, "EMAIL_PASSWORD": password, "EMAIL_IMAP_HOST": imap,
                             "EMAIL_SMTP_HOST": smtp, "EMAIL_AUTHSERV_ID": authserv_id,
                             "EMAIL_ALLOWED_USERS": ",".join(senders), "EMAIL_HOME_ADDRESS": senders[0]})
    if not own:
        if exists:
            purelymail.api("modifyUser", userName=address, newPassword=password)
        else:
            local, _, domain = address.partition("@")
            purelymail.api("createUser", userName=local, domainName=domain, password=password,
                enablePasswordReset=False, sendWelcomeEmail=False)
    if not inst.mailbox:
        inst.mailbox, inst.own_mailbox = address, own
        reg.save()   # only now: a refusal may mean that someone else created it meanwhile
    instances.ensure_mail_client(inst)
    instances.update_policy(s, inst)   # tells its agent about the mailbox
    instances.start_or_restart_gateway(s, inst)
    print(f"{name} reads and sends mail as {address}. {', '.join(senders)} may write to it.")


def check_login(address: str, password: str, imap: str, smtp: str) -> None:
    """Refuse a mailbox thinny cannot sign in to as Hermes does: its password is typed unseen, and Hermes would only
    log that it fails."""
    context = ssl.create_default_context()   # imaplib's and smtplib's own check no certificates
    try:
        with imaplib.IMAP4_SSL(imap, 993, ssl_context=context, timeout=60) as box:
            box.login(address, password)
    except (OSError, imaplib.IMAP4.error) as e:
        raise CommandError(f"cannot sign in to {address} at {imap}:993: {e}") from None
    try:
        with smtplib.SMTP(smtp, 587, timeout=60) as server:
            server.starttls(context=context)
            server.login(address, password)
    except OSError as e:   # smtplib's errors too
        raise CommandError(f"cannot sign in to {address} at {smtp}:587: {e}") from None
