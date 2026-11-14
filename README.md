# thinny

Turns a Debian 13 or Ubuntu server into a shared [Hermes Agent](https://github.com/NousResearch/hermes-agent)
server for a small organisation. Each instance is an [Incus](https://linuxcontainers.org/incus/)
system container with its own Hermes and a password-protected Webapp. All instances share one
central policy and one Hermes release.

Status: in development. Design notes: [`PLAN.md`](PLAN.md).

- **Isolated instances:** one container each (Debian 13 with systemd), with CPU, memory,
  process and disk limits. A firewall lets it reach the internet and nothing else. Its users
  have `sudo` in it.
- **One central policy** from `site.toml`, mounted read-only as each container's `/etc/hermes`
  (Hermes "managed scope"): approvals and the agent kept in its container. The optional **model
  lock** (`policy.model_lock`) also pins the provider, model, reasoning effort and plugins, and
  allows only `allowed_models`. Without it, users choose their own. One instance can have its own
  lock, or none, whatever the site's (`thinny instance lock` and `unlock`).
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

- Debian 13, Ubuntu 26.04 or Ubuntu 24.04, with SSH by key, as root or as a user with sudo.
- Internet access for GitHub, the Hermes and Claude Code installers, `images.linuxcontainers.org`,
  Debian mirrors and the model provider.
- About 0.4 GB RAM and 3 GB disk per idle instance, plus what its agent, Docker and Claude Code
  use.

With a sudo account, prefix the commands with `sudo` (`sudo thinny …`, `sudo incus …`).

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
   Python 3), copies thinny to `/usr/local/lib/thinny` and links the `thinny` command to
   `/usr/local/sbin/thinny`.
6. On the first run, `thinny init` asks for the settings below and writes them into
   `/etc/thinny/site.toml`, a copy of the commented example. Without a terminal, it copies the
   example and stops.
7. Once `site.toml` exists, it runs `thinny setup`: the Incus storage pool, the firewall, the
   update timer and the shared policy.

It does not add any instances.

</details>

On the first run, the installer asks for the model provider (OpenAI, Anthropic or another) and
model, the Webapp language, the internet interface and how users will open the Webapps. It
suggests the interface and LAN address it detects and the language pack it finds in Hermes's
catalog, shows a summary, and sets up the server once you confirm. Everything else keeps the
example's defaults: change them with `thinny config edit`.

Without a terminal, it stops after copying the example. Edit `/etc/thinny/site.toml` (at least
`policy.model`, `network.interface` and `access.lan_address`, or `""` and another
`access.default`), then run `sudo sh /usr/local/src/thinny/install.sh`.

Rerunning `install.sh` is safe. `thinny update` updates thinny itself too (see [Updates](#updates)).
A changed model guard loads at each gateway's next restart (`thinny instance restart`).

## Settings

Every key of `/etc/thinny/site.toml` is commented in [`site.example.toml`](site.example.toml).

```sh
thinny config edit    # edit a copy, check it, then apply it
thinny config apply   # apply after editing the file another way
```

With sudo, name your editor in the command: `sudo EDITOR=vim thinny config edit` (sudo drops
`EDITOR`). Avoid `sudo -E`: it also keeps your `HOME`, where Incus then leaves root-owned files.

Applying restarts gateways one by one where needed. `policy.language`, `port_range` and the
`[webapp]` look apply only to instances added afterwards. A new `language_pack` reaches existing
instances with their next Hermes update. Without the model lock, `model_provider` and `model`
are only what `instance add` suggests, and `reasoning_effort` where new instances start; turning
the lock on removes the models users chose.

## Instances

```sh
thinny instance add alice               # about 7 minutes; asks how users open the Webapp and the model
thinny instance login alice             # sign in to a model provider; asks which
thinny instance login alice --provider anthropic   # or name it: openai | anthropic | <Hermes provider id>
thinny instance list
thinny instance access alice tailscale  # lan | tailscale | url=<URL> | local
thinny instance limits alice --cpu 4 --memory-mb 8192 --disk-gb 50
thinny instance lock alice [model ...]  # only these models (default: the one it uses)
thinny instance unlock alice            # its users choose, even with the site's lock
thinny instance reset-password alice
thinny instance disable alice           # stop it for good, keep its data
thinny instance restart [alice ...]
thinny instance move alice bob --to admin@new-server   # or --all; experimental
```

- `add` prints the Webapp address, username and password. **The password is shown only
  once.** If `add` fails, fix the cause and run it again: it continues where it stopped.
- Run `login` before the hand-over. Enter takes the provider `alice` uses; with the model lock,
  `login` signs in to the lock's provider without asking. Whoever uses `alice` opens the link and
  signs in with their own account within about 15 minutes; a provider without such a sign-in asks
  for their API key instead. Signed in to another provider, they choose its models in the
  Webapp's model picker (Anthropic: *Claude Subscription DirectSDK (Experimental)*).
- The phone plugin stays idle until it is set up in the Webapp (Messaging → Hermes 3310), then
  with `~/.hermes/plugins/hermes-3310/install-asterisk.sh` and a gateway restart. See its README.
- `add` takes the same `--cpu`, `--memory-mb` and `--disk-gb`. Limits an instance was not given
  follow `[container]` in `site.toml`, also when it changes. `limits` applies at once, even to a
  running instance.
- `lock` keeps the provider `alice` uses and allows the models given, the first by default; it
  pins effort and plugins as the site's lock does. `unlock` leaves `alice` on the model it was
  locked to, for its users to change. Either stays whatever `policy.model_lock` says. The model
  picker keeps showing every model; the model guard refuses the others.
- Names are 2-32 lowercase letters, digits and `-`, starting with a letter. Names and ports are
  never reused, and a disabled instance cannot be re-enabled.
- Shell in a container: `sudo incus exec alice -- runuser -l alice` (or `-- bash` for root).
- **`move` is experimental:** it has not moved an instance between two real servers yet.
  Snapshot both servers first.
- `move` takes whole containers over SSH to another server that runs thinny, one at a time, each
  down for a few minutes. Everything inside comes along: data, sign-ins, the Webapp password,
  the model (unless the new server's model lock pins it), language, look and limits; its own
  `lock` or `unlock` does not: it follows the new server's `policy.model_lock`. The new
  server gives it its own port, address, policy and packages: `lan`, `tailscale` and `local`
  instances get a new address; for `url` ones, point your DNS or tunnel at the new server. The
  old copy stays here, disabled.
- Before a move, run `thinny update` on both servers: they need the same Hermes release. Each
  needs room for one container archive. SSH must reach the new server as root, or as an account
  with sudo without a password. It asks nothing, so connect once as root by hand to accept the
  host key (`sudo ssh admin@new-server true`). sudo hides your SSH agent: thinny uses root's key
  unless you run `sudo --preserve-env=SSH_AUTH_SOCK thinny instance move ...`.

## Access

| Method      | Address users open                           | Your part |
| ----------- | -------------------------------------------- | --------- |
| `lan`       | `https://<lan_address>:<port>/`              | trust Caddy's certificate on office PCs |
| `tailscale` | `https://<machine>.<tailnet>.ts.net:<port>/` | the server and users' devices on the tailnet, with MagicDNS and HTTPS certificates |
| `url=<URL>` | your URL                                     | a proxy or tunnel on this server, serving HTTPS and forwarding to `http://127.0.0.1:<port>`, or one tunnel for all (below) |
| `local`     | `http://<name>.localhost:<port>/`            | `ssh -L <port>:127.0.0.1:<port> <you>@<server>` |

Use HTTPS for network addresses: on plain HTTP, browsers turn off the Webapp's clipboard,
microphone, notifications and the Bot Screen. `<name>.localhost` through SSH keeps them.

**LAN certificate:** thinny runs Caddy with its own certificate authority and owns
`/etc/caddy/Caddyfile`. It copies the root certificate to `/etc/thinny/office-root.crt`, readable
by any account. Install it as trusted on each office PC (on Windows:
`certutil -addstore Root office-root.crt` as administrator):

```sh
scp <you>@<server>:/etc/thinny/office-root.crt .
```

**One tunnel for every `url` instance:** set `access.tunnel_port` (e.g. `8080`) and run
`thinny config apply`. Caddy then serves every `url` instance at `http://127.0.0.1:8080`, by the
host and path of its address. Set up the tunnel once, e.g. with Cloudflare:

1. `cloudflared` on this server, with a tunnel route from `*.example.org` to
   `http://127.0.0.1:8080` and a proxied DNS record `*.example.org` pointing to the tunnel. The
   free certificate covers one level: `jirka.example.org`, not `jirka.hermes.example.org`.
2. Cloudflare Access in front, e.g. with an email code. A policy can cover a single path.

Then an instance needs only its address:

```sh
thinny instance add jirka --access url=https://jirka.example.org/                 # a host of its own
thinny instance add office-accounting --access url=https://office.example.org/accounting/  # or a path
```

A host has one instance on its root or one on each path, never both. Instances on one host share
the browser's origin: a bug or a hijacked agent in one Webapp could act in another signed in on
the same browser. Share a host only within one organisation.

## Updates

Every night at `hermes.update_time`, thinny first updates itself: it pulls the clone it was
installed from (`git pull --ff-only`) and, on a new commit, runs its `install.sh`, which also
applies `site.toml`. If that fails, the run stops there. A private clone needs read access
without a password, such as a deploy key.

Then it installs the latest stable Hermes release once it is `min_release_age_hours` old. It
updates one instance at a time and stops at the first failure; the next run finishes the job.
With `auto_update = false` the nightly run leaves thinny as it is and only reports a new Hermes
release. The result is
in `/var/lib/thinny/update-status.json`, the log in `/var/log/thinny.log`. Nightly runs
also deliver new built-ins unless `auto_update = false`. Every non-dry run that gets past the thinny update updates Claude Code.

```sh
thinny update             # thinny and Hermes now, ignoring the minimum age and auto_update
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

Both exit with status 1 when anything fails, so monitoring can use them. Over SSH, a sudo account
that needs a password cannot run them unattended (`sudo -n thinny doctor` fails). Both only read,
so allow exactly these two without a password, with `sudo visudo -f /etc/sudoers.d/thinny-checks`:

```
<you> ALL=(root) NOPASSWD: /usr/local/sbin/thinny status, /usr/local/sbin/thinny doctor
```

## Uninstall

```sh
thinny uninstall
```

It lists the instances and asks you to type the server's hostname. Then it deletes every
instance's container with all its data, and everything thinny set up: the update timer,
`tailscale serve` of the instances' ports, thinny's Caddyfile (Caddy is disabled), the Incus profile
and storage pool, the firewall table, `/etc/thinny`, `/etc/hermes`, `/var/lib/thinny`, the log,
`/etc/sudoers.d/thinny-checks`, the clone in `/usr/local/src/thinny` and thinny itself. **There is
no undo.** If a step fails, fix the cause and run it again: it continues.

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
