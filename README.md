# thinny

Turns a Debian 13 or Ubuntu server into a shared [Hermes Agent](https://github.com/NousResearch/hermes-agent)
server for a small organisation, such as an office of a dozen people who are not programmers.
The server gets one central policy and one Hermes release for everyone. Each person gets their
own [Incus](https://linuxcontainers.org/incus/) system container with their own Hermes and a
private Webapp.

Status: in development. Design notes and test-server findings: [`PLAN.md`](PLAN.md).

What you get:

- **One container per person** (Debian 13 with systemd), with their own Hermes from the
  official installer, model-provider login, gateway and password-protected Webapp. The person
  has `sudo` in it, so they and their agent can install packages.
- **One Hermes release for everyone.** New people get the release everyone runs, and the
  nightly update moves everyone to the next one.
- **One central policy** in `/etc/hermes` (Hermes "managed scope"), mounted read-only into every
  container. It sets approvals and keeps the agent in the person's container. With the **model
  lock** (`policy.model_lock`, off by default) it also sets the provider, the model, the allowed
  models, the reasoning effort and the plugins; without it, people start with the site's model and
  choose their own. People cannot change the policy from the Webapp or their own settings.
- **Built in for everyone:** [Claude Code](https://code.claude.com/) with the Claude subscription
  plugin, so a person can use their own Claude Pro or Max subscription, and the
  [hermes-3310](https://github.com/melounvitek/hermes-3310) phone plugin with Docker for its
  Asterisk.
- **CPU, memory, process and disk limits** per container.
- **A firewall table** that lets each container reach the internet and nothing else.
- **Four ways to open the Webapp**, chosen per person: office network (HTTPS), Tailscale, your
  own address, or an SSH tunnel.
- **An optional nightly Hermes update** that updates and restarts people one at a time.
- **`thinny status`** for a quick look and **`thinny doctor`** for every isolation and health
  check, each with a fix.

## What is and is not protected

**Protected: people from each other.** Every person has their own container. Their chats,
files, model-provider login, Webapp and agent live in it, and the host keeps containers apart:

- each container has its own uid range on the host, its own filesystem, network namespace and
  processes. A person sees only their own users and processes, not other people's names;
- the host has no person accounts. Each Webapp is published on the host's loopback by Incus
  and asks for a username and password;
- a firewall table (`inet thinny`) lets a container reach only the internet: not other
  containers, not the host's services, not private networks or the tailnet, not the networks
  you block, and no IPv6. Only the host itself can open a connection into a container. Incus
  starts only after this table is loaded;
- Incus drops packets with another source address than the container's own (anti-spoofing);
- the shared policy and the model-guard plugin are mounted read-only, so nobody can change
  them for others.

**Not protected:**

- **From the server admin.** Root can read everything on the machine that runs the agent.
- **From the model provider.** Conversations go to the provider you configure.
- **The policy against a determined person.** A person is root in their own container
  (passwordless `sudo`, and the Webapp has a terminal). Approvals and, with the model lock, the
  pinned model and the model guard cannot be changed from the Webapp or the person's settings,
  but the person can get around them inside their container. They are a guardrail. `thinny doctor`
  reports the bypasses it can see. The container stays isolated from everyone else.
- **A person from their own agent.** An agent misled by a web page can do whatever the person
  can do in their container, including `sudo`. It cannot leave the container.
- **Plain HTTP over the network.** It sends passwords and chats unencrypted. LAN and Tailscale
  access use HTTPS, and local access goes through an SSH tunnel. If you use your own address,
  keep it on HTTPS too.

Docker and Tailscale work inside a container (nesting and `/dev/net/tun` are on). thinny installs
Docker for the phone plugin and puts the person in the `docker` group. Tailscale they can install
with `sudo`.

## Requirements

- Debian 13, Ubuntu 26.04 or Ubuntu 24.04 (`install.sh` warns on anything else) and root
  access over SSH with a key.
- Incus from the distribution's `incus-base` package (`incus` on Ubuntu 24.04), which
  `install.sh` installs. Containers only, no virtual machines, so the server needs no nested
  virtualisation.
- Internet access from the server for GitHub, the Hermes installer and what it downloads, the
  Claude Code installer, the Debian container image (`images.linuxcontainers.org`), Debian
  mirrors and the model provider.
  `network.interface` must name the server's internet interface (`eth0` by default).
- **Your firewall's forward chain must let the containers through.** thinny turns on
  IPv4 forwarding and filters the containers' traffic in its own table. A drop in any nftables
  chain is final, so if your own table has a forward chain that drops by default, add these
  two lines to it (see the example in [Prerequisites](#prerequisites-your-part)):

  ```
  iifname "hm-*" accept
  oifname "hm-*" ct state established,related accept
  ```

  With ufw, which drops forwarded traffic by default, run `ufw route allow in on hm-+`
  instead. Without this, `instance add` fails at the packages and `doctor` reports that the
  container cannot reach the internet.
- Hermes from its latest stable release. Tested with Hermes v0.23.
- Rough sizing, measured on a test server: about 0.4 GB RAM per idle person, plus what their
  agent's browser and tools use, and about 3 GB disk for each person's Hermes plus their
  container's system packages. Docker, Claude Code and the phone plugin add to this (not measured
  yet). Each container is capped at `container.disk_gb`.
- One model-provider account per person. The default provider, `openai-codex`, signs in with
  ChatGPT. A person can also use their own Claude Pro or Max subscription. Sharing one
  subscription among several people is against the providers' terms.

## Prerequisites (your part)

`thinny doctor` verifies these (`WARN` when missing) and prints the fix. Set them up
before you add people.

**Key-only SSH, reachable only from a private network** (office LAN, VPN or Tailscale). Create
`/etc/ssh/sshd_config.d/10-keys-only.conf`:

```
PasswordAuthentication no
KbdInteractiveAuthentication no
```

Then run `systemctl reload ssh`. `doctor` verifies only that SSH is key-only. Limiting where
SSH can be reached from is the job of your input firewall.

**An input firewall that drops by default** (nftables or ufw). With LAN access, it must let
office PCs reach the Webapp ports (`webapp.port_range`, 9120-9199 by default). Example
`/etc/nftables.conf`:

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
	}
	chain forward {
		type filter hook forward priority filter; policy drop;
		iifname "hm-*" accept                               # the containers (filtered by thinny)
		oifname "hm-*" ct state established,related accept
	}
}
```

Load it with `nft -f /etc/nftables.conf`. `doctor` also warns when no forward chain drops by
default: thinny turns IPv4 forwarding on, so the server would route between its networks.

> **Never run `systemctl restart nftables` on Debian or Ubuntu while containers run.** Its stop
> step runs `nft flush ruleset`, which removes every table: Tailscale's, Incus's anti-spoofing
> rules and thinny's. A config file that starts with `flush ruleset` does the same,
> and the default file does. If it happens, run `thinny config apply` (it reloads the
> containers' table) and `systemctl restart tailscaled`, then reboot at a quiet time: Incus
> sets up its anti-spoofing rules when a container starts.

**Unattended upgrades, with any reboot after the nightly update.**

```sh
apt install unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades
```

If you let it reboot, choose a time well after `hermes.update_time` (03:00 by default). With
many people the update can take an hour. For example, in
`/etc/apt/apt.conf.d/52unattended-upgrades-local`:

```
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "05:00";
```

The nightly update never reboots the server. When the OS needs a reboot, it says so in its
status.

## Install

As root:

```sh
apt update
git clone <repository URL> /root/thinny
cd /root/thinny
sh install.sh
```

The first run does three things and stops:

1. installs the packages (`incus-base`, `btrfs-progs`, `nftables`, ...);
2. installs the tool into `/usr/local/lib/thinny`;
3. creates `/etc/thinny/site.toml` from `site.example.toml`.

Edit `site.toml` (see [Settings](#settings)). At least set `policy.model` (with
`policy.model_lock = true` also `policy.allowed_models`), `access.lan_address` and
`network.interface`. If nobody will use LAN
access, set `access.lan_address = ""` and choose another `access.default`. Then run the script
again:

```sh
sh install.sh
```

The second run:

1. creates the Incus storage pool `default`: btrfs in a sparse file, sized once to 80% of
   the free space under `/var/lib`. It takes space only as people use it;
2. loads the firewall table and makes `incus.service` load it before it starts;
3. turns on IPv4 forwarding and installs the nightly update timer. On Ubuntu, it also turns
   off AppArmor's restriction on user namespaces, without which the agent's browser cannot
   start. A person is root in their container and can create them anyway, as on Debian;
4. creates the Incus profile `hm-person` with the `[container]` limits;
5. writes `/etc/hermes`.

Hermes itself is installed into each person's container by `instance add`.

Rerunning `install.sh` is safe. It repairs what is missing, keeps all data and never updates
Hermes. If the rerun changes `/etc/hermes`, it restarts the gateways one by one, like
`config apply`. A changed model guard reaches the gateways at their next restart (`thinny instance restart`).

To update thinny itself:

```sh
cd /root/thinny && git pull && sh install.sh
```

## Settings

The settings live in `/etc/thinny/site.toml`. Every key is commented in
[`site.example.toml`](site.example.toml). To change them:

```sh
thinny config edit
```

This opens a copy of the file in `$VISUAL`, `$EDITOR` or the system's default editor
(`editor`, usually nano). When you save and close it, thinny checks the copy. If it is valid,
thinny replaces `site.toml` with it and applies it. If not, it shows the error and lets you
edit again or discard the changes, so a broken file never takes effect.

| Section       | Holds |
| ------------- | ----- |
| `[hermes]`    | the nightly update: `auto_update`, `update_time`, `min_release_age_hours` |
| `[policy]`    | `model_provider`, the default `model`, `reasoning_effort`, `model_lock` (pins those three and the plugins, and allows only `allowed_models`, enforced by the model-guard plugin), `approvals` (`manual` or `smart`), the UI `language` and its `language_pack`, `model_guard_message` |
| `[container]` | each container's limits: `cpu`, `memory_mb`, `disk_gb` (processes are capped at 2000) |
| `[webapp]`    | `port_range`, and the Webapp look for new people (`theme`, `text_scale` (110 %), `interface`, `known_models`) |
| `[access]`    | the `default` access method, `lan_address` |
| `[network]`   | `interface`: the server's internet interface; `blocked_networks`: public networks containers may not reach (private networks and the tailnet are always blocked) |

If you edit `site.toml` another way, apply it with:

```sh
thinny config apply
```

Applying does the following:

- rewrites `/etc/hermes`, the firewall table, the units and the container profile. New
  limits apply to running containers too;
- updates each person's Webapp address;
- updates the Caddy sites and `tailscale serve`;
- restarts gateways one by one. It restarts every gateway when `/etc/hermes` changed, and
  otherwise only those whose Webapp address changed.

Notes:

- Unknown keys, placeholder values, and, with `model_lock`, a `model` that is not in
  `allowed_models` are errors.
- Some values apply only to people added afterwards: `policy.language`, the `[webapp]` look,
  the models shown in the model picker, and `port_range`. Existing people keep theirs.
  However, a model removed from `allowed_models` is blocked for everyone at once.
- Without `model_lock`, `model_provider`, `model` and `reasoning_effort` are only where new
  people start. People choose their own in the Webapp and turn plugins on and off in its
  Settings → Plugins. Turning the lock on removes the model people chose for themselves. If you
  turn it off when people already exist, they get their signed-in provider's first model until
  they choose one.
- `language_pack` is a plugin name from the Hermes plugin catalog, e.g. `hermes-lang-cs`. Each
  person's Hermes installs it at the commit its catalog pins, at `instance add` and with every
  update. A new `language_pack` reaches existing people with the next Hermes update.

## People

### Add a person

```sh
thinny instance add alice
```

The command asks how the person will open their Webapp (see [Access](#access)):

```
How will alice open their Webapp?
  1) Office network (LAN)  https://192.168.1.10:<port>/  built-in HTTPS proxy
  2) Tailscale             https://<machine>.<tailnet>.ts.net:<port>/
  3) Own address           you enter the URL (Cloudflare, your proxy, ...)
  4) Local only            no network access; open via an SSH tunnel
Choice [1]:
```

You can also answer up front with `--access lan|tailscale|url=<URL>|local`. Without a
terminal, the command uses `access.default`. The Tailscale option is offered only when
Tailscale is running.

Names are 2-32 characters long and use lowercase letters, digits and `-`. They start with a
letter, and must not be the name of an account on the host (the container has Debian's
system accounts too) or of an existing Incus container.

`instance add` takes about 7 minutes. It:

1. picks the next free Webapp port and container address (`10.78.0.0/24`);
2. creates the container `alice` from the Debian 13 image with the `hm-person` profile, a
   routed network interface (`hm-<port>` on the host), the read-only policy and the Webapp
   proxy, and starts it;
3. installs the system packages (Chromium's libraries, the Bot Screen desktop, build tools,
   Docker, `sudo`, `unattended-upgrades`);
4. creates the account `alice` in the container, with passwordless `sudo` and in the `docker`
   group;
5. installs Hermes with the official installer at the release everyone runs, builds the
   Webapp, installs Claude Code, and installs and turns on the language pack and the built-in
   plugins;
6. mounts the model-guard plugin read-only into the person's Hermes;
7. configures the Webapp on `127.0.0.1:<port>` in the container with its public address and
   a generated password, and sets the UI language and look (and, without the model lock, the
   provider, model and reasoning effort the person starts with);
8. publishes the Webapp;
9. starts the gateway and checks that it answers.

At the end, it prints the address, the username (the person's name) and the password. **The
password is shown only once.** If the command fails halfway, fix the cause and run it again.
It continues where it stopped.

### Sign in and hand over

```sh
thinny instance login alice
```

This command signs the person in to the model provider with a device code (ChatGPT for
`openai-codex`). It prints a link and a code. The person opens the link, signs in to *their
own* account and enters the code within about 15 minutes. The tokens stay in their container
(`~/.hermes/auth.json`). Never copy that file between people.

Run `instance login` before the hand-over. Without it, the Webapp's first screen invites the
person to connect other providers. Then give the person the address, username and password. With LAN
access, their PC also needs the office certificate.

A person who uses their own Claude Pro or Max subscription signs in with:

```sh
thinny instance login alice --claude
```

It prints a link. The person opens it, signs in to *their own* Claude account and gives you the
code they get, which you paste. The login stays in their container (`~/.claude`). Then they choose
*Claude Subscription DirectSDK (Experimental)* in the Webapp's model picker, which the model
lock allows only when `model_provider = "claude-subscription-directsdk-experimental"`. Every turn
counts against their subscription.

### Phone (hermes-3310)

Every person has the [hermes-3310](https://github.com/melounvitek/hermes-3310) plugin and Docker,
so they can call their Hermes over a phone line. It stays idle until it is set up with the person's
own SIP account: the PIN, the allowed callers and the SIP account on the Webapp's Messaging →
Hermes 3310 card, then `~/.hermes/plugins/hermes-3310/install-asterisk.sh` in the Webapp terminal
and a gateway restart. The plugin's README has the details. Calls through a container are not
tested yet: Asterisk reaches the SIP provider through the server's NAT and thinny's firewall.

### Other commands

```sh
thinny instance list                      # people, state, access, port, gateway, URL
thinny instance access alice tailscale    # change the access method (lan | tailscale | url=<URL> | local)
thinny instance reset-password alice      # new Webapp password, shown once
thinny instance disable alice             # stop the container for good, keep the data
thinny instance restart [alice ...]       # restart gateways one by one (all, or the named people)
```

- `instance access` restarts the person's gateway, and the old address stops working.
- `instance reset-password` restarts the person's gateway, and open browsers must sign in again.
- `instance disable` turns off the container's autostart, stops the container (forced after
  60 s) and removes the person's Caddy site or `tailscale serve` entry. The container and the
  person's data stay. Names, ports and container addresses are never reused, and a disabled
  person cannot be re-enabled.

To get a shell in a person's container:

```sh
incus exec alice -- runuser -l alice     # as alice
incus exec alice -- bash                 # as root
```

If the person installs `openssh-server` in their container, you can also reach it over SSH
with the server as jump host: `ssh -J root@<server> alice@<container address>`
(`incus list alice` shows the address).

## Access

Each person has one access method, because a Webapp only answers at its own address. The
Webapp listens on `127.0.0.1:<port>` inside the person's container, and an Incus proxy device
publishes it on the same port on the host's loopback. Every method points there.

| Method      | Address the person opens                     | Your part |
| ----------- | -------------------------------------------- | --------- |
| `lan`       | `https://<lan_address>:<port>/`              | trust Caddy's certificate on office PCs; open the ports in your input firewall |
| `tailscale` | `https://<machine>.<tailnet>.ts.net:<port>/` | enable HTTPS certificates in the tailnet; the person's device joins the tailnet |
| `url=<URL>` | your URL                                     | run a proxy or tunnel to `http://127.0.0.1:<port>` |
| `local`     | `http://<name>.localhost:<port>/`            | open an SSH tunnel |

**HTTPS matters.** On a plain-HTTP network address, browsers turn off the Webapp's
clipboard, microphone, notifications and the Bot Screen. `http://<name>.localhost` through a
tunnel counts as secure, so local access keeps every feature.

### Office network (`lan`)

thinny installs Caddy the first time someone gets LAN access. Each LAN person gets a
site on `access.lan_address` and their port. The site uses Caddy's own certificate authority
(`tls internal`) and forwards to the Webapp.

- **Trust the certificate on office PCs.** Otherwise every browser shows a warning. Copy the
  root certificate from the server:

  ```sh
  scp root@<server>:/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt office-root.crt
  ```

  Install it as a trusted root certificate on each PC. On Windows, run
  `certutil -addstore Root office-root.crt` as administrator, or use a group policy. The file
  appears once Caddy serves its first LAN site.
- **Open the Webapp ports** to the office network in your input firewall (see
  [Prerequisites](#prerequisites-your-part)).
- **thinny owns `/etc/caddy/Caddyfile`** once anyone uses LAN access. It rewrites the
  whole file (with Caddy's admin API off), so do not use this Caddy for other sites.

### Tailscale (`tailscale`)

The Webapp is published with `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`.
It gets a real certificate, so there is no browser warning.

- Enable MagicDNS and HTTPS certificates for your tailnet in the Tailscale admin console.
- The person's devices must be on the tailnet. Your tailnet access rules decide who can reach
  the server.

### Own address (`url=<URL>`)

For a Cloudflare Tunnel, a reverse proxy, or similar:

```sh
thinny instance add alice --access url=https://alice.example.org/
```

- Run the proxy or tunnel client on this server and point it at `http://127.0.0.1:<port>`.
  `thinny instance list` shows the port.
- Keep the address on HTTPS.

### Local only (`local`)

The Webapp is not reachable from the network. Open an SSH tunnel, then browse to
`http://alice.localhost:<port>/`:

```sh
ssh -L <port>:127.0.0.1:<port> root@<server>
```

## Updates

`thinny-update.timer` runs `thinny update --scheduled` every night at
`hermes.update_time`. If the server is off at that time, the update waits for the next night.

1. It asks GitHub for the latest stable Hermes release.
2. It stops if that is already the release everyone runs (`up-to-date`), or if the release is
   younger than `min_release_age_hours` (`waiting`).
3. With `auto_update = false`, it only reports the release (`available`).
4. Otherwise it updates the people one container at a time. For each person, it:
   1. installs the container's system packages (a new Hermes may need some the list gained)
      and the groups the built-ins need;
   2. runs the official installer with `--commit <release commit>` as the person, which
      moves their Hermes to exactly that release while their gateway keeps running;
   3. rebuilds the Webapp, installs Claude Code if it is missing, reinstalls the language pack
      and the built-in plugins and removes old Python environments (`hermes pm gc`, which keeps
      the previous one);
   4. restarts the gateway and checks that it is active, the Webapp answers and, with the model
      lock, model-guard is enabled. **It stops at the first failure.** People not yet updated
      keep running the old release.

   When everyone runs the new release, it records it in `/var/lib/thinny/release`.
   From then on, new people get it.
5. It writes the result (`up-to-date`, `waiting`, `available`, `ok` or `FAILED`) to
   `/var/lib/thinny/update-status.json`. If the OS needs a reboot, it adds that to the
   status. It never reboots.

```json
{"time": "2026-01-31T03:04:12", "result": "ok", "detail": "updated to <tag>"}
```

By hand:

```sh
thinny update                        # update now, ignoring the minimum age and auto_update = false
thinny update --dry-run              # report what `thinny update` would do; changes nothing, writes no status
thinny update --scheduled --dry-run  # the same for the nightly run
```

- **Log:** `/var/log/thinny.log` (all commands), and for the nightly run also
  `journalctl -u thinny-update.service`.
- **New built-ins** come with thinny updates: a new pin of the phone plugin, another built-in
  plugin or a changed container package list. The next run gives them to everyone at the
  release they run, even without a new Hermes release (`ok`, `new built-ins at <tag>`), one
  person at a time as above, and turns the plugins on again. With `auto_update = false` it only
  reports them (`available`). A plain Hermes update leaves the plugins people turned off alone.
  People added before Docker was built in can use it from their Webapp terminal once their
  container restarts.
- **Claude Code** is updated for everyone on every run (`claude update`), whatever happens to
  Hermes: the Claude subscription plugin turns its own updates off, and newer Claude models need
  newer versions. A failure is only logged.
- **OS updates inside the containers** come from `unattended-upgrades`, installed with
  Debian's defaults. The container's package list follows the official Hermes image's
  Dockerfile and is installed at `instance add` and before each Hermes update.
- **People can change their own Hermes**, since they are root in their container. `doctor`
  warns when someone does not run the release everyone runs.
- **There is no rollback.** Hermes databases cannot be downgraded. The only way back is your
  own snapshot of the whole server, taken before `update_time` (see
  [`extras/netcup-snapshot`](extras/netcup-snapshot/)).
- **Night-time jobs can be cut off.** A gateway restart waits for a reply in progress, but it
  cuts off scheduled jobs and background tasks. Keep people's scheduled jobs away from
  `update_time`.
- **Interrupted work is finished later.** If an update stops halfway, the next run (or
  `thinny update`) finishes it first, to the same release and without waiting
  for the minimum age. It starts with the person it stopped at, from the beginning, and skips
  the people already done. Gateways that `config apply`, `update` or `instance restart` could
  not restart are remembered and restarted by the next of these commands.
- **One run at a time.** thinny runs one command at a time. During the update, other
  commands that change things refuse to start. If such a command is still running at
  `update_time`, that night's update fails.

## Status and checks

```sh
thinny status
```

`status` is the quick look: the Hermes release everyone runs, the last nightly update, and
whether each person's container and gateway run.

```sh
thinny doctor
```

`doctor` runs every check below. Both print one line per result (`OK`, `WARN` or `FAIL`),
with a `fix:` line where it helps, and exit with status 1 when anything fails, so you can use
them from monitoring.

- **Install:**
  - the Incus storage pool `default` and profile `hm-person` exist;
  - `incus.service` loads the firewall table before it starts;
  - IPv4 forwarding is on;
  - on Ubuntu, AppArmor lets people create user namespaces (their browser needs them);
  - `/etc/hermes` matches `site.toml`;
  - the release everyone runs, and the update timer is enabled;
  - the last nightly update result: `FAIL` if it failed, `WARN` if it is missing or older
    than 48 h.
- **Prerequisites:** everything in [Prerequisites](#prerequisites-your-part).
- **Firewall:** the loaded `inet thinny` table is exactly the one `site.toml` gives,
  chain by chain and rule by rule (`FAIL` if not).
- **Office network:** Caddy is running when anyone uses LAN access.
- **Per person:**
  - the container is running and reaches the internet. If it does not, the fix shows the
    forward-chain lines from [Requirements](#requirements);
  - the gateway is active, the Webapp answers and the public address is as expected;
  - Hermes is at the release everyone runs (`WARN`);
  - Claude Code and the built-in plugins are installed (`WARN`). Whether a plugin is on is the
    person's choice without the model lock;
  - no bypass of the policy in the person's files: `HERMES_MANAGED_DIR`,
    `HERMES_BUNDLED_PLUGINS` or `HERMES_ENABLE_PROJECT_PLUGINS` in `.env` or the gateway unit,
    their own `model-guard` plugin (with the model lock), or a gateway unit that starts
    something else.

## Files on the server

| Path | What |
| ---- | ---- |
| `/etc/thinny/site.toml` | your settings |
| `/etc/thinny/thinny.nft` | the containers' firewall table (generated) |
| `/etc/systemd/system/incus.service.d/thinny.conf` | loads that table before Incus starts |
| `/etc/systemd/system/thinny-update.*` | the nightly update service and timer |
| `/etc/sysctl.d/90-thinny.conf` | IPv4 forwarding for the containers |
| `/var/lib/thinny/people.json` | the people registry: name, port, access, URL, container address, state, built-ins |
| `/var/lib/thinny/release` | the Hermes release everyone runs (tag and commit) |
| `/var/lib/thinny/update-status.json` | the last nightly update result |
| `/var/log/thinny.log` | the log |
| `/etc/hermes/` | managed scope, mounted read-only into every container: `config.yaml`, `.env` (no secrets), `allowed-models.txt`, `model-guard-message.txt` (generated) |
| `/usr/local/lib/thinny/`, `/usr/local/sbin/thinny` | the tool; its `files/model-guard` is mounted read-only into every container |
| `/var/lib/incus/` | Incus, with the storage pool `default` (a btrfs file) that holds the containers |
| `/etc/caddy/Caddyfile` | the LAN sites (generated, when used) |
| container `<name>`: `/home/<name>/.hermes/` | a person's Hermes: code, chats, settings, Webapp sign-in, provider login, plugins |
| container `<name>`: `/home/<name>/.local/bin/claude`, `~/.claude/` | a person's Claude Code and Claude login |

Generated files are rewritten by `config apply`, so do not edit them by hand.

## Extras

Optional tools, off unless you set them up:

- [`extras/netcup-snapshot`](extras/netcup-snapshot/): takes a nightly snapshot of a netcup VPS
  before the update, run from another machine.
- [`extras/nightly-watch`](extras/nightly-watch/): runs a morning check of the update and
  snapshot status files over SSH, and cron mails you on failure.

## Rejected options

- **Docker on the host:** membership in the docker group equals root.
- **Rootless Podman with the official Hermes image per person:** one 3.85 GB image copy per
  person, no `sudo` or `apt` for the person, and SSH, Tailscale and the phone plugin need
  workarounds.
- **One Linux account per person on a shared `/opt/hermes` install** (the previous design):
  borrowing a root-owned install needed workarounds for permissions, Python environments and
  the Webapp build, and a loopback firewall per account.
- **An Incus managed bridge:** containers can resolve each other's names through its DNS.
- **One Linux account per department:** colleagues could read each other's chats, logins and
  Webapp passwords.
- **Hermes profiles per person in one account:** the Webapp shows every profile.
- **Hermes's built-in "Web Access":** it needs a Nous account per person and uses plain HTTP on
  the LAN.
- **Plain HTTP as the LAN default:** it is unencrypted, and browsers turn off Webapp features.
- **A third-party web UI:** worse than the built-in Webapp for this use.
- **One shared ChatGPT subscription:** against the provider's terms.
- **Editing Hermes source:** everything works through managed scope and plugins, which keeps
  updates safe.

## License

MIT. See [LICENSE](LICENSE).
