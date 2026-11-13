# thinny

Turns a Debian 13 or Ubuntu server into a shared [Hermes Agent](https://github.com/NousResearch/hermes-agent)
server for a small organisation. Each instance is an [Incus](https://linuxcontainers.org/incus/)
system container with its own Hermes and a password-protected Webapp. All instances share one
central policy and one Hermes release.

Status: in development. Design notes: [`PLAN.md`](PLAN.md).

- **Isolated instances:** one container each (Debian 13 with systemd), with CPU, memory,
  process and disk limits. A firewall lets it reach the internet and nothing else. Its users
  have `sudo` in it.
- **One central policy** in `/etc/hermes` (Hermes "managed scope"), mounted read-only: approvals
  and the agent kept in its container. The optional **model lock** (`policy.model_lock`) also pins
  the provider, model, reasoning effort and plugins, and allows only `allowed_models`. Without it,
  users choose their own.
- **Built into every instance:** [Claude Code](https://code.claude.com/) with the Claude
  subscription plugin, and the [hermes-3310](https://github.com/melounvitek/hermes-3310) phone
  plugin with Docker.
- **One Hermes release**, with an optional nightly update that moves instances one at a time.
- **Four ways to open the Webapp:** office network (HTTPS), Tailscale, your own address, or an
  SSH tunnel.
- **`thinny status`** for a quick look, **`thinny doctor`** for every isolation and health
  check, each with a fix.

## Security

**Protected:** instances from each other. Each container has its own uid range, filesystem,
network namespace and processes. It cannot reach other containers, the host's services,
private networks or the tailnet, and cannot change the shared policy.

**Not protected:**

- anything from the server admin or the model provider;
- the policy from an instance's users: they are root in their container, so the policy is a
  guardrail. `doctor` reports the bypasses it can see;
- users from their own agent: it can do whatever they can in the container, but cannot leave it.

## Requirements

- Debian 13, Ubuntu 26.04 or Ubuntu 24.04, with root SSH access by key.
- Internet access for GitHub, the Hermes and Claude Code installers, `images.linuxcontainers.org`,
  Debian mirrors and the model provider.
- About 0.4 GB RAM and 3 GB disk per idle instance, plus what its agent, Docker and Claude Code
  use.

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
access, open the Webapp ports (`webapp.port_range`, 9120-9199) to office PCs. Example
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

As root:

```sh
apt update
git clone <repository URL> /root/thinny
cd /root/thinny
sh install.sh
```

The first run installs the packages and the tool, then stops. Edit `/etc/thinny/site.toml`: set
at least `policy.model` (with the model lock also `policy.allowed_models`), `network.interface`
and `access.lan_address` (or `""` and another `access.default` without LAN access). Then run `sh install.sh` again to set
up Incus, the firewall and the update timer.

Rerunning `install.sh` is safe. To update thinny: `cd /root/thinny && git pull && sh install.sh`.
A changed model guard loads at each gateway's next restart (`thinny instance restart`).

## Settings

Every key of `/etc/thinny/site.toml` is commented in [`site.example.toml`](site.example.toml).

```sh
thinny config edit    # edit a copy, check it, then apply it
thinny config apply   # apply after editing the file another way
```

Applying restarts gateways one by one where needed. `policy.language`, `port_range` and the
`[webapp]` look apply only to instances added afterwards. A new `language_pack` reaches existing
instances with their next Hermes update. Without the model lock, `model_provider`, `model` and
`reasoning_effort` are only where new instances start; turning the lock on removes the models
users chose.

## Instances

```sh
thinny instance add alice               # about 7 minutes; asks how users open the Webapp
thinny instance login alice             # sign in to the model provider with a device code
thinny instance login alice --claude    # or to a Claude Pro or Max subscription
thinny instance list
thinny instance access alice tailscale  # lan | tailscale | url=<URL> | local
thinny instance reset-password alice
thinny instance disable alice           # stop it for good, keep its data
thinny instance restart [alice ...]
```

- `add` prints the Webapp address, username and password. **The password is shown only
  once.** If `add` fails, fix the cause and run it again: it continues where it stopped.
- Run `login` before the hand-over. Whoever uses `alice` opens the link and signs in with their
  own account within about 15 minutes. With `--claude`, they then choose *Claude Subscription
  DirectSDK (Experimental)* in the Webapp's model picker (with the model lock, only if
  `model_provider = "claude-subscription-directsdk-experimental"`).
- The phone plugin stays idle until it is set up in the Webapp (Messaging → Hermes 3310), then
  with `~/.hermes/plugins/hermes-3310/install-asterisk.sh` and a gateway restart. See its README.
- Names are 2-32 lowercase letters, digits and `-`, starting with a letter. Names and ports are
  never reused, and a disabled instance cannot be re-enabled.
- Shell in a container: `incus exec alice -- runuser -l alice` (or `-- bash` for root).

## Access

| Method      | Address users open                           | Your part |
| ----------- | -------------------------------------------- | --------- |
| `lan`       | `https://<lan_address>:<port>/`              | trust Caddy's certificate on office PCs |
| `tailscale` | `https://<machine>.<tailnet>.ts.net:<port>/` | the server and users' devices on the tailnet, with MagicDNS and HTTPS certificates |
| `url=<URL>` | your URL                                     | a proxy or tunnel on this server, serving HTTPS and forwarding to `http://127.0.0.1:<port>` |
| `local`     | `http://<name>.localhost:<port>/`            | `ssh -L <port>:127.0.0.1:<port> root@<server>` |

Use HTTPS for network addresses: on plain HTTP, browsers turn off the Webapp's clipboard,
microphone, notifications and the Bot Screen. `<name>.localhost` through SSH keeps them.

**LAN certificate:** thinny runs Caddy with its own certificate authority and owns
`/etc/caddy/Caddyfile`. Install its root certificate as trusted on each office PC (on Windows:
`certutil -addstore Root office-root.crt` as administrator):

```sh
scp root@<server>:/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt office-root.crt
```

## Updates

Every night at `hermes.update_time`, thinny installs the latest stable Hermes release once it
is `min_release_age_hours` old (with `auto_update = false` it only reports it). It updates one
instance at a time and stops at the first failure; the next run finishes the job. The result is
in `/var/lib/thinny/update-status.json`, the log in `/var/log/thinny.log`. Nightly runs
also deliver new built-ins unless `auto_update = false`. Every non-dry run updates Claude Code.

```sh
thinny update             # update now, ignoring the minimum age and auto_update
thinny update --dry-run   # only report what it would do
```

- **There is no rollback.** Hermes databases cannot be downgraded. Snapshot the server before
  `update_time` (see [`extras/netcup-snapshot`](extras/netcup-snapshot/)).
- A gateway restart waits for a reply in progress, but cuts off scheduled jobs. Keep them away
  from `update_time`.
- The update never reboots the server.

## Status and checks

```sh
thinny status   # release, last update, containers, gateways
thinny doctor   # every isolation and health check, each with a fix
```

Both exit with status 1 when anything fails, so monitoring can use them.

## Extras

Optional tools, off unless you set them up:

- [`extras/netcup-snapshot`](extras/netcup-snapshot/): a nightly snapshot of a netcup VPS
  before the update, run from another machine.
- [`extras/nightly-watch`](extras/nightly-watch/): a morning check of the update and snapshot
  status over SSH, with a mail on failure.

## License

MIT. See [LICENSE](LICENSE).
