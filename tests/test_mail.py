import dataclasses
import io
import json
import socket
import subprocess
import tomllib
import urllib.request

import pytest
import yaml
from test_instances import S, gateways, incus, typed  # noqa: F401 - the fake Incus, gateway and terminal fixtures

from thinny import checks, host, instances, mail, render, settings
from thinny.registry import Instance, Registry, RegistryError
from thinny.system import CommandError

TOKEN = "pm-live-" + "t" * 32
SITE = dataclasses.replace(S, email_domain="example.org")
NO_TOKEN = "set THINNY_PURELYMAIL_API_TOKEN first: thinny config edit --secrets \\(see README, Email\\)"
ENV = "/home/alice/.hermes/.env"
HIMALAYA_CONFIG = "/home/alice/.config/himalaya/config.toml"


class Purelymail:
    """A fake Purelymail API whose account has the domain example.org, set up. Like Purelymail, it answers
    errors with HTTP 200."""

    def __init__(self):
        self.users = {}   # address -> password
        self.domain = {"name": "example.org", "symbolicSubaddressing": False,
                       "dnsSummary": {"passesMx": True, "passesSpf": True, "passesDkim": True, "passesDmarc": True}}
        self.calls = []   # (method, body)
        # method -> the error message it answers instead, or an OSError it raises once it did its work (the
        # answer got lost)
        self.broken = {}
        self.blocked = set()   # (host, port) this server cannot reach

    def connect(self, address, timeout):
        if address in self.blocked:
            raise TimeoutError("timed out")
        return io.BytesIO()

    def urlopen(self, request, timeout):
        assert request.full_url.startswith("https://purelymail.com/api/v0/")
        assert request.get_header("Purelymail-api-token") == TOKEN   # as urllib spells the header it sends
        method, body = request.full_url.rsplit("/", 1)[1], json.loads(request.data)
        self.calls.append((method, body))
        failure = self.broken.pop(method, None)
        if isinstance(failure, str):
            answer = {"type": "error", "code": "internalError", "message": failure}
        else:
            try:
                answer = {"type": "success", "result": self.answer(method, body)}
            except LookupError as e:
                answer = {"type": "error", "code": "internalError", "message": e.args[0]}
            if failure:
                raise failure
        return io.BytesIO(json.dumps(answer).encode())

    def answer(self, method, body):
        if method == "listDomains":
            return {"domains": [self.domain]}
        if method == "listUser":
            return {"users": list(self.users)}
        if method == "createUser":
            address = f"{body['userName']}@{body['domainName']}"
            if address in self.users:
                raise LookupError("User already exists")
            self.users[address] = body["password"]
        elif method == "modifyUser":
            if body["userName"] not in self.users:
                raise LookupError(f"Unknown user {body['userName']}")
            self.users[body["userName"]] = body["newPassword"]
        elif method == "deleteUser":
            if self.users.pop(body["userName"], None) is None:
                raise LookupError(f"Unknown user {body['userName']}")
        else:
            raise AssertionError(method)
        return {}

    def methods(self):
        return [method for method, _ in self.calls]


@pytest.fixture
def purelymail(monkeypatch):
    """The fake Purelymail, whose account's token is in secrets.env."""
    settings.SECRETS_ENV.write_text(f"THINNY_PURELYMAIL_API_TOKEN={TOKEN}\n")
    fake = Purelymail()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setattr(socket, "create_connection", fake.connect)
    return fake


@pytest.fixture
def alice(incus, gateways):
    """The registry with alice, active, with her Webapp sign-in in her .env."""
    reg = Registry()
    reg.add("alice", "local", (9120, 9199)).state = "active"
    incus.containers["alice"] = {"state": "RUNNING", "devices": {}, "account": True, "hermes": True, "unit": True,
                                 "files": {ENV: "HERMES_WEBAPP_AUTH_USERNAME=alice\n"}}
    return reg


def env(incus):
    """alice's .env, its values unquoted."""
    lines = incus.containers["alice"]["files"][ENV].splitlines()
    return {key: json.loads(value) if value.startswith('"') else value
            for key, _, value in (line.partition("=") for line in lines)}


def test_an_instance_gets_a_mailbox_that_only_named_addresses_may_write_to(alice, incus, gateways, purelymail,
                                                                          capsys):
    mail.connect(SITE, alice, "alice", ["anna@example.com", "Boss@Example.com"])
    password = purelymail.users["alice@example.org"]
    assert purelymail.calls[-1] == ("createUser", {"userName": "alice", "domainName": "example.org",
                                                   "password": password, "enablePasswordReset": False,
                                                   "sendWelcomeEmail": False})
    assert len(password) >= 24
    assert env(incus) == {"HERMES_WEBAPP_AUTH_USERNAME": "alice", "EMAIL_ADDRESS": "alice@example.org",
                          "EMAIL_PASSWORD": password, "EMAIL_IMAP_HOST": "imap.purelymail.com",
                          "EMAIL_SMTP_HOST": "smtp.purelymail.com", "EMAIL_AUTHSERV_ID": "purelymail.com",
                          "EMAIL_ALLOWED_USERS": "anna@example.com,Boss@Example.com",
                          "EMAIL_HOME_ADDRESS": "anna@example.com"}
    assert alice.get("alice").mailbox == "alice@example.org"
    restarted, _ = gateways
    assert restarted == ["alice"]
    assert not any(TOKEN in call or password in call for call in incus.calls)
    assert "alice reads and sends mail as alice@example.org. anna@example.com, Boss@Example.com may write to it." \
        in capsys.readouterr().out


@pytest.mark.parametrize("arch, sha", list(instances.HIMALAYA_SHA256.items()))
def test_a_mailbox_comes_with_the_email_client_of_hermess_himalaya_skill(alice, incus, purelymail, arch, sha):
    incus.containers["alice"]["arch"] = arch
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    # the pinned release for its architecture, checked before it is unpacked
    url = f"https://github.com/pimalaya/himalaya/releases/download/{instances.HIMALAYA}/himalaya.{arch}-linux.tgz"
    assert incus.ran(f"{url} {sha} /usr/local/bin/himalaya")
    assert incus.containers["alice"]["himalaya"] == instances.HIMALAYA

    incus.calls.clear()
    mail.connect(SITE, alice, "alice", [])
    assert not incus.ran("sha256sum")


def test_himalayas_account_for_the_mailbox_reads_the_password_from_the_env_at_every_use(alice, incus, purelymail,
                                                                                      tmp_path):
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    account = tomllib.loads(incus.containers["alice"]["files"][HIMALAYA_CONFIG])["accounts"]["alice@example.org"]
    cmd = account["backend"]["auth"].pop("cmd")
    assert account == {
        "default": True, "email": "alice@example.org",
        "backend": {"type": "imap", "host": "imap.purelymail.com", "port": 993, "encryption": {"type": "tls"},
                    "login": "alice@example.org", "auth": {"type": "password"}},
        "message": {"send": {"backend": {"type": "smtp", "host": "smtp.purelymail.com", "port": 587,
                                         "encryption": {"type": "start-tls"}, "login": "alice@example.org",
                                         "auth": {"type": "password", "cmd": cmd}},
                             "save-copy": False}}}

    def password():   # as himalaya gets it
        (tmp_path / ".env").write_text(incus.containers["alice"]["files"][ENV])
        return subprocess.run(["sh", "-c", cmd.replace(ENV, str(tmp_path / ".env"))], capture_output=True,
                              text=True, check=True).stdout.strip()
    assert password() == purelymail.users["alice@example.org"]
    mail.connect(SITE, alice, "alice", [], new_password=True)
    assert password() == purelymail.users["alice@example.org"]
    typed = 'it\'s "pa$$" #word\\'   # as an admin may type their own mailbox's
    instances.set_env("alice", {"EMAIL_PASSWORD": typed})
    assert password() == typed


def test_a_users_own_himalaya_accounts_stay(alice, incus, purelymail):
    own = '[accounts.gmail]\nemail = "anna@gmail.com"\ndefault = true\nbackend.type = "none"'
    incus.containers["alice"]["files"][HIMALAYA_CONFIG] = own
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    config = incus.containers["alice"]["files"][HIMALAYA_CONFIG]
    accounts = tomllib.loads(config)["accounts"]
    assert config.startswith(own) and accounts["gmail"]["default"] and "default" not in accounts["alice@example.org"]

    mail.connect(SITE, alice, "alice", [])
    assert incus.containers["alice"]["files"][HIMALAYA_CONFIG] == config


@pytest.mark.parametrize("own", ["[accounts", 'accounts = { gmail = { email = "anna@gmail.com" } }'])
def test_a_himalaya_config_thinny_cannot_add_its_account_to_stays_as_it_is(alice, incus, purelymail, capsys, own):
    incus.containers["alice"]["files"][HIMALAYA_CONFIG] = own
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    assert incus.containers["alice"]["files"][HIMALAYA_CONFIG] == own
    assert f"alice: {HIMALAYA_CONFIG} gets no account for alice@example.org, which its agent sends mail with: " \
        in capsys.readouterr().err


def test_the_agent_learns_how_to_send_mail_from_the_mailbox_before_its_gateway_restarts(alice, incus, gateways,
                                                                                       purelymail, monkeypatch):
    hints = []
    monkeypatch.setattr(instances, "_gateway_up", lambda s, inst: hints.append(policy_hint()))
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    hint = render.mail_hint("alice@example.org", instances.HIMALAYA_BIN)
    assert hints == [f"{render.SERVER_HINT}\n\n{hint}"]
    assert "/usr/local/bin/himalaya template send -a alice@example.org" in hint
    assert "EMAIL_HOME_ADDRESS" in hint and "--preview" in hint


def policy_hint():
    """What alice's policy adds to her agent's prompt."""
    return yaml.safe_load((host.POLICIES / "alice/config.yaml").read_text()).get("agent", {}).get("environment_hint")


def test_an_older_himalaya_gets_the_pinned_one(alice, incus, purelymail):
    incus.containers["alice"]["himalaya"] = "v1.1.0"
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    assert incus.containers["alice"]["himalaya"] == instances.HIMALAYA


def test_without_a_himalaya_for_its_architecture_the_mailbox_waits(alice, incus, gateways, purelymail):
    incus.containers["alice"]["arch"] = "riscv64"
    with pytest.raises(CommandError, match="alice: himalaya has no release for riscv64"):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    restarted, _ = gateways
    assert restarted == []


def test_running_it_again_changes_who_may_write_and_keeps_the_password(alice, incus, gateways, purelymail):
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    password = env(incus)["EMAIL_PASSWORD"]
    mail.connect(SITE, alice, "alice", ["ben@example.com", "anna@example.com"])
    assert (env(incus)["EMAIL_ALLOWED_USERS"], env(incus)["EMAIL_HOME_ADDRESS"]) == \
        ("ben@example.com,anna@example.com", "ben@example.com")
    mail.connect(SITE, alice, "alice", [])   # keeps them
    assert env(incus)["EMAIL_ALLOWED_USERS"] == "ben@example.com,anna@example.com"
    assert env(incus)["EMAIL_PASSWORD"] == purelymail.users["alice@example.org"] == password
    assert purelymail.methods().count("createUser") == 1
    restarted, _ = gateways
    assert restarted == ["alice"] * 3


def test_a_new_password_replaces_the_old_one_in_purelymail_and_the_instance(alice, incus, purelymail):
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    old = env(incus)["EMAIL_PASSWORD"]
    mail.connect(SITE, alice, "alice", [], new_password=True)
    assert env(incus)["EMAIL_PASSWORD"] == purelymail.users["alice@example.org"] != old
    assert env(incus)["EMAIL_ALLOWED_USERS"] == "anna@example.com"


@pytest.mark.parametrize("method", ["createUser", "modifyUser"])
def test_running_it_again_finishes_a_run_that_purelymail_refused(alice, incus, gateways, purelymail, method):
    if method == "modifyUser":
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    purelymail.broken[method] = "Service unavailable"
    with pytest.raises(CommandError, match=f"Purelymail refused {method}: Service unavailable"):
        mail.connect(SITE, alice, "alice", ["anna@example.com"], new_password=True)
    # thinny's only once created: a refusal may mean someone else created it meanwhile
    assert alice.get("alice").mailbox == ("alice@example.org" if method == "modifyUser" else "")
    assert instances.RESTART_PENDING.read_text() == "alice\n"   # its .env changed

    mail.connect(SITE, alice, "alice", [])
    assert purelymail.users["alice@example.org"] == env(incus)["EMAIL_PASSWORD"]
    assert alice.get("alice").mailbox == "alice@example.org"
    assert not instances.RESTART_PENDING.exists()


def test_a_mailbox_created_without_an_answer_is_left_to_the_admin(alice, purelymail):
    purelymail.broken["createUser"] = TimeoutError("timed out")   # created, but the answer got lost
    with pytest.raises(CommandError, match="Purelymail did not answer createUser: timed out"):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    created = purelymail.users["alice@example.org"]
    with pytest.raises(CommandError, match="if an earlier run stopped without Purelymail's answer, delete it there"):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    assert purelymail.users["alice@example.org"] == created and alice.get("alice").mailbox == ""


def test_other_settings_in_the_env_stay_as_they_are(alice, incus, purelymail):
    incus.containers["alice"]["files"][ENV] += \
        'OPENAI_API_KEY="sk-1" # mine\nNOTE=\'single\'\nEMAIL_ADDRESS=alice@example.org\n'
    mail.connect(SITE, alice, "alice", ["anna@example.com"])
    text = incus.containers["alice"]["files"][ENV]
    assert f'EMAIL_PASSWORD="{purelymail.users["alice@example.org"]}"' in text   # none before: a new one
    assert 'OPENAI_API_KEY="sk-1" # mine\nNOTE=\'single\'\n' in text


def test_a_mailbox_thinny_did_not_create_is_never_taken_over(alice, incus, gateways, purelymail):
    purelymail.users["alice@example.org"] = "its owner's"
    with pytest.raises(CommandError, match="alice@example.org exists in Purelymail, but thinny did not create it"):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    assert purelymail.users["alice@example.org"] == "its owner's"
    assert "EMAIL" not in incus.containers["alice"]["files"][ENV]
    assert alice.get("alice").mailbox == ""


@pytest.mark.parametrize("senders, message", [
    ([], "name who may write to alice"),
    (["*"], "'\\*' is not an address"),   # anyone: the agent runs commands
    (["anna"], "'anna' is not an address"),
    (["anna@example.com,eve@example.com"], "is not an address"),
])
def test_only_whole_addresses_may_write(alice, purelymail, senders, message):
    with pytest.raises(CommandError, match=message):
        mail.connect(SITE, alice, "alice", senders)
    assert purelymail.calls == []


@pytest.mark.parametrize("name", ["postmaster", "abuse", "admin"])
def test_role_addresses_are_no_instances_mailboxes(purelymail, name):
    reg = Registry()
    reg.add(name, "local", (9120, 9199)).state = "active"
    with pytest.raises(CommandError, match=f"{name}@example.org is .*reserved"):
        mail.connect(SITE, reg, name, ["anna@example.com"])
    assert purelymail.calls == []


@pytest.mark.parametrize("change, message", [
    ({"name": "other.org"}, "add example.org in Purelymail first"),
    ({"dnsSummary": {"passesMx": True, "passesSpf": True, "passesDkim": False, "passesDmarc": False}},
     "example.org's DNS records do not pass Purelymail's checks: DKIM, DMARC"),
    ({"symbolicSubaddressing": True}, "turn off symbolic subaddressing for example.org"),
])
def test_a_mailbox_needs_its_domain_set_up_in_purelymail(alice, purelymail, change, message):
    purelymail.domain |= change
    with pytest.raises(CommandError, match=message):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    assert "createUser" not in purelymail.methods()


def test_a_mailbox_needs_the_sites_domain_and_purelymail_account(alice, purelymail):
    with pytest.raises(CommandError, match="set email.domain first: thinny config edit \\(see README, Email\\)"):
        mail.connect(S, alice, "alice", ["anna@example.com"])
    settings.SECRETS_ENV.unlink()
    with pytest.raises(CommandError, match=NO_TOKEN):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    assert purelymail.calls == []


def test_a_disabled_instance_gets_no_mailbox(alice, purelymail):
    alice.get("alice").state = "disabled"
    with pytest.raises(RegistryError, match="alice is disabled"):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
    assert purelymail.calls == []


@pytest.fixture
def disabled(incus, typed):
    """The registry with alice disabled and her mailbox; the admin types her name to remove her."""
    incus.containers["alice"] = {"state": "STOPPED"}
    typed("alice")
    return Registry({"alice": Instance("alice", 9120, "local", state="disabled", mailbox="alice@example.org")})


def test_removing_an_instance_deletes_its_mailbox_first(disabled, incus, purelymail, monkeypatch, capsys):
    purelymail.users = {"alice@example.org": "x", "bob@example.org": "y"}
    saved = []
    monkeypatch.setattr(Registry, "save", lambda self, path=None: saved.append(
        (self.get("alice").mailbox, "alice" in incus.containers)))
    instances.remove(SITE, disabled, "alice")
    assert "This deletes its mailbox alice@example.org too, with all its mail." in capsys.readouterr().out
    assert purelymail.users == {"bob@example.org": "y"}
    # forgotten at once: a mailbox created at its address later is not thinny's to delete
    assert saved[0] == ("", True)
    assert "alice" not in incus.containers and disabled.get("alice").state == "removed"


@pytest.mark.parametrize("failure", ["Service unavailable", TimeoutError("timed out")])   # the second deletes it
def test_a_remove_that_purelymail_fails_deletes_nothing_and_runs_again(disabled, incus, purelymail, failure):
    purelymail.users = {"alice@example.org": "x"}
    purelymail.broken["deleteUser"] = failure
    with pytest.raises(CommandError, match="Purelymail .* deleteUser"):
        instances.remove(SITE, disabled, "alice")
    assert "alice" in incus.containers and disabled.get("alice").mailbox == "alice@example.org"

    instances.remove(SITE, disabled, "alice")
    assert purelymail.users == {} and "alice" not in incus.containers


def test_a_remove_can_leave_the_mailbox_to_another_server(disabled, incus, purelymail, capsys):
    purelymail.users = {"alice@example.org": "x"}
    settings.SECRETS_ENV.unlink()
    instances.remove(S, disabled, "alice", keep_mailbox=True)   # without the site's Purelymail account too
    assert "Its mailbox alice@example.org stays in Purelymail." in capsys.readouterr().out
    assert purelymail.users == {"alice@example.org": "x"} and purelymail.calls == []
    assert "alice" not in incus.containers and disabled.get("alice").mailbox == ""


def test_a_mailbox_needs_the_sites_purelymail_account_to_go(disabled, incus, purelymail):
    settings.SECRETS_ENV.unlink()
    with pytest.raises(CommandError, match=NO_TOKEN):
        instances.remove(S, disabled, "alice")
    assert "alice" in incus.containers and purelymail.calls == []


def test_doctor_checks_purelymail_only_on_a_site_with_mailboxes(purelymail):
    assert list(checks.mailboxes(S, Registry())) == []
    assert list(checks.mailboxes(SITE, Registry())) == [("OK", "Purelymail takes mail for example.org", "")]
    purelymail.domain["symbolicSubaddressing"] = True
    assert list(checks.mailboxes(SITE, Registry())) == [
        ("WARN", "mailboxes: turn off symbolic subaddressing for example.org in Purelymail (see README, Email)", "")]
    settings.SECRETS_ENV.unlink()
    assert list(checks.mailboxes(SITE, Registry())) == [
        ("WARN", "mailboxes: set THINNY_PURELYMAIL_API_TOKEN first: thinny config edit --secrets (see README, Email)",
         "")]


def test_doctor_warns_when_this_server_cannot_reach_purelymails_mail_servers(purelymail):
    purelymail.blocked.add(("smtp.purelymail.com", 587))   # as netcup's default firewall policy does
    assert list(checks.mailboxes(SITE, Registry())) == [
        ("WARN", "this server cannot reach smtp.purelymail.com:587: its instances' mail neither goes nor comes",
         "let outgoing TCP to port 587 through, also at your hosting provider (see README, Email)"),
        ("OK", "Purelymail takes mail for example.org", "")]


def test_doctor_finds_a_mailbox_gone_from_purelymail(purelymail):
    reg = Registry({"alice": Instance("alice", 9120, "local", state="active", mailbox="alice@example.org"),
                    "bob": Instance("bob", 9121, "local", state="active", mailbox="bob@example.org")})
    purelymail.users = {"bob@example.org": "x"}
    assert list(checks.mailboxes(SITE, reg))[1:] == [
        ("WARN", "alice's mailbox alice@example.org is gone from Purelymail", "thinny instance email alice")]


def test_purelymails_errors_come_with_http_200(alice, purelymail):
    purelymail.broken["createUser"] = "Password is too weak"
    with pytest.raises(CommandError, match="^Purelymail refused createUser: Password is too weak$"):
        mail.connect(SITE, alice, "alice", ["anna@example.com"])
