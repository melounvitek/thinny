# thinny

Turns a Debian 13 or Ubuntu server into a shared [Hermes Agent](https://github.com/NousResearch/hermes-agent)
server for a small organisation. Each instance is an [Incus](https://linuxcontainers.org/incus/)
system container with its own Hermes and a password-protected Webapp, or
[hermes-webui](https://github.com/nesquena/hermes-webui) in its place. All instances share one
central policy and one Hermes release.

Status: in development. Design notes: [`PLAN.md`](PLAN.md).

- **Isolated instances:** one container each (Debian 13 with systemd), with CPU, memory,
  process and disk limits. A firewall lets it reach the internet and nothing else. Its users
  have `sudo` in it.
- **One central policy** from `config.toml`, mounted read-only as each container's `/etc/hermes`
  (Hermes "managed scope"): approvals and the agent kept in its container. The optional **model
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
- **One Hermes release**, with an optional nightly update that moves instances one at a time.
- **Webapp or WebUI per instance:** Hermes's built-in Webapp, or hermes-webui, with the same
  password. WebUI follows its stable releases like Hermes.
- **Four ways to open the Webapp:** office network (HTTPS), Tailscale, your own address, or an
  SSH tunnel.
- **`thinny status`** for a quick look, **`thinny doctor`** for every isolation and health
  check, each with a fix.

## Security

**Protected:** instances from each other. Each container has its own uid range, filesystem,
network namespace and processes. It cannot reach other containers, the host's services,
private networks or the tailnet, and cannot change the shared policy.

**Not protected:**

- anything from the server admin or the model provider. The [access trace](#access-trace) shows
  when the admin enters a container;
- the policy from an instance's users: they are root in their container, so the policy is a
  guardrail. `doctor` reports the bypasses it can see;
- users from their own agent: it can do whatever they can in the container, but cannot leave it.

## Requirements

- Debian 13, Ubuntu 26.04 or Ubuntu 24.04, with SSH by key, as root or as a user with sudo.
- Internet access for GitHub, the Hermes and Claude Code installers, `images.linuxcontainers.org`,
  Debian mirrors and the model provider.
- About 0.4 GB RAM and 3 GB disk per idle instance, plus what its agent, Docker and Claude Code
  use. WebUI takes about 0.3 GB RAM more.

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
to office PCs. Example
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

**Unattended upgrades.** If they reboot the server, schedule it well after the nightly Hermes
update (`hermes.update_time`, 03:00), which can take an hour with many instances:

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
7. Once `config.toml` exists, it runs `thinny setup`: the Incus storage pool, the firewall, the
   update timer and the shared policy.

It does not add any instances.

</details>

On the first run, the installer asks for the model provider (OpenAI or Anthropic) and
model, the Webapp language, the internet interface and how users will open the Webapps. It
suggests the interface and LAN address it detects and the language pack it finds in Hermes's
catalog, shows a summary, and sets up the server once you confirm. Everything else keeps the
example's defaults: change them with `thinny config edit`.

Without a terminal, it stops after copying the example. Edit `/etc/thinny/config.toml` (at least
`policy.model`, `network.interface` and `access.lan_address`, or `""` and another
`access.default`), then run `sudo sh /usr/local/src/thinny/install.sh`.

Rerunning `install.sh` is safe. `thinny update` updates thinny itself too (see [Updates](#updates)).

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
instances with their next Hermes update. Without the model lock, `model_provider` and `model`
are only what `instance add` suggests, and `reasoning_effort` where new instances start; turning
the lock on removes the models users chose.

## Instances

```sh
thinny instance add alice               # about 7 minutes; asks how users open it, Webapp or WebUI, the model and a phone line
thinny instance login alice             # sign in to a model provider; asks which
thinny instance login alice --provider anthropic   # or name it: openai | anthropic
thinny instance telegram alice          # a Telegram bot its user creates with a link (see Telegram)
thinny instance list                    # with each one's CPU, RAM and disk use
thinny instance access alice tailscale  # lan | tailscale | url=<URL> | local
thinny instance frontend alice webui    # webapp | webui
thinny instance limits alice --cpu 4 --memory-mb 8192 --disk-gb 50
thinny instance lock alice [model ...]  # only these models (default: the one it uses)
thinny instance unlock alice            # its users choose, even with the server's lock
thinny instance reset-password alice
thinny instance disable alice           # stop it for good, keep its data
thinny instance remove alice            # then delete its container and data; asks you to type its name
thinny instance remove alice --purge    # also free its name and port, even once removed
thinny instance restart [alice ...]
thinny instance move alice bob --to admin@new-server   # or --all; experimental
thinny instance restart --group sales   # instead of names: limits, lock, unlock, login and move too (see Groups)
```

- `add` prints the address, the username (Webapp only) and the password. **The password is shown
  only once.** If `add` fails, fix the cause and run it again: it continues where it stopped.
- **WebUI** ([hermes-webui](https://github.com/nesquena/hermes-webui), `--frontend webui`) runs on
  the instance's port in place of the Webapp; the gateway keeps running for scheduled jobs and the
  phone. It starts in `policy.language` and `webapp.theme`, with Graphite colours, large text, only
  Chat and Todos in the sidebar and a plain message box; then it keeps its users' settings. A
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
- `add` can connect a phone line to the phone plugin. Answer `y` and it asks for the SIP account
  (Odorik's server by default), the PIN, the numbers that may call, the number Hermes calls back
  and an OpenAI API key for speech recognition, then starts Asterisk in the container. With
  `policy.language = "cs"` the phone speaks Czech. Every new instance gets a faster Piper voice
  (`tts.piper.length_scale` 0.65).
- If the line does not register, a stateless firewall at the hosting provider (such as a netcup
  firewall policy) may drop the SIP provider's replies: let UDP from it in, from any port to any
  port (Odorik: `81.31.45.0/24`).
- Without a line, the phone plugin stays idle until it is set up in the Webapp (Messaging →
  Hermes 3310), then with `~/.hermes/plugins/hermes-3310/install-asterisk.sh` and a gateway
  restart. See its README.
- `add` takes the same `--cpu`, `--memory-mb` and `--disk-gb`. Limits an instance was not given
  follow `[container]` in `config.toml`, also when it changes. `limits` applies at once, even to a
  running instance. The storage is compressed, and disk use and its limit count the compressed
  size (about half of what the files take).
- `lock` keeps the provider `alice` uses and allows the models given, the first by default; it
  pins effort and plugins as the server's lock does. `unlock` leaves `alice` on the model it was
  locked to, for its users to change. Either stays whatever `policy.model_lock` says. The model
  picker keeps showing every model; the model guard refuses the others.
- Names are 2-32 lowercase letters, digits and `-`, starting with a letter. Names and ports are
  not reused, and a disabled instance cannot be re-enabled.
- `remove` takes a disabled instance, or a pending one whose `add` you give up on. Its name
  stays taken and its [access trace](#access-trace) stays on the server.
- `remove --purge` frees the name and port of a removed instance too, for a new one. Old
  bookmarks and saved passwords for its address then lead to the new instance. Its access trace
  stays, renamed to `alice.purged-<time>`; rows still waiting for its Google Sheet are dropped.
- Shell in a container: `sudo incus exec alice -- runuser -l alice` (or `-- bash` for root).
  Both show in the [access trace](#access-trace).
- **`move` is experimental:** it has not moved an instance between two real servers yet.
  Snapshot both servers first.
- `move` takes whole containers over SSH to another server that runs thinny, one at a time, each
  down for a few minutes. Everything inside comes along: data, sign-ins, the password, Webapp or
  WebUI (at the new server's WebUI release), the model (unless the new server's model lock pins it), language, look and limits; its own
  `lock` or `unlock` does not: it follows the new server's `policy.model_lock`. The new
  server gives it its own port, address, policy and packages: `lan`, `tailscale` and `local`
  instances get a new address; for `url` ones, point your DNS or tunnel at the new server. The
  old copy stays here, disabled.
- Before a move, run `thinny update` on both servers: they need the same Hermes release. Each
  needs room for one container archive. SSH must reach the new server as root, or as an account
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
thinny instance restart --group sales   # also limits, lock, unlock, login and move
```

- A group's file holds Hermes settings, keys of Hermes's `config.yaml` in TOML (see
  [`group.example.toml`](group.example.toml)):

  ```toml
  [display]
  language = "cs"
  ```

- They join each member's policy, so its users cannot change them. thinny's own policy (the
  sandbox, approvals, the model lock) wins over them, and `webapp` is thinny's. A list, such as
  `plugins.enabled`, replaces the instance's own.
- Joining a group removes the instance's own values of those settings, and leaving it does not
  bring them back: Hermes's defaults apply.
- Every account in a member's container can read the group's settings, MCP servers' tokens
  included.
- `--group` takes the group's active instances one after another and stops at the first failure.
  With `lock`, every name is a model: `thinny instance lock --group sales gpt-5.5`. `login` waits
  for each instance's users in turn.
- To remove a group, move its instances to another group or none, then delete its file. While an
  instance is in a group without a file, `config apply` changes nothing and says so.

## Access

| Method      | Address users open                           | Your part |
| ----------- | -------------------------------------------- | --------- |
| `lan`       | `https://<lan_address>:<port>/`              | trust Caddy's certificate on office PCs |
| `tailscale` | `https://<machine>.<tailnet>.ts.net:<port>/` | the server and users' devices on the tailnet, with MagicDNS and HTTPS certificates |
| `url=<URL>` | your URL, on a host of its own               | a proxy or tunnel on this server, serving HTTPS and forwarding to `http://127.0.0.1:<port>`, or one tunnel for all (below) |
| `local`     | `http://<name>.localhost:<port>/`            | `ssh -L <port>:127.0.0.1:<port> <you>@<server>` |

Use HTTPS for network addresses: on plain HTTP, browsers turn off the Webapp's clipboard,
microphone, notifications and the Bot Screen. `<name>.localhost` through SSH keeps them.

**LAN certificate:** thinny runs Caddy with its own certificate authority and owns
`/etc/caddy/Caddyfile`. It copies the root certificate to `/etc/thinny/office-root.crt`, readable
by any account. While an instance uses LAN access, Caddy also serves it on the office network at
`http://<lan_address>/office-root.crt`; thinny prints the link when an instance gets LAN access.
Download it on each office PC and install it as trusted: open it, "Install Certificate", "Current
User", "Trusted Root Certification Authorities" (or for all accounts, as administrator:
`certutil -addstore Root office-root.crt`). Over plain HTTP, someone on the office network could
swap the file; where that matters, copy it with `scp <you>@<server>:/etc/thinny/office-root.crt .`
instead.

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
3. Put its token in `config.toml` with `thinny config edit`:

   ```toml
   [telegram]
   manager_token = "123456:ABC-..."
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
  `config.toml`, which only root can read.
- The bot belongs to its user: it is in their BotFather, where they can rename or delete it.
- Ctrl+C stops the wait. A bot created meanwhile waits up to 24 hours: run the command again and
  confirm it. One wait at a time per server; other thinny commands run meanwhile.
- Run it again to give an instance another bot, e.g. when its user deleted theirs. A moved
  instance keeps its bot.

## Updates

Every night at `hermes.update_time`, thinny first updates itself: it pulls the clone it was
installed from (`git pull --ff-only`) and, on a new commit, runs its `install.sh`, which also
applies `config.toml`. If that fails, the run stops there. A private clone needs read access
without a password, such as a deploy key.

Then it installs the latest stable Hermes release once it is `min_release_age_hours` old. It
updates one instance at a time and stops at the first failure; the next run finishes the job.
Then the same for WebUI's latest stable release, on the instances that use it. WebUI restarts
with every gateway restart, which a new Hermes needs.
With `auto_update = false` the nightly run leaves thinny as it is and only reports a new Hermes
release. The result is
in `/var/lib/thinny/update-status.json`, the log in `/var/log/thinny.log`. Nightly runs
also deliver new built-ins unless `auto_update = false`. Every non-dry run that gets past the thinny update updates Claude Code.

```sh
thinny update             # thinny, Hermes and WebUI now, ignoring the minimum age and auto_update
thinny update --dry-run   # only report what it would do
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

## Access trace

When someone reaches into an instance's container through Incus, with `incus exec`, `incus shell`,
`incus console` (or its log), `incus file`, a snapshot or an export, thinny notes the time and the
command. thinny's own commands there are noted once per run of thinny, e.g. `thinny doctor ran its
commands in alice` (`doctor` reads the instance's `.env`, among others).

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
the Incus profile and storage pool, the firewall table, `/etc/thinny`, `/etc/hermes`,
`/var/lib/thinny`, the log, `/etc/sudoers.d/thinny-checks`, the clone in `/usr/local/src/thinny`
and thinny itself. **There is no undo.** If a step fails, fix the cause and run it again: it
continues.

A storage pool that something else uses stays; the output says so. The packages stay installed:
`apt purge` removes those nothing else uses. IP forwarding and user namespaces stay on until the
next reboot.

## Extras

Optional tools, off unless you set them up:

- [`extras/netcup-snapshot`](extras/netcup-snapshot/): a nightly snapshot of a netcup VPS
  before the update, run from another machine.
- [`extras/nightly-watch`](extras/nightly-watch/): a morning check of the update and snapshot
  status over SSH, with a mail on failure.

## License

MIT. See [LICENSE](LICENSE).
