# thinny: plan

Status: one Incus container per instance, implemented (sections 6-8) and tested on the test server
from a rebuilt playground (round 7, 2026-11-13). The README is the user documentation; this file
keeps the design and what we learned.

## 1. Goal

Turn a Debian 13 or Ubuntu (26.04, 24.04) server into a shared Hermes Agent server for a small organisation
of non-programmers, with one command, and add instances
with a second command (`thinny`).

The design below grew out of a hand-built test server (section 4). This project turns
it into a reproducible, public tool.

## 2. Requirements

In priority order.

1. **Instance isolation is non-negotiable.** Every instance has its own Incus system
   container (Debian 13 with systemd): its own uid range on the host, filesystem, network
   namespace and processes. The host has no instance accounts. One instance must never reach
   another instance's data (files, chats, credentials, Webapp, browser) nor see other
   instances' names or processes. The containers and our nft table enforce it: a container
   reaches the internet only, not other containers, host services, private networks, the
   tailnet or the blocked networks. Hermes's own settings (managed scope) are a guardrail
   inside a container. The model provider seeing traffic is accepted. There is no file
   sharing between instances through Hermes.
2. **One release and one policy.** One Hermes release for all instances, installed in every
   container with the official installer at the recorded commit, and one config
   (`/etc/hermes`, Hermes "managed scope") mounted read-only into every container. One
   nightly update for all instances, one instance at a time.
3. **Central policy:** approvals mode and terminal backend; with the model lock
   (`policy.model_lock`, off by default) also pinned model and reasoning effort, model
   allowlist enforced by a plugin and plugin list. Users cannot change it from the
   Webapp or their settings. It is a guardrail, not enforcement: users are root in their
   container and can get around it. `doctor` reports it when they do.
4. **Agent tools run in the instance's container** (`terminal.backend: local`); no sandbox
   image, no nested Podman. The instance's account has passwordless `sudo`, so users and their
   agent can `apt install`. Docker is installed inside (the phone plugin's Asterisk), Tailscale is
   possible (nesting, `/dev/net/tun`). No Docker daemon on the host (docker group = root).
   Built into every instance: Claude Code with the Claude subscription plugin and the hermes-3310
   phone plugin.
5. **Built-in Hermes Webapp per instance** on loopback, always behind sign-in. How users
   reach it is chosen per instance when it is added (section 6.6). The tool assumes
   nothing about the site's network.
6. **Unattended operation:** optional automatic nightly Hermes update with careful one-by-one
   gateway restarts; everything comes back after a reboot. OS updates and the reboot
   window are the admin's (section 6.3).
7. **Non-programmer users:** UI language and Webapp look preset per instance.
8. **Admin stays root.**
9. **Public repository from day one** (section 3).

Non-goals: hiding data from the server admin (impossible while the admin is root on the
machine that runs the agent), multi-server fleets, a web UI for the admin, hardening the
base server (a documented prerequisite that `doctor` verifies), making the policy
tamper-proof against users who are root in their container.

## 3. Public from day one

- No personal or site data in the repo: no IPs, hostnames, tailnet names, e-mails,
  organisation names, tokens, provider server ids. Site data lives on the server in
  `/etc/thinny/site.toml`; the repo ships `site.example.toml` with placeholders.
- Secrets are generated on the server (Webapp sign-in) or entered by the admin on the
  server (ChatGPT device login). Never logged, never committed, never printed except the
  one-time hand-over of an instance's Webapp password.
- Code, comments, docs and CLI output in English. User-facing strings the server shows
  users (UI language, the model-guard message) come from settings.
- Opinionated defaults (pinned model, approvals `smart`, browser on) are settings with
  defaults, not hardcoded.
- Provider-specific extras (netcup snapshots, morning watchdog) live in `extras/`, off
  unless configured.
- `local/` is git-ignored and holds the hand-built prototype for reference
  (`local/prototype/`: config and scripts pulled from the test server, the snapshot and
  watchdog scripts). It contains site data and must never be committed.

## 4. What we know

Hermes v0.23.1, Debian 13, rootless Podman 5.4, Incus 6.0.4. Sources are marked: **[proto]**
seen on the hand-built test server, **[tested]** tried on the test server during the review,
round 0 (2026-11-12) and the container spike (2026-11-12/13), **[src]** read in the Hermes
source (also present in v0.23.1 unless noted), **[static]** read in the source but not run.

The subsections from Layout to Capacity describe the previous design (one Linux account per
user on a shared `/opt/hermes` install, sandboxes in rootless Podman) and are kept as
history; their findings about Hermes itself (managed scope, model guard, Webapp, login,
restarts) still hold unless the last subsection says otherwise.

The prototype ran one Linux account with two Hermes homes. Round 0 added two user
accounts on a separate v0.23.0 install, updated it to v0.23.1 and rebooted the server.

### Layout
- Hermes installed with the official installer into `/opt/hermes` (`HERMES_HOME` of the
  install, code in `/opt/hermes/hermes-agent`); `/usr/local/bin/hermes` is a 2-line
  launcher. Users borrow the code read-only **[proto]**.
- **`/opt/hermes` must be mode 711.** Hermes chmods its `HERMES_HOME` to 700 on almost every
  command (`_secure_dir`), so root's `hermes` with `HERMES_HOME=/opt/hermes` breaks every
  user's `hermes` (running gateways keep working, so it goes unnoticed) **[proto]**.
  `HERMES_HOME_MODE=0711` in root's environment keeps it, also through `hermes update`
  **[tested]**. `auth.json` saves chmod the parent regardless **[static]**.
- The official installer leaves a fresh install at 700 and does not build the Webapp; a
  user cannot build it into the root-owned checkout. After installing: fix permissions
  and run `hermes webapp --build-only` as root (~2 min, ~1 GB in `.build`) **[tested]**.
- Run as root, the installer publishes launchers into the invoking `HOME`'s `~/.local/bin`;
  run it with a separate `HOME` **[tested]**.
- The host-side `cua-driver` fails to install (missing `libXi`); harmless, computer_use
  runs in the sandbox **[tested]**.
- **One user = one Linux account with Hermes's default home `~/.hermes`.** The gateway
  unit is the standard `hermes-gateway.service` (`hermes gateway install`, needs linger)
  **[tested]**. This is the topology upstream supports: one gateway per OS user.
- **Each home builds its own Python venv** via `hermes pm repair` (15 s, 440 MB for a new
  account **[tested]**; ~0.9 GB per generation in newer source **[static]**). It cannot be
  shared across accounts.
- After a code update, the automatic resync at launch is refused for a root-owned
  checkout ("refusing to update ... owned by uid 0") and Hermes keeps running on the old
  dependencies. `hermes pm repair` does **not** fix that: it rebuilds the recorded (old)
  dependency set, so users ran new code on old dependencies (a package v0.23.1 removed was
  still loaded) and kept printing "venv out of sync with uv.lock". `hermes pm install --extra
  all` as the user syncs to the new lock in their own home, skips the root-owned tool store,
  removes the warning and is a no-op when current **[tested]**. `hermes update` leaves
  `install-stamp.json` at 600, so user commands fail until permissions are fixed **[tested]**.
  For a new user `pm repair` builds a current environment **[tested]**.
- Old environment generations (~130 MB each) pile up in users' homes; `hermes pm gc` as a
  user fails on the root-owned tool store lock **[tested]**.
- Account home dir 700, linger on. Rootless Podman images are per account (~4 GB each).
- `/proc` mounted `hidepid=invisible` with a `proc-viewers` group for logind **[proto]**.

### Managed scope (`/etc/hermes`)
- `config.yaml` values win over each user's config **[src]**: `model.provider`,
  `model.default`, `agent.reasoning_effort`, `plugins.enabled`, `plugins.disabled: []`,
  `terminal.backend: docker` with the Podman image and limits, `security.redact_secrets`,
  `approvals.mode: smart` **[proto]**.
- Managed lists **replace** the user's list wholesale, so `plugins.enabled` must list every
  plugin that must be on **[src]**.
- `.env` is world-readable: never secrets. It loads last and wins over the user's `.env`
  **[src]**. It sets `HERMES_DOCKER_BINARY` **[proto]** and should also pin
  `TERMINAL_ENV=docker`: otherwise a user's `.env` can beat the managed `terminal.backend`
  **[static, reproduced with throwaway homes]**.
- Ways around it for a user with a shell **[src/static]**: `HERMES_MANAGED_DIR` or
  `HERMES_BUNDLED_PLUGINS` in their own `.env` (read before the managed dir is resolved),
  `--safe-mode` (skips all plugins, model-guard included), `/yolo`, `/model` and
  `/reasoning` per session, a same-named plugin in `~/.hermes/plugins`, editing their own
  gateway unit. `doctor` looks for these.
- `approvals.mode` must not be `off`: with approvals off, computer_use runs "unrestricted"
  and looks for `cua-driver` on the host instead of in the sandbox, which breaks the Bot
  Screen **[proto, src]**. Pin `bot_desktop.placement: terminal` so the browser and
  computer_use stay in the sandbox **[static]**.

### Model guard
- Bundled plugin `model-guard` copied into `/opt/hermes/hermes-agent/plugins/`, listed in
  `.git/info/exclude`, enabled from the managed scope. Its `pre_llm_call` hook blocks any
  turn whose model is not in `/etc/hermes/allowed-models.txt` (fail closed when empty)
  **[proto]**. There is no built-in model allowlist for the main agent **[src]**.
- The hook gets the model but not the provider or effort, and does not see a fallback
  model chosen mid-turn **[src]**.

### UI language
- Catalog plugin `hermes-lang-cs` (pinned by commit sha) in the bundled plugins dir +
  managed `plugins.enabled`; `display.language cs` per home **[proto]**. Some placeholder
  strings stay English (not in the pack).

### What runs where
- In the sandbox: terminal, file tools, code execution, sandbox Chromium, computer_use
  (with placement `auto`/`terminal`) **[src]**.
- On the host as the user's account: the Webapp's host terminal and `shell.exec`, MCP
  stdio servers, image/TTS tools, cron script jobs, Lightpanda **[src]**. Hence policy is a
  guardrail and isolation rests on the Linux account.

### Sandboxes
- **Sandbox network dies on gateway restart** unless the container is started outside the
  gateway's cgroup: `HERMES_DOCKER_BINARY=/usr/local/bin/hermes-podman`, a wrapper that runs
  `podman run|start|restart` via `systemd-run --user --scope` in `hermes-sandboxes.slice`
  **[proto]**. Hermes detects Podman when the binary's basename contains "podman" **[src]**.
- Sandboxes start on first tool use, not at boot.
- A container whose conmon died lands in "Exited (-1)" after the first `rm -f`; a second
  `rm -f --ignore` removes it **[proto]**.
- The sandbox image is pulled only when missing, so a floating tag is never updated
  **[src]**.
- Chromium in a sandbox was once found stopped (state T); `kill -CONT` fixed it. Root
  cause unknown **[proto]**.

### Webapp
- Per home: `webapp.enabled`, `webapp.host 127.0.0.1`, `webapp.port` (9120-9199),
  `webapp.public_url`, basic auth via `HERMES_DASHBOARD_BASIC_AUTH_USERNAME/PASSWORD/SECRET`
  in the home's `.env` (`_PASSWORD_HASH` also exists) **[src]**. Generate fresh values per
  user.
- **`public_url` is required** **[tested, docs]**: Hermes answers only requests for the bound
  address or `public_url` ("Invalid Host header" otherwise), and only `public_url` turns on
  username/password sign-in on a loopback bind. Without it the only way in is a one-use
  link from `hermes webapp` on the server, valid 60 s. A wrong password stays on the login
  page **[tested]**.
- **Plain HTTP on a non-loopback address loses** clipboard, microphone, `crypto.subtle`
  (Bot Screen), notifications **[tested, docs]**. HTTPS through Caddy with its local CA:
  login, real chat and clipboard work **[tested, with the certificate warning bypassed]**.
- A signed-in Webapp user can run any command as the account (host terminal) **[docs]**.
- Changing `webapp.*` needs a gateway restart **[docs]**.
- **Loopback is shared by all Linux accounts.** The prototype's nft output rule lets ports
  9120-9199 through only for the owner uid and root **[proto, tested]**, but every other
  loopback port stays open to all accounts.
- **Fix [tested]:** one generic rule per user instead of port lists. Output: `oif lo`
  new TCP gets `meta mark set meta skuid`. Input: `iif lo` new TCP whose receiving socket
  is in `user.slice/user-<uid>.slice` (`socket cgroupv2 level 2`) and whose mark is not 0,
  `<uid>` or a proxy account is rejected. Blocked a test listener for other
  accounts; owner and root still connect. nft resolves the cgroup path at load and refuses
  a missing one, so a loader rebuilds the rules from the existing user slices: a oneshot
  unit (`After=`/`PartOf=nftables.service`) at boot and a `user@.service` drop-in
  (`ExecStartPre=+`) before each user's manager starts. Survived a reboot, a
  `user@<uid>` restart and `systemctl restart nftables`. A proxy account not on the list
  gets 502 through the proxy. It replaces the Webapp port rule.
- Webapp look (light mode, text size, simple interface, visible models) is server-side
  `~/.hermes/ui-preferences.json`; applied from a template through
  `hermes_cli.ui_preferences.write_preferences`, run as the user with the home's venv
  Python **[proto]**. The template must include `known-models`. Some settings (e.g. the Bots
  plugin toggle) are browser localStorage only.
- Cookie names carry a hash of the home, so two Webapps on one hostname do not clash
  **[src]**.
- With an untrusted certificate (Caddy's local CA), the browser shows a warning; after
  "Advanced" -> "Proceed" login and all features work **[tested]**.
- A loopback `public_url` (`http://127.0.0.1:<port>/`) does **not** turn on password sign-in;
  the Webapp still asks for a one-use link. A one-use link through an SSH tunnel opened as
  root gets 403 (Hermes checks the connection belongs to the user's account). A
  `public_url` of `http://<name>.localhost:<port>/` through a root SSH tunnel works: login
  required, secure context, all features **[tested]**.
- Under a path: a proxy that strips `/<path>` and sends `X-Forwarded-Prefix: /<path>`, with
  `public_url` `https://<host>/<path>/`. Redirects, assets, sign-in, the WebSocket and a reload
  stay under the path; cookies get `__Secure-` and `Path=/<path>`, so two Webapps on one host
  stay signed in side by side. Over Caddy 2.6.2 with `trusted_proxies` for a stand-in of
  Cloudflare's edge (Secure cookies need its `X-Forwarded-Proto`, the per-client sign-in limit its
  `X-Forwarded-For`) **[tested, without a model login]**. Instances on one host share the
  browser's origin, and with it a few browser-side settings (e.g. `sidebar_state`).
- Without a ChatGPT login, the Webapp's first screen invites the user to connect other
  providers **[tested]**: do the login before the hand-over.
- `hermes -p <profile> webapp` does not lock a Webapp to one profile; profiles of one gateway
  share it **[docs]**.

### Network
- nft output rules: non-root accounts may not reach the tailnet (100.64.0.0/10,
  fd7a:115c:a1e0::/48) except MagicDNS port 53; root exempt (`meta skuid 0`) **[proto]**.
  Rootless Podman traffic leaves as the user, so sandboxes are covered.
- A sandbox reached no host port: loopback-bound Webapps, another account's port
  bound to all interfaces, SSH on the public and the Tailscale address; the internet works
  **[tested]**.
- Tailscale socket root-only (drop-in: `RuntimeDirectoryMode=0700`, chmod 600 on the
  socket) **[proto]**. Without it another account can read the whole tailnet (devices,
  addresses) and the `serve` config with every instance's URL, but cannot change it ("Access
  denied") **[tested]**: an information leak, not a hijack.
- **Debian's `systemctl restart nftables` runs `nft flush ruleset`** and removes every table,
  Tailscale's included; restarting `tailscaled` restores them **[tested]**. Never restart
  `nftables.service`; load our table with `nft -f`. Elements added at runtime are gone
  after a reboot, so everything is regenerated from the registry.
- nft changes on a remote server go with an auto-rollback timer (`systemd-run
  --on-active=240`) and are verified before persisting **[proto]**.

### Nightly update (prototype `hermes-nightly-update`, root timer 03:00)
- Plain `hermes update` restarts only gateways in its own scope; other accounts keep
  running old code **[src]**. Hence a custom script **[proto]**: wait until the latest stable
  release is 48 h old, back up `/etc/hermes` + local plugins, `hermes update --yes
  --no-gateway-restart` with `HERMES_HOME=/opt/hermes`, fix permissions, restore local
  plugins if missing, then restart gateways one by one and check unit active + Webapp
  2xx/3xx + model-guard enabled, stop at the first failure, then check every sandbox's
  network and remove dead ones. Status file + log.
- Corrections from the review:
  - `hermes gateway migrate --dry-run` is the preflight for merging profiles into one
    gateway, not a database check; drop it **[src]**. The downgrade guard checks only the
    updating home **[static]**; databases cannot be downgraded, so the snapshot is the only
    way back.
  - `hermes gateway restart` refuses without `--yes` when a reply, cron job or delegation is
    running and there is no terminal **[src]**. With `--yes` it still waits for a running
    reply but cuts off jobs and background tasks.
  - Homes were discovered from user-writable unit files; use the registry.
  - Rerunning the official installer on an existing checkout updates it to the latest
    stable release **[proto: installer source]**.
- Round 0 update v0.23.0 -> v0.23.1 with two user gateways running **[tested]**: `hermes
  update` 1:43 min; `/opt/...` stayed 711 with `HERMES_HOME_MODE`; local plugins survived;
  after fixing permissions and `pm repair`, `gateway restart --yes` per user came back with
  the Webapp answering and model-guard + language pack enabled. A restart without `pm
  repair` also came back, on the old dependencies. Not checked: whether `hermes update`
  rebuilds the Webapp bundle.
- After a reboot every gateway, Webapp, the firewall rules, hidepid and the Tailscale
  socket mode came back **[tested]**.

### Per-instance ChatGPT login
- `hermes auth add openai-codex --type oauth --no-browser` as the instance's account; device code,
  ~15 min expiry **[proto]**. Tokens in `~/.hermes/auth.json`, mode 600 **[src]**. Never copy
  `auth.json`: refresh tokens are single-use.

### Capacity (measured on the prototype)
- ~0.8 GB RAM per idle instance (gateway ~0.45 + sandbox ~0.35).
- Disk per instance: venv 0.45-0.9 GB + sandbox image ~4 GB.

### Incus containers (2026-11-12/13)
Debian 13, kernel 6.12, Incus 6.0.4 (`incus-base`), Hermes v0.23.0 -> v0.23.1. All
**[tested]**; "VM" marks what was tried in a local Debian 13 VM instead of the test server.

- Storage: a btrfs pool in a sparse loop file on the ext4 root; snapshots are instant;
  idmapped mounts, so nothing is chowned. A root disk `size` is a quota (`fallocate` of
  28 GB on 5 GB used: "Disk quota exceeded"); `limits.processes=2000` shows as `pids.max`.
  Plain `images:debian/13` + one `apt-get install --no-install-recommends` of the full
  package list: 92 s, so no template.
- Inside, no trace of others: hostname = own name, own users, processes and cgroup, `dmesg`
  blocked, lxcfs shows the container's own CPU and memory limits.
- Network: with a managed bridge (VM) containers pinged each other and resolved each other's
  names through its dnsmasq; `port_isolation` stopped the ping, but reverse DNS still leaked
  names with `dns.mode=none`. A routed NIC (`host_name` at most 15 characters) has no DHCP
  and no shared DNS. Inside, systemd-networkd needs a static file (`Address=<ip>/32`,
  `Gateway=169.254.0.1`, `GatewayOnLink=yes`, public DNS; the host's may be MagicDNS) or it
  drops the address.
- Firewall (the rules in 6.2, eth0 hardcoded then): from inside, the internet works; another
  container, host SSH on every address, Caddy, the monitoring agent, the host's tailnet
  address, MagicDNS and private ranges are blocked. A drop in any base chain is final: the
  admin's forward chain (`policy drop`) needed `iifname "thinny-*" accept` and
  `oifname "thinny-*" ct state established,related accept`.
- Anti-spoofing: Incus adds `iif "thinny-..." fib saddr . iif oif missing drop` per routed NIC
  (table `inet incus`) and `rp_filter=2`; a spoofed source address got no reply.
- An incus.service drop-in `ExecStartPre=/usr/sbin/nft -f <file>`: `systemctl restart incus`
  keeps containers and gateways running (same PID); with the file missing incus.service
  fails (fail closed). After a reboot the tables, `ip_forward`, containers, gateways, a
  container's own Tailscale and the Webapp over `tailscale serve` came back.
- Webapp: a proxy device on the host loopback (listener owned by incusd); Hermes sees
  127.0.0.1, and a Host header mismatch is harmless (302 to /login). A real browser over
  `tailscale serve` got `__Host-` Secure cookies, the Czech UI and the seeded look.
- `incus exec --user` sets no supplementary groups, so commands run through `runuser`.
  Gateway ExecStart: `"/home/<name>/.hermes/hermes-agent/.hermes/bin/hermes" "gateway" "run"`.
- Policy: a read-only `/etc/hermes` disk (`shift=true`) was honoured ("9 managed settings
  were not saved"); model-guard mounted read-only into `hermes-agent/plugins/` plus a
  `.git/info/exclude` line was enabled. Managed `plugins.enabled` replaces the instance's
  list, so a plugin users install (e.g. hermes-3310, the phone plugin) loads only if listed.
- Official installer as the instance's account, `--channel stable --commit <sha>`: fresh v0.23.0
  in 191 s; rerun with the v0.23.1 sha: exit 0 in 93 s, exactly v0.23.1, git status clean,
  read-only model-guard intact. It does not build the Webapp: `hermes webapp --build-only`
  122 s fresh, 107 s after an update. `hermes update` cannot pin a tag (VM: 171 s, ~200 MB
  download, +0.8 GB disk, rebuilds the Webapp itself).
- `hermes pm gc`: 1 s, keeps the previous generation (2 environments, ~420 MB each).
  `hermes plugins install hermes-lang-cs --yes-deps --no-enable` as the instance's account
  installs the sha its catalog pins into `~/.hermes/plugins/`.
- The native installer installs neither Chromium's shared libraries nor the Bot Screen
  packages. With the official image's Dockerfile lists (v0.23.1) the browser tool, the Bot
  Screen and a real chat with terminal and browser tools worked.
- Docker inside worked (hermes-3310's `install-asterisk.sh` ran unchanged); Tailscale inside
  signed in to another tailnet and worked over a DERP relay.
- `instance add` ≈ 92 + 191 + 122 s + config ≈ 7 min; update ≈ 93 + 107 s + restart per instance.
  Idle container with Hermes ~370 MB private RAM; an empty one with Docker and Tailscale
  ~80 MB. ~2.8 GB own disk per instance for Hermes (copied from a template, so without the
  system packages).

### Plugins and Claude Code (2026-11-13)
- `hermes plugins install` without a terminal reads `requires_env` prompts from stdin; with
  stdin at end of file (our `as_user`) it skips them and goes on. Flags: `--force`, `--ref
  <40-hex sha>` (the only way to pin a Git install), `--yes-deps`, `--enable`/`--no-enable`.
  `--no-enable` also skips the plugin's Python packages; `--enable` on an already active plugin
  leaves the selection alone, on one the users turned off turns it on again **[src]**.
- The package manager installs packages for the plugins in the instance's own `plugins.enabled`;
  managed lists replace the instance's but are not what it reads, and a bulk config save (e.g.
  `gateway install`, the Webapp's settings) drops managed keys from the instance's config. The
  next `hermes` command then finds the environment out of date and rebuilds it without those
  plugins' packages. Extras (`hermes pm install --extra <name>`) are kept through every
  rebuild, so the phone plugin's packages come from Hermes's own `piper` extra **[src]**.
- Debian 13's `docker.io` only recommends `docker-cli` **[src]**.
- A `kind: model-provider` plugin (the Claude subscription) loads without `plugins.enabled`;
  only `plugins.disabled` stops it. A `kind: platform` plugin from outside the bundle
  (hermes-3310) must be in `plugins.enabled`, and its adapter starts only once its env vars
  (`PHONE_PIN`, `PHONE_ALLOWED_CALLERS`) are set **[src]**.
- hermes-3310 is not in the plugin catalog; the Claude subscription plugin is, pinned by sha.
  Hermes's own plugin auto-update (`plugins.auto_apply`) is opt-in and skips `--ref` installs
  **[src]**.
- Managed `model.*` keys: `hermes config set` and `unset` refuse them (exit 1); `/model` and the
  Webapp picker still switch per session **[src]**. `hermes config get agent.reasoning_effort`
  calls the key unrecognized, but Hermes reads it **[src]**. Without
  `hermes.desktop.visible-models` (a diff against `known-models`) the picker shows the
  provider's featured models **[src]**.
- Hermes writes a section that the managed-key stripping left empty, below the template's
  comments, as a bare `{}` line: invalid YAML, after which its package manager cannot read the
  instance's plugin selection. It happened when the lock went on for an instance that had
  `model.provider` and `model.default` of its own (setting the provider drops `base_url`,
  the section's last other key). `apply` unsets those keys before it writes a locked managed
  scope; `unset` leaves a valid file **[tested]**.
- Claude Code: the native installer puts `claude` in `~/.local/bin`, needs no Node and refuses
  to run under `sudo`. `claude auth login` works without a terminal: it prints a link and reads
  the code from stdin. The Claude subscription plugin starts `claude` with
  `DISABLE_AUTOUPDATER=1`, so it never updates itself there **[src, tested locally]**.

## 5. Rejected options (and why)

- Docker: docker group = root.
- One Linux account per department with a Hermes home per user: colleagues can read each
  other's chats, ChatGPT tokens and Webapp passwords (host shell, same uid).
- Hermes profiles per user in one account: the Webapp shows all profiles; `-p` does not
  lock it. Fine for sharing an instance, not for privacy between users.
- Hermes's built-in "Web Access" (desktop app): needs a Nous account per user, plain
  HTTP on the LAN.
- Plain HTTP as the LAN default: unencrypted, and the browser turns off Webapp features.
- Third-party web UI (hermes-webui): worse than the built-in Webapp for this use.
- One shared ChatGPT subscription for several users: against the provider's terms.
- Editing Hermes source: everything works through managed scope, bundled-but-excluded
  plugins and catalog plugins. Keep it that way so updates stay safe.
- Rootless Podman with the official Hermes image per user: one 3.85 GB image copy per
  user, no `sudo` or `apt` for the user, and SSH, Tailscale and the phone plugin need
  workarounds.
- One Linux account per user on a shared `/opt/hermes` install (the previous design):
  borrowing a root-owned install needed workarounds (permissions, the environment resync,
  `pm gc`, the Webapp build as root) and a per-account loopback firewall (section 4).
- An Incus managed bridge: containers resolve each other's names through its DNS, reverse
  lookups even with `dns.mode=none`.

## 6. Approach

### 6.1 Repository layout

```
install.sh          thin bootstrap: packages, the tool, then `thinny setup`
bin/thinny          admin CLI entry point (Python 3 stdlib only, runs as root)
thinny/             settings, registry, render (all generated text), system (commands on the
                    host and in containers), host (Incus pool and profile, firewall, units,
                    release, access), instances, apply, checks, update
files/model-guard/  mounted read-only into every container
site.example.toml   every site setting with its default, commented
extras/, tests/     netcup snapshot and nightly watch (run off the server); pytest
```

### 6.2 State on the server

```
/etc/thinny/site.toml           admin-edited settings
/var/lib/thinny/instances.json  registry: name, port, container address, access, URL, state,
                                the built-ins (render.BUILT_INS) its container last got
/var/lib/thinny/release         "<tag> <commit>": the Hermes release all instances run
/etc/hermes/                            Hermes managed scope, rendered from site.toml
/etc/thinny/thinny.nft  our nft table for the containers
/etc/systemd/system/incus.service.d/thinny.conf, /etc/sysctl.d/90-thinny.conf
Incus                                   pool `default` (btrfs, sparse file), profile `thinny-instance`,
                                        one container per instance, named after it
```

- Names, ports and container addresses (`10.78.0.0/24`) are never reused; states `pending`,
  `active`, `disabled`.
- Profile `thinny-instance`: root disk with the `disk_gb` quota, `/dev/net/tun`,
  `security.idmap.isolated`, `security.nesting`, `limits.cpu`, `limits.memory`,
  `limits.processes=2000`, `boot.autostart`. Per container: a routed NIC `thinny-<port>`,
  `/etc/hermes` and model-guard as read-only disks (`shift=true`), and a proxy device from
  host `127.0.0.1:<port>` to the same port inside.
- Our nft table: forward from `thinny-*` only out of `network.interface`, not to private, CGNAT
  (tailnet), link-local, multicast or blocked networks, no IPv6, nothing new into `thinny-*`;
  input: nothing new from `thinny-*`; masquerade. Loaded by `config apply` and by the incus.service
  drop-in (`ExecStartPre`, `PartOf=nftables.service`), so incusd never starts without it.
  The admin's tables stay untouched; their forward chain must accept `thinny-*`.
- Everything in a container runs through `incus exec`; the instance's commands run as its account
  (`runuser`, clean environment), and files in its home are read and written as that account.
  Output is bounded in size and time: users control every program in their container.
- `thinny` and the nightly update share one lock.

### 6.3 Prerequisites (the admin's)

In the README, verified by `doctor` with the exact fix, all warnings: SSH key-only and on a
private network, an input firewall with policy drop, unattended upgrades with a reboot window
after the update window. The forward chain is a requirement: without it containers have no
internet, and `doctor` fails per instance with the two lines to add. hidepid and a root-only
Tailscale socket are no longer needed: the host has no instance accounts.

### 6.4 `install.sh`

Idempotent; a rerun repairs and never destroys data or updates Hermes: packages
(`incus-base`, or `incus` on Ubuntu 24.04, `btrfs-progs`, `nftables`, `curl`, `git`, `python3`;
Caddy later when LAN access needs it), the tool and the path of the clone it ran from,
`site.toml` from the example on the first run, then `thinny setup` and the commit it set up:
the storage pool once (80% of the free space under `/var/lib`; no Incus network), then
`config apply`: firewall first, units, sysctl, profile, `/etc/hermes`, Webapp settings, access,
remembered restarts. No Hermes on the host.

`bin/install` is the one-line install (`curl ... | bash`): Git, a clone in
`/usr/local/src/thinny` (an existing one is kept), then its `install.sh`.

### 6.5 `thinny`

```
setup                             Incus storage pool, then config apply
status | doctor                   quick look (release, last update, containers, gateways);
                                  every check (section 7)
update [--dry-run] [--scheduled]  update now (6.8); the timer runs --scheduled, which waits
                                  for the minimum age and respects auto_update
config edit | apply               visudo-like edit of site.toml, then apply; re-render and
                                  restart where needed
instance add <name> [--access ...]
                                  port, address, container, packages, account (uid 1000, sudo,
                                  linger, docker), Hermes at the recorded release, Webapp build,
                                  Claude Code, language pack and built-in plugins (turned on),
                                  model-guard, Webapp password, language, look, without the
                                  model lock the starting model, access, gateway
instance access | login [--claude] | reset-password <name>
                                  access method, ChatGPT or Claude device login, new Webapp
                                  password
instance disable <name>           autostart off, container stopped (forced after 60 s), access
                                  removed; container and data stay
instance list | restart           instances; one-by-one gateway restarts
```

- Names: lowercase ASCII (Linux account and URL safe), not an account on the host (the
  container has Debian's system accounts too) and not an existing container.
- `instance add` records `pending` first and can be rerun to finish after a failure.
- Gateway restarts happen only where unavoidable, and are announced.
- The admin's shell in a container: `incus exec <name> -- runuser -l <name>`.

### 6.6 Access per instance

`instance add` asks for one method per instance (`--access`, default from `site.toml`), since a
Webapp accepts only its own `public_url`. The Webapp listens on `127.0.0.1:<port>` inside the
container; the proxy device publishes it on the same host loopback port, and every method
points there: **LAN** a Caddy site with its local CA (`tls internal`); **Tailscale**
`tailscale serve --bg --https=<port>`, offered only when Tailscale runs; **own address** the
URL entered, the admin points a proxy at the port; **local** `http://<name>.localhost:<port>/`
through `ssh -L <port>:127.0.0.1:<port> root@<server>`.

With `access.tunnel_port`, Caddy also serves every own address at `http://127.0.0.1:<tunnel_port>`
for one tunnel (cloudflared with a wildcard route): one site per host, with an instance on its
root or one per path (`handle_path`, `X-Forwarded-Prefix`), 404 for anything else. An address is
a host or a host and one path segment, and a host is never both an instance's root and another's
path.

### 6.7 Settings (`site.toml`)

`[hermes]` update timing, `[policy]` provider, models, effort, `model_lock`, approvals, language and
`language_pack` (a catalog plugin name), `[container]` `cpu`, `memory_mb`, `disk_gb`,
`[webapp]` port range and the look for new instances (`text_scale` 110), `[access]` `default`,
`lan_address`, `tunnel_port`, `[network]` `interface` (`eth0`) and `blocked_networks` (public networks;
private ones and the tailnet are always blocked). `site.example.toml` lists every key.

### 6.8 Nightly update

- thinny first: `git pull --ff-only` in the clone `install.sh` ran from, never prompting for a
  password. When its commit is not the one `install.sh` last set up, `install.sh` runs (outside
  the lock: it runs `thinny setup`), then the run starts over in the new code. A failure ends the
  run as FAILED; a commit pulled but not set up is set up by the next run. `auto_update = false`
  leaves thinny as it is, `--dry-run` does not touch it.
- The latest stable release once it is `min_release_age_hours` old; `auto_update = false`
  only reports it.
- Exact target: the tag's commit from the GitHub API. `hermes update` cannot pin one, so for
  each active instance not at the commit, one at a time: the official installer again with
  `--commit <sha>` (the same path as `instance add`), `hermes webapp --build-only`, the language
  pack (`hermes plugins install --force`), `hermes pm gc`, then `gateway restart --yes` with
  the checks. Stop at the first failure.
- `update-unfinished` holds tag, commit and the instance being updated; a later run finishes the
  rollout without asking GitHub, whatever its age: that instance again from the start (its
  checkout may be at the commit while its Webapp build is not), then the instances not yet at
  the commit. The recorded release
  moves only when all instances run it; `instance add` installs it (the latest stable release when
  none is recorded yet).
- Built-ins: `render.BUILT_INS` is a hash of the container package list and the built-in
  plugin pins, recorded per instance after its checked restart. When it changes (a thinny
  update), the next run rolls out at the recorded release, also when a newer release still
  waits, through the same one-by-one rollout; `auto_update = false` only reports it. Plugins are
  installed with `--enable` only then and at `instance add`, so a Hermes update keeps what
  users turned off.
- Every run that is not a dry run also runs `claude update` for every instance, best effort: the
  Claude subscription plugin starts `claude` with `DISABLE_AUTOUPDATER=1`.
- No pre-update backup: `/etc/hermes`, model-guard and the language pack can all be
  recreated. The snapshot (off the server, not awaited) is the only way back.
- Inside the containers, `unattended-upgrades` with Debian's defaults updates the OS.
- Report when the host needs a reboot (`/run/reboot-required`); never reboot.

## 7. Checks (`thinny doctor`)

- Install: Incus pool `default` and profile `thinny-instance`, the incus.service drop-in, IPv4
  forwarding (FAIL); `/etc/hermes` matches `site.toml` (WARN); the recorded release; nightly
  timer enabled (WARN); last update status (FAIL when failed, WARN when missing or older than
  48 h).
- Prerequisites (section 6.3), all WARN.
- Firewall: the loaded table is exactly what `site.toml` renders, chain by chain (FAIL).
- Caddy active when any instance uses LAN access, or own addresses with a tunnel port (FAIL);
  then something answers at the tunnel port (FAIL).
- Per instance: container running and reaching the internet (FAIL; the fix shows the
  forward-chain lines); gateway active and Webapp answering (FAIL), `public_url` as recorded
  (WARN); Hermes at the recorded commit (WARN); Claude Code and the built-in plugins installed
  (WARN); no bypass (WARN): `HERMES_MANAGED_DIR`, `HERMES_BUNDLED_PLUGINS` or
  `HERMES_ENABLE_PROJECT_PLUGINS` in `.env` or the gateway unit, their own `model-guard` in
  `~/.hermes/plugins` (with the model lock), a gateway unit that does not start their own
  `hermes`.
- A check that crashes is reported as FAIL; the others still run.

## 8. Rounds

Each round ends with a stop for review and a commit.

Shared-install design (done, 2026-11-12): 0 spike on the test server (loopback isolation,
a real update, nft table at boot and restarts, Tailscale socket, sandbox reach, access
option 4, Caddy's warning, a reboot); 1 settings, registry, rendering; 2 commands and
`install.sh`; 3 nightly update; 4 full run from a clean-base snapshot; 5 README, LICENSE,
extras, leak scan.

Container switch (2026-11-12/13):

0. **Server spike** (done; section 4, Incus containers): installer `--commit`, model-guard
   mount through an update, language pack, limits, incus drop-in, anti-spoofing, reboot.
1. Settings, registry, rendering: `[container]`, `network.interface`, text scale 110,
   container addresses, firewall, profile, networkd file, local terminal backend (done).
2. Instances as containers: add, resume, remove, bounded commands as the instance's
   account (done).
3. Install and apply: storage pool, profile, units, sysctl, firewall first (done).
4. Versions: the recorded release, the per-instance rollout, resume (done).
5. Checks for containers; shared install, hermes-podman and sandboxes removed (done).
6. README and this file (done).
7. Server test from a rebuilt playground (done, 2026-11-13) **[tested]**: everything of the
   shared-install design and of the spikes removed (Tailscale kept); `install.sh` twice; `doctor`
   all OK. With the forward-chain lines removed, `instance add` hung in apt for over 15 minutes
   (apt neither fails nor gives up without a network): now a DNS probe fails it in 40 s with
   the fix, and the resumed add finished. alice (local), bob (tailscale), carol (lan): 486, 438
   and 487 s each, pinned to v0.23.0. From inside a container, as its account and as root: only
   the internet; another container (a real listener), host SSH on every address, Caddy, another
   Webapp port, MagicDNS, a tailnet device, private ranges and IPv6 blocked; only itself in
   /home, passwd and processes. Browser sign-in for every access method (Secure cookies over
   Tailscale and LAN, distinct cookie names); ChatGPT device login through `thinny instance login`;
   a real chat with terminal (`sudo` works) and browser tools. `sudo apt install`, Docker (also
   `--network host`) and Tailscale inside a container. `update` to v0.23.1 killed during
   alice's install, resumed (alice redone), killed again after bob's checkout had moved,
   resumed (bob redone first, then carol): release recorded, apt packages, Docker images,
   the login and the plugins kept, `doctor` all OK. Reboot: all 36 checks OK, Webapps answer.
   `instance reset-password`, `instance access` (local to tailscale) and `instance disable`
   (stopped, no autostart, port closed, access removed).

Ubuntu support (2026-11-13), in local Multipass VMs **[tested]**: 26.04.1 (kernel 7.0, Incus
6.0.5, Caddy 2.6.2) and 24.04.5 (kernel 6.8, Incus 6.0.0, Caddy 2.6.2). `install.sh` twice,
alice (local) and bob (lan): `doctor` all OK, Webapps answer, isolation as on Debian, Docker
inside, a reboot.

- 24.04 has no `incus-base`; its `incus` has no virtual machines either.
- Ubuntu's `kernel.apparmor_restrict_unprivileged_userns = 1` keeps the instance's account
  (not root in the container) from creating user namespaces, so Hermes's Chromium dies ("No usable
  sandbox"). Incus stacks the host's `unconfined` onto every container label, so the
  restriction applies there. A profile with `userns,` inside the container did not attach to
  Chromium; an `incusd` host profile with `userns,` broke container start (LXC wrote
  "incusd (unconfined)" as the label). Setting it to 0 works, and costs nothing that matters:
  users are root in their containers and can create user namespaces anyway, as on Debian.
  `sysctl -p` fails on Debian's missing key despite `-`; `systemd-sysctl` honors it.
- ufw's default `DEFAULT_FORWARD_POLICY="DROP"` cuts the containers off;
  `ufw route allow in on thinny-+` lets them through, and our table still filters first.

Model lock and built-ins (2026-11-13): 1 `policy.model_lock`, off by default (the managed
scope drops model, effort and plugins; new instances get them as their own config); 2 Docker,
Claude Code, the Claude subscription and hermes-3310 for every instance, `instance login --claude`,
the `doctor` check; 3 the per-instance built-ins hash, its rollout at the recorded release, the
nightly `claude update`; 4 README and this file. Server test (2026-11-13, Debian 13, Hermes
v0.23.1) **[tested]**: alice added by the previous thinny, then the upgrade (lock off by
default) and `thinny update`: `new built-ins at v0.23.1` in under 5 min, Docker (`docker run`
works), Claude Code 2.1.323, both plugins on, piper importable, `doctor` all OK; its running
gateway got the `docker` group only after `incus restart alice`. bob (lock off) added in 9 min
with the starting model and effort, the `docker` group in its gateway. Lock on: `config apply`
passed the model-guard checks, carol (locked) added in 11 min. A minute after the lock went on,
Hermes left bob's config unreadable (section 4); with `apply` unsetting first, all configs
stayed valid, also after a forced bulk save, and carol's own `plugins.enabled` was gone while
its environment kept piper through `hermes pm install`. Phone plugin listening with the lock on;
`install-asterisk.sh` ran as the instance's account and Asterisk sent its REGISTER out masqueraded.
`instance login --claude` printed the link (a wrong code asks again). Not done: a real Claude
sign-in and a real call (see section 9).

## 9. Open questions

- Package drift: `render.PACKAGES` copies the official image's Dockerfile lists (v0.23.1).
  When Hermes adds a system dependency the list must follow; containers get the list at
  `instance add` and before each Hermes update.
- Phone calls through a container are untested. The test server gets no SIP replies at all,
  not even to the host itself (`tcpdump` on `eth0`: OPTIONS to Odorik and Linphone leave, nothing
  comes back; the same OPTIONS from elsewhere get `200 OK`): the server's firewall in netcup's
  control panel drops them. Open it for SIP before a call test.
- Per-instance SSH and Tailscale are left to the users; SSH needs the host as jump host.
- Not tested: `systemctl restart nftables` with containers running (do Incus's per-NIC
  anti-spoofing rules come back without restarting the containers?).
- The storage pool is sized once at install; growing it is manual.
- Alerting from the server itself (no mail sender today; the watchdog runs off-server).
- Per-instance ChatGPT seats versus one API account per organisation (a local proxy such as
  CLIProxyAPI is parked).
