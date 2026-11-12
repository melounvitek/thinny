# hermes-manager

Turns a Debian 13 server into a shared [Hermes Agent](https://github.com/NousResearch/hermes-agent)
server for a small organisation, such as an office of a dozen people who are not programmers.
The server gets one central Hermes install and one central policy. Each person gets their own
isolated Linux account with a private Hermes Webapp.

Status: in development. Design notes and test-server findings: [`PLAN.md`](PLAN.md).

What you get:

- **One Hermes install** in `/opt/hermes`, which everyone uses read-only.
- **One central policy** in `/etc/hermes` (Hermes "managed scope"). It sets the provider, the
  default model, the allowed models, the reasoning effort, approvals, plugins and the sandbox.
  People cannot change it from the Webapp or their own settings.
- **One Linux account per person**, with their own Hermes home, model-provider login, gateway
  and password-protected Webapp.
- **Agent tools in rootless Podman sandboxes.** There is no Docker daemon.
- **A firewall table** that keeps people out of each other's local services and blocks the
  networks you choose.
- **Four ways to open the Webapp**, chosen per person: office network (HTTPS), Tailscale, your
  own address, or an SSH tunnel.
- **An optional nightly Hermes update** that restarts gateways one at a time.
- **`hermes-manager check`**: isolation and health checks, each with a fix.

## What is and is not protected

**Protected: people from each other.** Every person has their own Linux account. Their
chats, files, model-provider login, Webapp and sandbox belong to that account, and the
operating system keeps other accounts out:

- home directories are private (mode 700) and `/proc` hides other accounts' processes
  (hidepid, a prerequisite);
- a firewall table rejects any loopback connection to a person's programs from another
  account (only root and the Webapp proxy may connect), so nobody can reach another person's
  Webapp, browser or other local services. A person's Webapp port only ever reaches that
  person's gateway, so nobody can take it over while their gateway is down;
- people's accounts and their sandboxes cannot reach the networks you block. When Tailscale
  is installed, the tailnet is blocked automatically, except its DNS;
- every Webapp asks for a username and password.

**Not protected:**

- **From the server admin.** Root can read everything on the machine that runs the agent.
- **From the model provider.** Conversations go to the provider you configure.
- **The policy against a determined person.** The pinned model, approvals and sandbox
  settings cannot be changed from the Webapp or the person's settings. But a signed-in
  person has a shell on the server as their own account (the Webapp's terminal) and can
  get around the policy. `hermes-manager check` reports the bypasses it can see in their
  files. Their account is still isolated from everyone else.
- **A person from their own agent.** An agent misled by a web page can do whatever that
  person's account can do. Most tools run in a rootless Podman sandbox, but some run on
  the host as the person.
- **Plain HTTP over the network.** It sends passwords and chats unencrypted. LAN and Tailscale
  access use HTTPS, and local access goes through an SSH tunnel. If you use your own address,
  keep it on HTTPS too.

## Requirements

- Debian 13 (`install.sh` warns on anything else) and root access over SSH with a key.
- Internet access from the server for GitHub, the Hermes installer, `docker.io` (the sandbox
  image) and the model provider.
- Hermes from its latest stable release. Tested with Hermes v0.23.
- Rough sizing, measured on a test server: 0.8 GB RAM per idle person and about 5 GB disk per
  person (a Python environment plus a 4 GB sandbox image), plus a few GB for the shared install.
  Twelve people need about 10 GB RAM and 60 GB disk.
- One model-provider account per person. The default provider, `openai-codex`, signs in with
  ChatGPT. Sharing one subscription among several people is against the provider's terms.

## Prerequisites (your part)

`hermes-manager check` verifies these and prints the fix for anything missing. Set them up
before you add people.

### Required (`FAIL` in `check`)

**Hide other accounts' processes.** Without this, every account sees everyone's processes
and command lines.

```sh
groupadd --system proc-viewers
getent group proc-viewers        # note the gid
```

Add this line to `/etc/fstab`, using that gid:

```
proc /proc proc defaults,hidepid=invisible,gid=<gid> 0 0
```

systemd-logind must still see every process. Create
`/etc/systemd/system/systemd-logind.service.d/hidepid.conf`:

```ini
[Service]
SupplementaryGroups=proc-viewers
```

```sh
systemctl daemon-reload && systemctl restart systemd-logind && mount -o remount /proc
```

**A root-only Tailscale socket** (only if Tailscale is installed). Without it, every account
can read the tailnet (devices, addresses) and the `tailscale serve` configuration with
everyone's Webapp addresses. Create `/etc/systemd/system/tailscaled.service.d/10-root-only.conf`:

```ini
[Service]
RuntimeDirectoryMode=0700
ExecStartPost=/bin/chmod 0600 /run/tailscale/tailscaled.sock
```

```sh
systemctl daemon-reload && systemctl restart tailscaled
```

### Recommended (`WARN` in `check`)

**Key-only SSH, reachable only from a private network** (office LAN, VPN or Tailscale). Create
`/etc/ssh/sshd_config.d/10-keys-only.conf`:

```
PasswordAuthentication no
KbdInteractiveAuthentication no
```

Then run `systemctl reload ssh`. `check` verifies only that SSH is key-only. Limiting where
SSH can be reached from is the job of your input firewall.

**An input firewall that drops by default** (nftables or ufw). With LAN access, it must let
office PCs reach the Webapp ports (`webapp.port_range`, 9120-9199 by default). Example
`/etc/nftables.conf`:

```
#!/usr/sbin/nft -f
# No "flush ruleset": it would also remove Tailscale's and hermes-manager's tables.
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
}
```

Load it with `nft -f /etc/nftables.conf`.

> **Never run `systemctl restart nftables` on Debian.** Its stop step runs
> `nft flush ruleset`, which removes every table, including Tailscale's. A config file that
> starts with `flush ruleset` does the same, and Debian's default file does. If it happens,
> restore the tables with `systemctl restart hermes-manager-firewall tailscaled`.

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
git clone <repository URL> /root/hermes-manager
cd /root/hermes-manager
sh install.sh
```

The first run does three things and stops:

1. installs the packages (Podman, passt, nftables, ...);
2. installs the tool into `/usr/local/lib/hermes-manager`;
3. creates `/etc/hermes-manager/site.toml` from `site.example.toml`.

Edit `site.toml` (see [Settings](#settings)). At least set `policy.model`,
`policy.allowed_models` and `access.lan_address`. If nobody will use LAN access, set
`access.lan_address = ""` and choose another `access.default`. Then run the script again:

```sh
sh install.sh
```

The second run takes several minutes. It:

1. installs the latest stable Hermes into `/opt/hermes` with the official installer;
2. installs the `hermes` launcher and the sandbox wrapper;
3. installs the model-guard plugin and the language pack;
4. builds the Webapp;
5. installs the systemd units;
6. writes `/etc/hermes` and loads the firewall.

Rerunning `install.sh` is safe. It repairs what is missing, keeps all data and never updates
Hermes, because the official installer runs only when `/opt/hermes` has no install yet. If the
rerun changes `/etc/hermes`, it restarts the gateways one by one, like `apply`.

To update hermes-manager itself:

```sh
cd /root/hermes-manager && git pull && sh install.sh
```

## Settings

Edit `/etc/hermes-manager/site.toml`. Every key is commented in
[`site.example.toml`](site.example.toml).

| Section     | Holds |
| ----------- | ----- |
| `[hermes]`  | the nightly update: `auto_update`, `update_time`, `min_release_age_hours` |
| `[policy]`  | `model_provider`, the default `model`, `allowed_models` (enforced by the model-guard plugin), `reasoning_effort`, `approvals` (`manual` or `smart`), the UI `language` and its `language_pack`, `model_guard_message` |
| `[sandbox]` | the sandbox image and its CPU, memory and disk limits |
| `[webapp]`  | `port_range`, and the Webapp look for new people (`theme`, `text_scale`, `interface`, `known_models`) |
| `[access]`  | the `default` access method, `lan_address`, `proxy_users` |
| `[network]` | `blocked_networks`: networks people's agents may not reach |

After editing, run:

```sh
hermes-manager apply
```

`apply` does the following:

- rewrites `/etc/hermes`, the timer, the plugins and the firewall table;
- updates each person's Webapp address;
- updates the Caddy sites and `tailscale serve`;
- restarts gateways one by one. It restarts every gateway when `/etc/hermes` changed, and
  otherwise only those whose Webapp address changed.

Notes:

- Unknown keys, placeholder values, and a `model` that is not in `allowed_models` are errors.
- Some values apply only to people added afterwards: `policy.language`, the `[webapp]` look,
  the models shown in the model picker, and `port_range`. Existing people keep theirs.
  However, a model removed from `allowed_models` is blocked for everyone at once.
- `language_pack` is a plugin name from the Hermes plugin catalog, e.g. `hermes-lang-cs`. It is
  installed at the commit that the installed Hermes catalog pins.

## People

### Add a person

```sh
hermes-manager add-person alice
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
letter and must not already be the name of a Linux account.

`add-person` takes a few minutes. It:

1. picks the next free Webapp port;
2. creates the Linux account (home 700, linger on) and its firewall rule;
3. builds the person's own Hermes Python environment (`hermes pm repair`);
4. configures the Webapp on `127.0.0.1:<port>` with its public address and a generated
   password;
5. sets the UI language and look;
6. publishes the Webapp;
7. starts the gateway and checks that it answers.

At the end, it prints the address, the username (the person's name) and the password. **The
password is shown only once.** If the command fails halfway, fix the cause and run it again.
It continues where it stopped.

### Sign in and hand over

```sh
hermes-manager login alice
```

This command signs the person in to the model provider with a device code (ChatGPT for
`openai-codex`). It prints a link and a code. The person opens the link, signs in to *their
own* account and enters the code within about 15 minutes. The tokens stay in their account
(`~/.hermes/auth.json`). Never copy that file between people.

Run `login` before the hand-over. Without it, the Webapp's first screen invites the person to
connect other providers. Then give the person the address, username and password. With LAN
access, their PC also needs the office certificate.

### Other commands

```sh
hermes-manager list                      # people, state, access, port, gateway, address
hermes-manager access alice tailscale    # change the access method (lan | tailscale | url=<URL> | local)
hermes-manager password alice            # new Webapp password, shown once
hermes-manager remove-person alice       # disable the person
```

- `access` restarts the person's gateway, and the old address stops working.
- `password` restarts the person's gateway, and open browsers must sign in again.
- `remove-person` disables the person and keeps their data:
  - it stops and disables the gateway, removes the sandboxes and turns linger off;
  - it ends the person's sessions and locks the account;
  - it removes their Caddy site or `tailscale serve` entry and regenerates the firewall.

  Their home (`/home/<name>`) stays. Names, accounts (uids) and ports are never reused, and a
  removed person cannot be re-enabled. Keep the locked account, because deleting it would
  free its uid for someone else.

## Access

Each person has one access method, because a Webapp only answers at its own address.

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

hermes-manager installs Caddy the first time someone gets LAN access. Each LAN person gets a
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
- **hermes-manager owns `/etc/caddy/Caddyfile`** once anyone uses LAN access. It rewrites the
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
hermes-manager add-person alice --access url=https://alice.example.org/
```

- Run the proxy or tunnel client on this server and point it at `http://127.0.0.1:<port>`.
  `hermes-manager list` shows the port.
- The firewall lets only root, Caddy and the accounts in `access.proxy_users` connect to
  people's Webapps. If your proxy runs as another account (e.g. `cloudflared`), add that
  account and run `hermes-manager apply`. Otherwise the proxy gets refused and returns 502.
- Keep the address on HTTPS.

### Local only (`local`)

The Webapp is not reachable from the network. Open an SSH tunnel as root, then browse to
`http://alice.localhost:<port>/`:

```sh
ssh -L <port>:127.0.0.1:<port> root@<server>
```

The tunnel must run as root, because the firewall refuses connections from other accounts.

## Updates

`hermes-manager-update.timer` runs `hermes-manager update` every night at
`hermes.update_time`. If the server is off at that time, the update waits for the next night.

1. It asks GitHub for the latest stable Hermes release.
2. It stops if `/opt/hermes` already runs that release (`up-to-date`), or if the release is
   younger than `min_release_age_hours` (`waiting`).
3. With `auto_update = false`, it only reports the release (`available`).
4. Otherwise it updates:
   1. it backs up `/etc/hermes` and the local plugins to `/var/backups/hermes-manager/` and
      keeps the newest 14 backups;
   2. it runs `hermes update` on the shared install while the gateways keep running;
   3. it restores permissions and the local plugins (model-guard, language pack), rebuilds the
      Webapp and checks that the install is at the new release;
   4. it runs `hermes pm install --extra all` as each person to bring their Python
      environment to the new release's dependencies;
   5. it restarts the gateways one by one. After each restart, it checks that the gateway is
      active, the Webapp answers and model-guard is enabled. **It stops at the first failure.**
      The remaining gateways keep running the old code until they are restarted;
   6. it pulls each person's sandbox image. A new image replaces that person's sandboxes.
      Otherwise, it removes sandboxes that lost their network. Hermes recreates removed
      sandboxes on next use, and old images are pruned.
5. It writes the result (`up-to-date`, `waiting`, `available`, `ok` or `FAILED`) to
   `/var/lib/hermes-manager/update-status.json`. If the OS needs a reboot, it adds that to the
   status. It never reboots.

```json
{"time": "2026-01-31T03:04:12", "result": "ok", "detail": "updated to <tag>"}
```

Run it by hand:

```sh
hermes-manager update --check    # report what would happen; changes nothing, writes no status
hermes-manager update --now      # update now, ignoring the minimum age and auto_update = false
hermes-manager restart [alice ...] # restart gateways one by one (all, or the named people)
```

- **Log:** `/var/log/hermes-manager.log` (all commands), and for the nightly run also
  `journalctl -u hermes-manager-update.service`.
- **There is no rollback.** Hermes databases cannot be downgraded. The only way back is your
  own snapshot of the whole server, taken before `update_time` (see
  [`extras/netcup-snapshot`](extras/netcup-snapshot/)).
- **Night-time jobs can be cut off.** A gateway restart waits for a reply in progress, but it
  cuts off scheduled jobs and background tasks. Keep people's scheduled jobs away from
  `update_time`.
- **Interrupted work is finished later.** If an update stops halfway, the next run (or
  `hermes-manager update --now`) finishes it first, without waiting for the minimum age.
  Gateways that `apply`, `update` or `restart` could not restart are remembered and restarted
  by the next of these commands.
- **One run at a time.** hermes-manager runs one command at a time. During the update, other
  commands that change things refuse to start. If such a command is still running at
  `update_time`, that night's update fails.

## Checks

```sh
hermes-manager check
```

`check` prints one line per result (`OK`, `WARN` or `FAIL`), with a `fix:` line where it
helps. It exits with status 1 when anything fails, so you can use it from monitoring.

- **Install:**
  - `/opt/hermes` has mode 711;
  - the `hermes` launcher and `hermes-podman` are present;
  - the Webapp is built and the plugins are present;
  - `/etc/hermes` matches `site.toml`;
  - the firewall unit is active and the update timer is enabled;
  - the last nightly update result: `FAIL` if it failed, `WARN` if it is missing or older
    than 48 h.
- **Prerequisites:** everything in [Prerequisites](#prerequisites-your-part).
- **Firewall:** the `inet hermes_manager` table is loaded with a rule for every person.
- **Office network:** Caddy is running when anyone uses LAN access.
- **Listeners:** no person's program listens on a network address. The firewall guards
  loopback only.
- **Per person:**
  - the Webapp port is closed to other accounts;
  - the gateway is active, the Webapp answers and the public address is as expected;
  - no bypass of the policy in the person's files: `HERMES_MANAGED_DIR`,
    `HERMES_BUNDLED_PLUGINS` or `HERMES_ENABLE_PROJECT_PLUGINS` in `.env` or the gateway unit,
    their own `model-guard` plugin, or a gateway unit that starts something else;
  - running sandboxes reach the internet and run in `hermes-sandboxes.slice`. Outside that
    slice, a sandbox loses its network when the gateway restarts.

## Files on the server

| Path | What |
| ---- | ---- |
| `/etc/hermes-manager/site.toml` | your settings |
| `/etc/hermes-manager/hermes-manager.nft` | the firewall table (generated) |
| `/var/lib/hermes-manager/people.json` | the people registry: account, uid, port, access, state |
| `/var/lib/hermes-manager/update-status.json` | the last nightly update result |
| `/var/log/hermes-manager.log` | the log |
| `/var/backups/hermes-manager/` | `/etc/hermes` and local plugins, saved before each update |
| `/opt/hermes/` | the shared Hermes install (code in `hermes-agent/`), mode 711 |
| `/etc/hermes/` | managed scope: `config.yaml`, `.env` (world-readable, no secrets), `allowed-models.txt`, `model-guard-message.txt` (generated) |
| `/usr/local/bin/hermes`, `/usr/local/bin/hermes-podman` | the launcher and the sandbox wrapper |
| `/usr/local/lib/hermes-manager/`, `/usr/local/sbin/hermes-manager` | the tool |
| `/etc/systemd/system/hermes-manager-*`, `user@.service.d/hermes-manager.conf` | the firewall unit and the nightly update service and timer |
| `/etc/caddy/Caddyfile` | the LAN sites (generated, when used) |
| `/home/<name>/.hermes/` | a person's Hermes home: chats, settings, Webapp sign-in, provider login |

Generated files are rewritten by `apply`, so do not edit them by hand.

## Extras

Optional tools, off unless you set them up:

- [`extras/netcup-snapshot`](extras/netcup-snapshot/): takes a nightly snapshot of a netcup VPS
  before the update, run from another machine.
- [`extras/nightly-watch`](extras/nightly-watch/): runs a morning check of the update and
  snapshot status files over SSH, and cron mails you on failure.

## Rejected options

- **Docker:** membership in the docker group equals root.
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
