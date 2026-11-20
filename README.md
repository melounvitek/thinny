# thinny

Turns a Debian 13 or Ubuntu server into a shared [Hermes Agent](https://github.com/NousResearch/hermes-agent)
server for a small organisation. Each instance is an [Incus](https://linuxcontainers.org/incus/)
system container with its own Hermes and a password-protected Webapp, or
[hermes-webui](https://github.com/nesquena/hermes-webui) in its place. All instances share one
central policy and one Hermes release.

Status: in development. Design notes: [`NOTES.md`](NOTES.md).

- **Isolated instances:** one container each (Debian 13 with systemd), with CPU, memory,
  process and disk limits. A firewall lets it reach the internet but not mail servers on port 25,
  and nothing else. Its users have `sudo` in it.
- **One central policy** from `config.toml`, mounted read-only as each container's `/etc/hermes`
  (Hermes "managed scope"): approvals, the agent kept in its container, Hermes not borrowing
  Claude Code's or Codex CLI's sign-ins on its own (it could log them out), and no messages to
  users on Telegram, email or the phone whenever Hermes restarts, such as at the nightly update or
  a reboot. It also tells the agent that it runs on a server its users reach from their own
  devices, so it does not offer to open things in their browser. The optional **model
  lock** (`policy.model_lock`) also pins the provider, model, reasoning effort and plugins, and
  allows only `allowed_models`. Without it, users choose their own. One instance can have its own
  lock, or none, whatever the server's (`thinny instance lock` and `unlock`).
- **Groups** of instances share Hermes settings their users cannot change, and take commands
  together.
- **Built into every instance:** [Claude Code](https://code.claude.com/) with the Claude
  subscription plugin, and the [hermes-3310](https://github.com/melounvitek/hermes-3310) phone
  plugin with Docker.
- **Telegram with one link:** each instance's user creates a bot of their own that only they can
  write to (optional, see [Telegram](#telegram)).
- **A mailbox per instance**, `alice@<your domain>` at Purelymail, that the people you name can email
  and get replies from (optional, see [Email](#email)).
- **One Hermes release**, with an optional nightly update that moves instances one at a time.
- **Webapp or WebUI per instance:** Hermes's built-in Webapp, or hermes-webui, with the same
  password. WebUI follows its stable releases like Hermes.
- **Four ways to open the Webapp:** office network (HTTPS), Tailscale, your own address, or an
  SSH tunnel.
- **`thinny status`** for a quick look, **`thinny doctor`** for every isolation and health
  check, each with a fix.
- **An optional dashboard** with the history of the server's and each instance's CPU, memory,
  disk and network, and if you choose, of its users' subscription usage, turns and tokens (see
  [Dashboard](#dashboard)).

## Security

**Protected:** instances from each other. Each container has its own uid range, filesystem,
network namespace and processes. It cannot reach other containers, the host's services,
private networks or the tailnet, and cannot change the shared policy. It cannot send mail straight
to mail servers on port 25 either, so spam sent that way cannot get the server's address blocked.

**Not protected:**

- anything from the server admin or the model provider. The [access trace](#access-trace) shows
  when the admin enters a container;
- the policy from an instance's users: they are root in their container, so the policy is a
  guardrail. `doctor` reports the bypasses it can see;
- users from their own agent: it can do whatever they can in the container, but cannot leave it;
- a little of the server, which a shared kernel shows every container: an instance sees its number of
  CPUs, the size and use of its disk (through the folders thinny mounts into it), and a few kernel
  counters that hint at how busy it is. The other instances' disks (their names, sizes and activity)
  and the server's memory stay hidden;
- an instance's sign-in from other instances' users, with `lan` or `tailscale` access. Those
  instances share one address, and browsers send a site's cookies to every port of it: when
  Alice opens Bob's address in the browser she is signed in with, Bob's container gets her
  sign-in, and Bob is root there. Where users do not trust each other, give each instance an
  address of its own (`url`, see [Access](#access)).

## Requirements

- Debian 13, Ubuntu 26.04 or Ubuntu 24.04, with SSH by key, as root or as a user with sudo.
- Internet access for GitHub, the Hermes and Claude Code installers, `images.linuxcontainers.org`,
  Debian mirrors and the model provider.
- About 0.4 GB RAM and 3 GB disk per idle instance, plus what its agent, Docker and Claude Code
  use. WebUI takes about 0.3 GB RAM more, the optional dashboard about 0.3 GB RAM and 2.5 GB disk.

With a sudo account, thinny runs itself with sudo, which asks for your password. On Debian, whose
`PATH` has `/usr/local/sbin` only for root, type `sudo thinny`. Prefix other commands with `sudo`
(`sudo incus …`).

## Prerequisites

`thinny doctor` checks these and prints the fix.

**Key-only SSH**, reachable only from a private network. Create
`/etc/ssh/sshd_config.d/10-keys-only.conf`, then run `systemctl reload ssh`:

```
PasswordAuthentication no
KbdInteractiveAuthentication no
```

**A firewall** whose input chain drops by default, and whose forward chain lets the containers
through (thinny filters them in its own table, but a drop in any chain is final). With LAN
access, open the Webapp ports (`webapp.port_range`, 9120-9199) and port 80 (the root certificate)
to office PCs, and the [dashboard](#dashboard)'s port when it is on the LAN. Example
`/etc/nftables.conf`, loaded with `nft -f /etc/nftables.conf`:

```
#!/usr/sbin/nft -f
# No "flush ruleset": it would also remove Tailscale's, Incus's and thinny's tables.
table inet filter
delete table inet filter
table inet filter {
	chain input {
		type filter hook input priority filter; policy drop;
		ct state established,related accept
		ct state invalid drop
		iif "lo" accept
		meta l4proto { icmp, ipv6-icmp } accept
		ip saddr 192.168.1.0/24 tcp dport 22 accept         # SSH from the office network
		ip saddr 192.168.1.0/24 tcp dport 9120-9199 accept  # office PCs to the Webapps
		ip saddr 192.168.1.0/24 tcp dport 80 accept         # and to the root certificate
		ip saddr 192.168.1.0/24 tcp dport 9200 accept       # and to the dashboard (dashboard.access = "lan")
	}
	chain forward {
		type filter hook forward priority filter; policy drop;
		iifname "thinny-*" accept                           # the containers (filtered by thinny)
		oifname "thinny-*" ct state established,related accept
	}
}
```

With ufw, run `ufw route allow in on thinny-+` instead of the forward chain.

> **Never run `systemctl restart nftables` while containers run.** It flushes every table. If
> it happens, run `thinny config apply` and `systemctl restart tailscaled`, then reboot at a
> quiet time.

Every minute, thinny checks that its table is loaded and loads it again when something flushed it,
with a line in `/var/log/thinny.log` and the journal. That can take about a minute, and brings back
thinny's table only: Incus's and Tailscale's tables stay gone, so the steps above still apply.

**Unattended upgrades, with automatic reboots.** The containers share the server's kernel, so a
fixed kernel protects them only once the server reboots into it. Schedule the reboot well after
the nightly Hermes update (`hermes.update_time`, 03:00), which can take an hour with many
instances. `thinny doctor` warns when automatic reboots are off and while a reboot is pending:

```sh
apt install unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

```
# /etc/apt/apt.conf.d/52unattended-upgrades-local
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "05:00";
```

## Install

As root or as a user with sudo, with `curl`:

```sh
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/melounvitek/thinny/main/bin/install | bash'
```

<details>
<summary>What it does</summary>

1. Downloads and runs thinny's installer from the `main` branch. Without root, it runs the next
   steps with sudo.
2. On the first run, lists what it changes on the machine and asks whether to continue.
3. Installs Git with apt if it is missing.
4. Clones thinny to `/usr/local/src/thinny`. If a clone is already there, it uses it as it is.
5. Runs its `install.sh`, which installs the packages (Incus, btrfs-progs, nftables, curl, Git,
   Python 3, bash-completion), copies thinny to `/usr/local/lib/thinny`, links the `thinny`
   command to `/usr/local/sbin/thinny` and adds its Tab completion for bash.
6. On the first run, `thinny init` asks for the settings below and writes them into
   `/etc/thinny/config.toml`, a copy of the commented example. Without a terminal, it copies the
   example and stops.
7. Once `config.toml` exists, it runs `thinny setup`: the Incus profile, the firewall and its
   check, the update timer and the shared policy.

It does not add any instances.

</details>

On the first run, the installer asks for the Webapp language, the internet interface and
how users will open the Webapps. It
suggests the interface and LAN address it detects and the language pack it finds in Hermes's
catalog, shows a summary, and sets up the server once you confirm. Everything else keeps the
example's defaults: change them with `thinny config edit`.

Without a terminal, it stops after copying the example. Edit `/etc/thinny/config.toml` (at least
`network.interface` and `access.lan_address`, or `""` and another
`access.default`), then run `sudo sh /usr/local/src/thinny/install.sh`.

Rerunning `install.sh` is safe. `thinny update` updates thinny from its clone (see [Updates](#updates)).

In bash, Tab completes thinny's commands and options. Instance names complete only in a root
shell (`sudo -i`): elsewhere, the completion runs as you, and only root reads the instances.
A changed model guard loads at each gateway's next restart (`thinny instance restart`).

## Settings

Every key of `/etc/thinny/config.toml` is commented in [`config.example.toml`](config.example.toml),
also on the server at `/usr/local/lib/thinny/config.example.toml`. Settings that a later thinny adds
appear there, not in your `config.toml`: thinny uses their defaults until you add them.

```sh
thinny config edit    # edit a copy, check it, then apply it
thinny config apply   # apply after editing the file another way
```

Without root, `thinny config edit` keeps your `VISUAL` or `EDITOR` through sudo; `sudo thinny config
edit` drops them. Avoid `sudo -E`: it also keeps your `HOME`, where Incus then leaves root-owned files.

Applying restarts gateways one by one where needed. `policy.language`, `port_range` and the
`[webapp]` look apply only to instances added afterwards; `webapp.frontend` is what `instance add`
suggests. A new `language_pack` reaches existing
instances with their next Hermes update. `model_provider` and `model` count only with the model
lock, which needs `model`. Without it, `model_provider` is only the provider `instance add`
suggests, `model` is ignored, and `reasoning_effort` is where new instances start; turning the
lock on removes the models users chose, and turning it off leaves each locked instance on the
lock's model, for its users to change (as `instance unlock` does).

Tokens are not in `config.toml`, so you can show and share it. They are in `/etc/thinny/secrets.env`,
which only root can read. Never show or share it, not even in part.

```sh
thinny config edit --secrets   # edit a copy, check it, then save it (nothing to apply)
```

It takes one `NAME=value` per line, without quotes: `THINNY_TELEGRAM_MANAGER_TOKEN` (see
[Telegram](#telegram)) and `THINNY_PURELYMAIL_API_TOKEN` (see [Email](#email)). Commands read it when
they need a token. `sudo grep -o '^[A-Z_]*=' /etc/thinny/secrets.env` lists the names set without their
tokens, and `thinny doctor` warns when others than root can read the file.

For one run, an environment variable of the same name wins, e.g. to try a token before saving it. In a
root shell, or through sudo by name:

```sh
read -rs THINNY_PURELYMAIL_API_TOKEN && export THINNY_PURELYMAIL_API_TOKEN   # not shown, not in the history
sudo --preserve-env=THINNY_PURELYMAIL_API_TOKEN thinny doctor
```

## Instances

```sh
thinny instance add alice               # about 7 minutes; asks how users open it, Webapp or WebUI, the model provider and a phone line
thinny instance login alice             # sign in to a model provider; asks which
thinny instance login alice --provider anthropic   # or name it: openai | anthropic
thinny instance telegram alice          # a Telegram bot its user creates with a link (see Telegram)
thinny instance email alice anna@example.com   # a mailbox alice@<domain> that anna can write to (see Email)
thinny instance list                    # with each one's CPU, RAM and disk use, mailbox and phone line
thinny instance usage [alice]           # its users' share of their 5-hour and weekly subscription limits, turns and tokens
thinny instance access alice tailscale  # lan | tailscale | url=<URL> | local
thinny instance frontend alice webui    # webapp | webui
thinny instance resize alice --cpu 4 --memory-mb 8192 --disk-gb 50
thinny instance model alice             # the model its new chats start on, and the models it can use
thinny instance model alice gpt-5.5     # new chats start on it; --provider openai | anthropic: of another provider
thinny instance lock alice [model ...]  # only these models (default: the one it uses)
thinny instance unlock alice            # its users choose, even with the server's lock
thinny instance reset-password alice
thinny instance reset-password alice --set   # to one you type in, not a random one
thinny instance export alice            # its chats, memories and skills for its users, without sign-ins
thinny instance disable alice           # stop it for good, keep its data
thinny instance remove alice            # then delete its container and data; asks you to type its name
thinny instance remove alice --purge    # also free its name and port, even once removed
thinny instance restart [alice ...]
thinny instance move alice bob --to admin@new-server   # or --all; experimental
thinny instance restart --group sales   # instead of names: resize, login, model, lock, unlock and move too (see Groups)
```

- `add` prints the address, the username (Webapp only), the password and the model it starts on.
  **The password is shown only once.** If `add` fails, fix the cause and run it again: it continues where it stopped.
- **WebUI** ([hermes-webui](https://github.com/nesquena/hermes-webui), `--frontend webui`) runs on
  the instance's port in place of the Webapp; the gateway keeps running for scheduled jobs and the
  phone. It starts in `policy.language` and `webapp.theme`, with Graphite colours, only
  Chat and Todos in the sidebar and a plain message box. Its chat list shows chats from Telegram,
  email and the like too, under CLI; then it keeps its users' settings. A
  browser shows every sidebar tab until it has opened WebUI's Settings once (WebUI v0.52.678).
  `frontend` switches an instance either way; one added before thinny had WebUI gets a new
  password when it first switches (the Webapp keeps only a hash of it).
- WebUI takes the password in plain text, in `~/.config/hermes-webui.env` in the container. The
  agent runs inside WebUI and can read it from its environment, so it could end up in a chat, and
  at the model provider. `reset-password` signs everyone out of WebUI, but its public share links
  stay.
- WebUI's model picker ignores the model lock and lists every model; the model guard refuses
  chats with the others, but not WebUI's chat titles.
- Run `login` before the hand-over. Enter takes the provider `alice` uses; with the model lock,
  `login` signs in to the lock's provider without asking. Whoever uses `alice` opens the link and
  signs in with their own account within about 15 minutes. Signed in to another provider, they
  choose its models in the Webapp's model picker (Anthropic: *Claude Subscription DirectSDK
  (Experimental)*). thinny offers OpenAI and Anthropic only; users set up others in Hermes.
- `usage` shows admins how much of their ChatGPT or Claude subscription the users of each active
  instance (or of `alice`) used: the share of the 5-hour and the weekly limit, and when each
  resets, in the server's time, with the email of the account they signed in with. Hermes asks
  the provider afresh (`hermes usage`; for Claude, Claude Code answers without using any of the
  limits), for every instance at once, up to about 20 s each. `?` means no answer. The
  [dashboard](#dashboard) can keep its history.
  It also shows how much they used their agent in the last 7 days, to tell who uses most of a
  subscription that several instances share: their turns (messages to it) and its tokens (input,
  output and cache writes, without the cache reads, which count the least against a limit). They
  come from Hermes's chats in the instance, so not what the users do in Claude Code, ChatGPT or
  Claude.ai. Hermes counts a chat's tokens, not each reply's, so thinny shares them out by its
  replies: a close estimate, not the provider's count.
- `add` can connect a phone line to the phone plugin. Answer `y` and it asks for the SIP account
  (Odorik's server by default), the PIN, the numbers that may call, the number Hermes calls back
  and an OpenAI API key for speech recognition, then starts Asterisk in the container. With
  `policy.language = "cs"` the phone speaks Czech. Every new instance gets a faster Piper voice
  (`tts.piper.length_scale` 0.65).
- On a call, a command that needs approval is described and answered on the keypad: `1` allows it
  once, `2` refuses it. A group turns that off with `approvals = "deny"` under
  `[plugins.entries.hermes-3310.settings]`.
- If the line does not register, a stateless firewall at the hosting provider (such as a netcup
  firewall policy) may drop the SIP provider's replies: let UDP from it in, from any port to any
  port (Odorik: `81.31.45.0/24`).
- Without a line, the phone plugin stays idle until it is set up in the Webapp (Messaging →
  Hermes 3310), then with `~/.hermes/plugins/hermes-3310/install-asterisk.sh` and a gateway
  restart. See its README.
- `add` takes the same `--cpu`, `--memory-mb` and `--disk-gb`. CPU and memory limits an instance was
  not given follow `[container]` in `config.toml`, also when it changes; its disk gets `disk_gb` when
  it is added. `resize` applies at once, even to a running instance. A disk can grow but not shrink.
- Each instance's disk is a storage pool of its own, a file on the server's disk that grows as the
  instance writes, so `df` in the container shows its size. `add` and `resize` refuse a disk that
  would make the instances' disks together larger than 80% of the server's disk. The storage is
  compressed, and disk use and its size count the compressed size (about half of what the files take).
- `lock` keeps the provider `alice` uses and allows the models given, the first by default; it
  pins effort and plugins as the server's lock does. `unlock` leaves `alice` on the model it was
  locked to, for its users to change. Either stays whatever `policy.model_lock` says. The model
  picker keeps showing every model; the model guard refuses the others.
- `model` sets the model `alice`'s new chats start on, in its own config, at once (no restart); its
  users can still choose others. Hermes must offer it to `alice`, as its model picker does: a model
  of the provider `alice` uses, or with `--provider`, of another one it is signed in to (`login`).
  Open chats keep their model, but Telegram and phone chats take the new one at their next message.
  A browser where a user picked a model in the Webapp keeps that pick for new chats, and an open
  WebUI tab keeps the old model until it is reloaded.
- Without the model lock, a new instance starts on its Hermes's default model for its provider
  (ChatGPT: the first of Hermes's list, such as `gpt-6-sol`; Claude, for which Hermes has none:
  the Claude plugin's first model); `model` changes it. On a locked instance (the server's lock,
  or its own `lock`), the lock decides where new chats start, so `model` refuses it: change its
  lock with `lock`, or `unlock` it first.
- Names are 2-32 lowercase letters, digits and `-`, starting with a letter. Names and ports are
  not reused, and a disabled instance cannot be re-enabled.
- `remove` takes a disabled instance, or a pending one whose `add` you give up on. Its name
  stays taken and its [access trace](#access-trace) stays on the server.
- `remove --purge` frees the name and port of a removed instance too, for a new one. Old
  bookmarks and saved passwords for its address then lead to the new instance. Its access trace
  stays, renamed to `alice.purged-<time>`; rows still waiting for its Google Sheet are dropped.
- `export` saves `alice`'s Hermes data for its users, e.g. when someone leaves: their chats, memories,
  skills, settings and scheduled jobs, as its Hermes backs them up (`hermes backup`). It lands in
  `/var/lib/thinny/exports/alice-<time>.zip`, which only root can read. They load it into a Hermes of
  their own with `hermes import alice-<time>.zip`, and sign in there again.
- It leaves out the files that hold sign-ins or passwords: those Hermes counts as credentials (`.env`,
  `auth.json`, MCP servers' and other services' tokens, its credential vault, the Webapp's sign-in),
  WebUI's keys and sign-in sessions, and the phone line's Asterisk config. Some belong to you (the
  mailbox, the phone line, the OpenAI key). Chats and settings can still hold secrets users typed in:
  hand it over as you would a password. Copy it to your account
  (`sudo install -m 600 -o "$USER" /var/lib/thinny/exports/alice-<time>.zip ~/`), then e.g. with `scp`,
  and delete both copies once it is handed over. `remove` leaves it, `uninstall` deletes it.
- Export before `disable`: only an active instance's container runs. Hermes makes the archive in the
  container first, so it needs room there (`resize --disk-gb`). Before that, the server needs the
  size of the instance's disk free on top of a tenth of its disk: the archive takes from the same disk. Its
  copy stops at that size, or before it would leave the server's disk less than 10% free. Files the agent made
  outside Hermes's home (`~/.hermes`) are not in it.
  The [access trace](#access-trace) notes `thinny instance export alice ran its commands in alice`.
- Shell in a container: `sudo incus exec alice -- runuser -l alice` (or `-- bash` for root).
  Both show in the [access trace](#access-trace).
- **`move` is experimental:** so far it has moved one small instance between two servers, once.
  Snapshot both servers first.
- `move` takes whole containers over SSH to another server that runs thinny, one at a time, each
  down for a few minutes. Everything inside comes along: data, sign-ins, the password, Webapp or
  WebUI (at the new server's WebUI release), the model (unless the new server's model lock pins it), language, look and limits; its own
  `lock` or `unlock` does not: it follows the new server's `policy.model_lock`. The new
  server gives it its own port, address, policy and packages: `lan`, `tailscale` and `local`
  instances get a new address; for `url` ones, point your DNS or tunnel at the new server. The
  old copy stays here, disabled.
- Before a move, run `thinny update --all` on both servers: they need the same Hermes release. Each
  needs room for one container archive. The new server takes them only while its instances' disks stay
  within 80% of its own. SSH must reach the new server as root, or as an account
  with sudo without a password. It asks nothing, so connect once as root by hand to accept the
  host key (`sudo ssh admin@new-server true`). sudo hides your SSH agent: thinny uses root's key
  unless you run `sudo --preserve-env=SSH_AUTH_SOCK thinny instance move ...`.
- A moved instance arrives in no [group](#groups).

## Groups

A group's instances get the same Hermes settings, and commands can take them together.

```sh
thinny config edit --group sales        # create or edit /etc/thinny/groups/sales.toml, check it, apply it
thinny instance add alice --group sales
thinny instance group set bob sales     # join or switch; none: leave
thinny instance group list              # groups and their instances
thinny instance list --group sales
thinny instance restart --group sales   # also resize, login, model, lock, unlock and move
```

- A group's file holds Hermes settings, keys of Hermes's `config.yaml` in TOML (see
  [`group.example.toml`](group.example.toml)):

  ```toml
  [display]
  language = "cs"
  ```

- They join each member's policy, so its users cannot change them. thinny's own policy (the
  sandbox, approvals, the model lock) wins over them, and `webapp` is thinny's. A group's
  `agent.environment_hint` follows thinny's own. A list, such as
  `plugins.enabled`, replaces the instance's own.
- Joining a group removes the instance's own values of those settings, and leaving it does not
  bring them back: Hermes's defaults apply.
- Every account in a member's container can read the group's settings, MCP servers' tokens
  included.
- `--group` takes the group's active instances one after another and stops at the first failure.
  With `lock` and `model`, the names are models: `thinny instance model --group sales gpt-5.5`.
  `login` waits for each instance's users in turn.
- To remove a group, move its instances to another group or none, then delete its file. While an
  instance is in a group without a file, `config apply` changes nothing and says so.

## Access

| Method      | Address users open                           | Your part |
| ----------- | -------------------------------------------- | --------- |
| `lan`       | `https://<lan_address>:<port>/`              | trust Caddy's certificate on office PCs |
| `tailscale` | `https://<machine>.<tailnet>.ts.net:<port>/` | the server and users' devices on the tailnet, with MagicDNS and HTTPS certificates |
| `url=<URL>` | your URL, on a host of its own               | a proxy or tunnel on this server, serving HTTPS and forwarding to `http://127.0.0.1:<port>`, or one tunnel for all (below) |
| `local`     | `http://<name>.localhost:<port>/`            | `ssh -L <port>:127.0.0.1:<port> <you>@<server>` |

`lan` and `tailscale` instances share one address, so one's users can get another's sign-in (see
[Security](#security)); `url` and `local` instances each have a host of their own.

Use HTTPS for network addresses: on plain HTTP, browsers turn off the Webapp's clipboard,
microphone, notifications and the Bot Screen. `<name>.localhost` through SSH keeps them.

**LAN certificate:** thinny runs Caddy with its own certificate authority and owns
`/etc/caddy/Caddyfile`. It copies the root certificate to `/etc/thinny/office-root.crt`, readable
by any account. While an instance uses LAN access, Caddy also serves it on the office network at
`http://<lan_address>/office-root.crt`; thinny prints the link when an instance gets LAN access.
Download it on each office PC, install it as trusted and restart the browser:

- Windows: open it, "Install Certificate", "Current User", "Trusted Root Certification Authorities"
  (or for all accounts, as administrator: `certutil -addstore Root office-root.crt`).
- macOS: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain office-root.crt`,
  or in Keychain Access add it to the System keychain, open it and under "Trust" set "When using
  this certificate" to "Always Trust".
- Arch, Fedora: `sudo trust anchor --store office-root.crt`.
- Debian, Ubuntu: `sudo cp office-root.crt /usr/local/share/ca-certificates/` (keep the `.crt`
  extension), then `sudo update-ca-certificates`.
- Browsers that ignore the Linux store need it imported too: Chrome, Chromium and Firefox on
  Debian and Ubuntu (snaps included), and Firefox from Mozilla or Flathub. Chrome:
  `chrome://certificate-manager`, "Local certificates", "Installed by you", "Trusted Certificates",
  "Import". Firefox: in settings, "Certificates", "Manage certificates" (or "View Certificates…"),
  "Authorities", "Import…", "Trust this CA to identify websites". Opening it in GNOME's
  Certificate Viewer doesn't trust it.

Over plain HTTP, someone on the office network could swap the file; where that matters, copy it
with `scp <you>@<server>:/etc/thinny/office-root.crt .` instead.

**One tunnel for every `url` instance:** set `access.tunnel_port` (e.g. `8080`) and run
`thinny config apply`. Caddy then serves every `url` instance at `http://127.0.0.1:8080`, by the
host of its address. Set up the tunnel once, e.g. with Cloudflare:

1. `cloudflared` on this server, with a tunnel route from `*.example.org` to
   `http://127.0.0.1:8080` and a proxied DNS record `*.example.org` pointing to the tunnel. The
   free certificate covers one level: `jirka.example.org`, not `jirka.hermes.example.org`.
2. Cloudflare Access in front, e.g. with an email code.

Then an instance needs only its address:

```sh
thinny instance add jirka --access url=https://jirka.example.org/
```

An address has no path, and no two instances share a host: they would share the browser's origin,
and whoever is root in one could act as the other's signed-in user.

## Telegram

Each instance's user can get a Telegram bot of their own with one link, through a manager bot
(Telegram's managed bots). Set it up once:

1. In Telegram, create the manager bot with [@BotFather](https://t.me/BotFather) (`/newbot`), e.g.
   `@acme_hermes_bot`.
2. In BotFather's app (*Open*), choose the bot, then *Bot Settings*, and turn on *Bot Management Mode*.
3. Put its token in `secrets.env` with `thinny config edit --secrets`:

   ```sh
   THINNY_TELEGRAM_MANAGER_TOKEN=123456:ABC-...
   ```

Then, after `thinny instance login alice`:

```sh
thinny instance telegram alice
```

It prints a link for alice's user, such as
`https://t.me/newbot/acme_hermes_bot/acme_hermes_alice_bot?name=Hermes`, and waits. They open it in
Telegram and tap *Create*. thinny shows who created which bot and asks whether it is alice's; with
`y`, it connects the bot and restarts alice's gateway, and the bot greets its user. Only that user
can write to it. Their chat is also Hermes's Telegram home channel, so Hermes does not ask for one.

- Anyone who knows the manager bot can create a bot through it, also with the suggested name.
  Check the name and Telegram ID before you answer `y`.
- The manager token gets the token of every bot created through it. It stays on the server, in
  `secrets.env`, which only root can read.
- The bot belongs to its user: it is in their BotFather, where they can rename or delete it.
- Ctrl+C stops the wait. A bot created meanwhile waits up to 24 hours: run the command again and
  confirm it. One wait at a time per server; other thinny commands run meanwhile.
- Run it again to give an instance another bot, e.g. when its user deleted theirs. A moved
  instance keeps its bot.

## Email

Each instance can get a mailbox of its own, `alice@<your domain>`, at
[Purelymail](https://purelymail.com/) ($10 a year for any number of mailboxes). The people you name
can email the agent and get its replies, and its scheduled jobs can mail the first of them. Hermes
reads the mailbox and sends from it with its
[email gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/email).

Set it up once:

1. Create a Purelymail account and add your domain. Add the DNS records Purelymail lists for it at
   your DNS provider: the ownership TXT record, MX, SPF, the three DKIM records and DMARC. On
   Cloudflare, keep them *DNS only*, not proxied.
2. In the domain's settings, turn off *symbolic subaddressing*. With it on, Purelymail ignores dots
   and may cut addresses at symbols such as `-`: mail for `office-sales@` could reach `office@`.
3. Set the domain with `thinny config edit`:

   ```toml
   [email]
   domain = "example.org"
   ```

   Then create an API token in Purelymail and put it in `secrets.env` with `thinny config edit --secrets`:

   ```sh
   THINNY_PURELYMAIL_API_TOKEN=...
   ```

The server must reach `smtp.purelymail.com` on port 587 and `imap.purelymail.com` on 993. Some
hosting providers block outgoing mail ports: netcup's default firewall policy *netcup Mail block*
does (ports 25, 465 and 587), so remove it from the server. That opens port 25 too, but thinny's
firewall keeps the instances from it: they send mail through Purelymail. Until Hermes reaches both,
it logs `email failed to connect` and tries again every few minutes.

`thinny doctor` checks that the server reaches both and that the domain passes Purelymail's DNS
checks. Then:

```sh
thinny instance email alice anna@example.com boss@example.com
```

It creates `alice@example.org`, sets alice's Hermes up with it and restarts alice's gateway. Only
`anna@example.com` and `boss@example.com` can write to alice; mail from alice's scheduled jobs goes to
the first.

- Name whole addresses. Hermes ignores mail from anyone else, without an answer. thinny refuses
  `*`: whoever can write to the agent can have it run commands in its container.
- Hermes answers only mail whose sender Purelymail could verify (DMARC, SPF or DKIM), so a forged
  sender gets nothing.
- **Best effort:** Hermes skips mail that arrives while its gateway restarts (a Hermes update,
  `config apply`, `reset-password`, a reboot) or while the instance is disabled. That mail stays
  unread in the mailbox, and its sender has to write again.
- Hermes does not mail the first address about its restarts ("back online", "shutting down"):
  thinny's policy turns that off.
- Its agent can also send mail from the mailbox, attachments too, and read it, when its users ask:
  with Hermes's himalaya skill and [himalaya](https://github.com/pimalaya/himalaya) v1.2.0, which
  thinny installs in the instance. The mailbox is an account named after its address, added to its
  users' own himalaya config, if any. thinny's policy tells the agent so, and where its users' address is. No copy of
  what it sends is kept. Mailboxes from before get it at the next `thinny config apply` or update.
- Run it again to change who can write to it; without addresses it keeps them. `--new-password`
  gives the mailbox a new password. The password is in the instance's `.env`, where its users can
  read it: the mailbox is theirs.
- thinny never takes over a mailbox it did not create, and gives none to role names such as
  `postmaster`, `abuse` or `admin`. If Purelymail created one but its answer never came, thinny
  says so: delete the mailbox in Purelymail and run the command again.
- `remove` deletes the mailbox, with all its mail, before the container. When Purelymail fails,
  nothing is deleted: run it again. `--keep-mailbox` leaves the mailbox in Purelymail. `uninstall`
  deletes every mailbox it can and names the rest.
- The API token manages every mailbox of the Purelymail account. It stays on the server, in
  `secrets.env`, which only root can read. Keep people's mailboxes in another account, or at least
  on another domain.
- `move` takes the mailbox along: the new server deletes it when it removes the instance, with its
  own `[email]` settings (the same Purelymail account). After the move, check that the instance
  still gets and sends mail. When a move fails after the instance may have reached the other
  server, remove the copy here with `--keep-mailbox` if it did.

## Updates

Every night at `hermes.update_time`, thinny first updates itself: it pulls the clone it was
installed from (`git pull --ff-only`) and, on a new commit, runs its `install.sh`, which also
applies `config.toml`. If that fails, the run stops there. A private clone needs read access
without a password, such as a deploy key.

Then it installs the latest stable Hermes release once it is `min_release_age_hours` old, with
the plugins from Hermes's catalog (the Claude plugin, the language pack) at the versions that
release pins. It updates one instance at a time and stops at the first failure; the next run
finishes the job.
Then the same for WebUI's latest stable release, on the instances that use it. WebUI restarts
with every gateway restart, which a new Hermes needs.
With `auto_update = false` the nightly run leaves thinny as it is and only reports a new Hermes
release. The result is
in `/var/lib/thinny/update-status.json`, the log in `/var/log/thinny.log`. Nightly runs
also deliver new built-ins unless `auto_update = false`. Every nightly or `--all` run that gets past the thinny
update, except a dry run, updates Claude Code.

```sh
thinny update                   # thinny only
thinny update --all             # thinny, then Hermes, WebUI and built-ins, even with auto_update = false
thinny update --all --now       # the same, without waiting for min_release_age_hours
thinny update --all --dry-run   # only report what --all would do
```

- **There is no rollback.** Hermes databases cannot be downgraded. Snapshot the server before
  `update_time` (see [`extras/netcup-snapshot`](extras/netcup-snapshot/)).
- A gateway restart waits for a reply in progress, but cuts off scheduled jobs. Keep them away
  from `update_time`.
- The update never reboots the server.

## Status and checks

```sh
thinny status   # releases, last update, containers, gateways
thinny doctor   # every isolation and health check, each with a fix
```

Both exit with status 1 when anything fails, so monitoring can use them. Over SSH, a sudo account
that needs a password cannot run them unattended (`sudo -n thinny doctor` fails). Both only read,
so allow exactly these two without a password, with `sudo visudo -f /etc/sudoers.d/thinny-checks`:

```
<you> ALL=(root) NOPASSWD: /usr/local/sbin/thinny status, /usr/local/sbin/thinny doctor
```

## Dashboard

An optional history, kept for `retention_days`, of each instance's CPU (as a share of its limit), memory
without file cache, disk use against its size, disk reads and writes, network, processes and
out-of-memory kills, and of the server's CPU, memory and disk. It sends no alerts.

```toml
[dashboard]
enabled = true
access = "local"        # local | lan | tailscale
port = 9200             # outside webapp.port_range
retention_days = 30
subscription_usage = false   # true: also each instance's subscription usage, turns and tokens, hourly (see below)
```

Then run `thinny config apply` on a terminal (`thinny config edit` does it too). The first time, it
lists what it installs and asks: Prometheus and its node exporter from the distribution, Grafana from
Grafana's apt repository, about 450 MB to download, 2.2 GB of disk plus the history (about 10 MB a
day), and 0.3 GB of RAM. Without a terminal (`install.sh`, the nightly update) it installs nothing
and says so.

| `access`    | Address users open                           | Your part |
| ----------- | -------------------------------------------- | --------- |
| `local`     | `http://localhost:9200/`                     | `ssh -L 9200:127.0.0.1:9200 <you>@<server>` |
| `lan`       | `https://<lan_address>:9200/`                | office PCs trust Caddy's certificate (see [Access](#access)), and port 9200 is open to them |
| `tailscale` | `https://<machine>.<tailnet>.ts.net:9200/`   | as for instances |

Sign in as `admin` with the first password, in `/etc/thinny/dashboard.env` (`sudo cat` it; only root
reads it). A password changed in Grafana stays in Grafana only.

- **With `lan` or `tailscale`, open it in a browser profile of its own.** Browsers send a site's
  cookies to every port of its address, so a Webapp opened in the same profile gets the dashboard's
  sign-in, and its users are root in their container. Through the SSH tunnel, `localhost` shares
  no cookies with the instances' `<name>.localhost`.
- Everything is read on the server: Incus's metrics, with a certificate that can read nothing
  else, the server's own, and each instance's disk use, which thinny writes every 5 minutes. Only
  the subscription usage (below) runs a command in the containers, which may renew the provider's
  sign-in there, as Hermes does. Nothing else changes in them or their firewall, and they cannot
  reach the dashboard.
  Prometheus, its node exporter, Incus's metrics and Grafana listen on 127.0.0.1 only (9090, 9100,
  8444 and `port`); Caddy or Tailscale publish Grafana.
- Grafana reaches nothing but the server: no usage reports, update checks or plugin downloads.
  Plugins cannot be installed from it, and it has no snapshots or public dashboards.
- Unattended upgrades update Grafana from its repository, new major versions included. Prometheus
  and its node exporter get the distribution's updates (on Ubuntu they are in universe, whose
  security fixes come with Ubuntu Pro). The nightly update renews Grafana's signing key.
- `enabled = false` stops it and takes back its address, Incus's metrics and Grafana's repository.
  The packages (their services masked), the password and the history stay, and turning it on again
  asks nothing.
- `doctor` checks that its services run on 127.0.0.1 only and Prometheus gets every number.

**Subscription usage** (`subscription_usage = true`, off by default): once an hour, thinny runs
`hermes usage --json` in each running instance, as its account, as `thinny instance usage` does.
The dashboard then shows how much of their ChatGPT or Claude subscription's 5-hour and weekly limits
the users of the instance you choose used, and the weekly one of the 10 that used the most. Only
these two shares: no plan, credits, reset times or accounts. Next to them, their turns and tokens
of the last 5 hours and 7 days, as `thinny instance usage` counts them, read from Hermes's chats
in the instance: only how many, nothing of what they say.

- **Tell the instances' users before you turn it on.** Whoever can sign in to the dashboard sees
  their use hour by hour, for `retention_days`. The limits are the whole subscription's: with their
  own account, they include what they do outside Hermes too (ChatGPT, Claude.ai, Codex).
- Every check shows in each instance's [access trace](#access-trace) as `thinny metrics ran its
  commands in alice`: about 24 entries a day, also in its Google Sheet.
- ChatGPT: normally one request for the limits, no model call. Claude: Claude Code starts and asks
  Anthropic, without using any of the limits. Anthropic limits how often a sign-in can read them:
  users report that frequent reads block the readout for hours, also in their own Claude Code. Once
  an hour is far below what status-line tools ask. Each check runs within its instance's CPU and
  memory limits, four instances at a time, up to 20 s each.
- An instance that is stopped or not signed in, or whose check fails, shows a gap. Turns and
  tokens count also while it is not signed in. While thinny's file is over 2 hours old, the
  panels show nothing, and `doctor` warns.
- Turned off, the checks stop within 5 minutes. The history stays until `retention_days` ends.

## Access trace

When someone reaches into an instance's container through Incus, with `incus exec`, `incus shell`,
`incus console` (or its log), `incus file`, a snapshot or an export, thinny notes the time and the
command. thinny's own commands there are noted once per run of thinny, e.g. `thinny doctor ran its
commands in alice` (`doctor` reads the instance's `.env`, among others). With the dashboard's
[subscription usage](#dashboard), `thinny metrics` runs there once an hour.

- **On the server:** `journalctl -t thinny-trace`, and per instance in
  `/var/lib/thinny/trace/<name>/admin-access.log`.
- **Inside the instance:** the same file, read-only, at `/var/log/thinny/admin-access.log`. Its
  users can ask their agent whether anyone came in.
- **Off the server**, out of reach of whoever is root on it: the entries have the syslog tag
  `thinny-trace` and the facility `authpriv`, so they can go wherever your logins go. With
  rsyslog (`apt install rsyslog`), put this in `/etc/rsyslog.d/50-thinny-trace.conf`, then run
  `systemctl restart rsyslog`:

  ```
  if $programname == "thinny-trace" then action(type="omfwd" target="logs.example.org" port="514" protocol="tcp")
  ```

- **In the instance's Google Sheet**, which its users own. Every change to it shows in its
  version history (File → Version history). See below.

**A Google Sheet per instance.** The instance's users create a sheet, open Extensions → Apps
Script, replace the code with this, put in the sheet's ID (the part of its address between `/d/`
and `/edit`), and save:

```js
function doPost(e) {
  const row = JSON.parse(e.postData.contents);
  SpreadsheetApp.openById("SHEET_ID").getSheets()[0].appendRow([row.time, row.instance, row.entry]);
  return ContentService.createTextOutput("ok");
}
```

Then Deploy → New deployment → Web app, with *Execute as* Me and *Who has access* Anyone, and
allow it access to their sheets. They send you the web app URL
(`https://script.google.com/macros/s/…/exec`), and you run:

```sh
thinny instance trace-sheet alice https://script.google.com/macros/s/…/exec
thinny instance trace-sheet alice off   # stop
```

- thinny connects a sheet only once it took a test row. A sheet that stops getting the trace (`off`
  or another URL) gets a last row saying so.
- The URL only lets the server add rows. Keep yourself out of the sheet's editors: whoever can
  edit it can delete rows, and only its version history would show it.
- Rows the sheet cannot take wait on the server, and go again every 5 minutes, oldest first;
  `doctor` warns while they wait. A Google Workspace that allows no access from outside it
  refuses them all.
- A moved instance needs its sheet connected again on the new server.

It is a trace, not a lock: root on the server can stop it or change its files. It does not see
`incus copy` and `incus publish`, which files `incus file` touched, the containers' files on the
server's disk, `nsenter`, what happens in a shell after it opens, a command with the mark of a
running thinny (`env THINNY=<its process>`), or the few seconds while it restarts (e.g. with Incus).
Commands are noted in full, also in a Google Sheet: keep passwords out of them. `doctor` warns
when the trace is not running.

## Uninstall

```sh
thinny uninstall
```

It lists the instances and asks you to type the server's hostname. Then it deletes every
instance's container with all its data, and everything thinny set up: the update timer, the
access trace, `tailscale serve` of the instances' ports, thinny's Caddyfile (Caddy is disabled),
the dashboard's settings, the Incus profile and the instances' storage pools, the firewall table and
its check, `/etc/thinny`, `/etc/hermes`, `/var/lib/thinny`, the log, `/etc/sudoers.d/thinny-checks`,
the clone in `/usr/local/src/thinny` and thinny itself. **There is no undo.** If a step fails, fix the
cause and run it again: it continues.

Storage pools that are not thinny's stay. The packages stay installed (the dashboard's with their
services masked, and their history): `apt purge` removes those nothing else uses. IP forwarding and
user namespaces stay on until the next reboot.

## Extras

Optional tools, off unless you set them up:

- [`extras/netcup-snapshot`](extras/netcup-snapshot/): a nightly snapshot of a netcup VPS
  before the update, run from another machine.
- [`extras/nightly-watch`](extras/nightly-watch/): a morning check of the update and snapshot
  status over SSH, with a mail on failure.

## License

MIT. See [LICENSE](LICENSE).
