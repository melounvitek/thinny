# thinny: design notes

What we learned building thinny: how Hermes, Incus and the services around them behave, the options
we rejected and what is still open. The README is the user documentation; the code and `git log` say
what thinny does.

The repository is public: no personal or site data in it (IPs, hostnames, tailnet names, e-mails,
organisation names, tokens). `local/` is git-ignored and holds the hand-built prototype for reference;
it contains site data and must never be committed.

## 1. What we know

Hermes v0.23.1, Debian 13, Incus 6.0.4 unless noted. Sources are marked: **[proto]** seen on the
hand-built test server, **[tested]** tried on a test server (or where noted, a VM or this
workstation), **[src]** read in the Hermes source (also present in v0.23.1 unless noted),
**[static]** read in the source but not run, **[docs]** from the documentation.

### Hermes
Learned under the previous design (one Linux account per user on a shared install, sandboxes in
rootless Podman); it still holds unless a later subsection says otherwise.

- Managed scope (`/etc/hermes`): its `config.yaml` values win over each home's config **[src]**.
  Managed lists **replace** the home's list wholesale, so `plugins.enabled` must list every
  plugin that must be on **[src]**.
- Its `.env` is world-readable: never secrets. It loads last and wins over the home's `.env`
  **[src]**. It must pin `TERMINAL_ENV` too: otherwise a user's `.env` can beat the managed
  `terminal.backend` **[static, reproduced with throwaway homes]**.
- Ways around it for a user with a shell **[src/static]**: `HERMES_MANAGED_DIR` or
  `HERMES_BUNDLED_PLUGINS` in their own `.env` (read before the managed dir is resolved),
  `--safe-mode` (skips all plugins, model-guard included), `/yolo`, `/model` and
  `/reasoning` per session, a same-named plugin in `~/.hermes/plugins`, editing their own
  gateway unit. `doctor` looks for those that stay in files: the variables, the plugin and
  the unit.
- `approvals.mode` must not be `off`: with approvals off, computer_use runs "unrestricted"
  and looks for `cua-driver` on the host instead of in the sandbox, which breaks the Bot
  Screen **[proto, src]**. Pin `bot_desktop.placement: terminal` so the browser and
  computer_use stay with the terminal backend **[static]**.
- There is no built-in model allowlist for the main agent **[src]**. model-guard's `pre_llm_call`
  hook blocks any turn whose model is not in `allowed-models.txt` (fail closed when empty)
  **[proto]**. The hook gets the model but not the provider or effort, and does not see a
  fallback model chosen mid-turn **[src]**.
- UI language: the catalog plugin `hermes-lang-cs` plus `display.language cs` per home
  **[proto]**. Some placeholder strings stay English (not in the pack).
- Webapp per home: `webapp.enabled`, `webapp.host 127.0.0.1`, `webapp.port`, `webapp.public_url`,
  basic auth via `HERMES_DASHBOARD_BASIC_AUTH_USERNAME/PASSWORD/SECRET` in the home's `.env`
  (`_PASSWORD_HASH` also exists) **[src]**. Changing `webapp.*` needs a gateway restart **[docs]**.
- **`public_url` is required** **[tested, docs]**: Hermes answers only requests for the bound
  address or `public_url` ("Invalid Host header" otherwise), and only `public_url` turns on
  username/password sign-in on a loopback bind. Without it the only way in is a one-use
  link from `hermes webapp` on the server, valid 60 s. A wrong password stays on the login
  page **[tested]**.
- A loopback `public_url` (`http://127.0.0.1:<port>/`) does **not** turn on password sign-in;
  the Webapp still asks for a one-use link. A one-use link through an SSH tunnel opened as
  root gets 403 (Hermes checks the connection belongs to the user's account). A
  `public_url` of `http://<name>.localhost:<port>/` through a root SSH tunnel works: login
  required, secure context, all features **[tested]**.
- **Plain HTTP on a non-loopback address loses** clipboard, microphone, `crypto.subtle`
  (Bot Screen), notifications **[tested, docs]**. HTTPS through Caddy with its local CA:
  login, real chat and clipboard work; after the certificate warning's "Advanced" ->
  "Proceed" too **[tested]**.
- A signed-in Webapp user can run any command as the account (its terminal) **[docs]**.
- Cookie names carry a hash of the home, so two Webapps on one hostname do not clash
  **[src]**.
- Webapp look (light mode, text size, simple interface, visible models) is server-side
  `~/.hermes/ui-preferences.json`; applied from a template through
  `hermes_cli.ui_preferences.write_preferences`, run as the user with the home's venv
  Python **[proto]**. The template must include `known-models`. Some settings (e.g. the Bots
  plugin toggle) are browser localStorage only.
- Without a ChatGPT login, the Webapp's first screen invites the user to connect other
  providers **[tested]**: do the login before the hand-over.
- `hermes -p <profile> webapp` does not lock a Webapp to one profile; profiles of one gateway
  share it **[docs]**.
- ChatGPT login: `hermes auth add openai-codex --type oauth --no-browser` as the home's account;
  device code, ~15 min expiry **[proto]**. Tokens in `~/.hermes/auth.json`, mode 600 **[src]**.
  Never copy `auth.json`: refresh tokens are single-use.
- `hermes gateway restart` refuses without `--yes` when a reply, cron job or delegation is
  running and there is no terminal **[src]**. With `--yes` it still waits for a running
  reply but cuts off jobs and background tasks.
- The downgrade guard checks only the updating home **[static]**; databases cannot be
  downgraded, so the snapshot is the only way back.
- **Debian's `systemctl restart nftables` runs `nft flush ruleset`** and removes every table,
  Tailscale's included; restarting `tailscaled` restores them **[tested]**. Never restart
  `nftables.service`; load our table with `nft -f`. Elements added at runtime are gone
  after a reboot, so everything is regenerated from the registry.

### WebUI (hermes-webui, 2026-11-15)
- Python's standard library and plain JS, no build. `python3 server.py` with Debian's Python
  finds `~/.hermes/hermes-agent`, goes on in the Python of the instance's Hermes and imports the
  agent: chats run in WebUI's process, not the gateway's. Scheduled jobs and the phone still need
  the gateway, with `webapp.enabled false` **[tested]**. About 330 MB RSS idle **[tested]**.
- `HERMES_WEBUI_PASSWORD` (plain text) beats a hash in its `settings.json`; then its UI can neither
  change nor turn off the password **[src]**. Sessions live 30 days in memory and
  `webui/.sessions.json`, not tied to the password: delete the file while WebUI is stopped
  **[src, tested]**. Hermes does not strip the variable from the environment of the agent's tools
  **[src]**.
- Cookies `hermes_session` and a profile cookie, `Path=/`, named by
  `HERMES_WEBUI_COOKIE_NAME`/`_PROFILE_COOKIE_NAME`; `Secure` only with `HERMES_WEBUI_SECURE`
  **[src]**. Over Tailscale and `http://<name>.localhost` through SSH: sign-in, a cookie of its
  own name, Secure over HTTPS **[tested]**. Requests must come from the origin of their `Host`, which the proxy
  device, Caddy and `tailscale serve` keep **[src, tested over Tailscale]**.
- A real chat with the Claude subscription plugin, the terminal tool with Hermes's tools and
  `claude` on its `PATH` although the unit sets none **[tested]**.
- After a Hermes update, chats fail until WebUI restarts **[docs]**. Its own updater can move
  WebUI and Hermes's checkout; `check_for_updates: false` hides the offer **[src]**.
- Its model picker reads only the user's config and sends the chosen model with each chat:
  the managed model lock does not narrow it, model-guard still refuses the turn, but not WebUI's
  chat titles **[src]**.
- Theme and language: the server's `settings.json` wins in a browser without its own choice
  **[src, tested]**. Its server refuses the GitHub and Codex skins its picker offers: they stay in
  the browser, the server keeps `default` (v0.52.678, also its master) **[src, tested]**. thinny
  starts it with Graphite, only Chat and Todos in the sidebar and a plain message box
  (the look the admin chose on the test server), and with external sessions (Telegram, email) in
  its chat list but not Claude Code's. WebUI turns external sessions off itself when
  `settings.json` exists before its first start (v0.52.679) **[src]**. On a page load WebUI takes the hidden sidebar
  tabs from the browser only; the server's reach a browser once it opens Settings **[src, tested]**.
- Not addressed: 5 failed sign-ins a minute per client, and behind the proxy device every client
  is 127.0.0.1; a built-in terminal that cannot be turned off; public share links **[src]**.

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
- Firewall (thinny's table, eth0 hardcoded then): from inside, the internet works; another
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
- A disk added to a running container is not locked (2026-11-14): root inside remounted a
  read-only `shift=true` disk read-write and wrote into its host folder as the host's root.
  model-guard (one folder for all instances) was added that way at every `instance add`, and
  the policy disk when apply moved it to the instance's own folder. Without shift the remount
  was refused, and the container's root is only "others" on the folder. `incus config device
  set <name> <disk> shift=false` mounts it so in a running container, but only detaches the old
  mount: what its root bind-mounted or opened there while it was writable stays until the
  container restarts. A container started with the disk has it locked either way. Hermes checks
  no owner of plugin or managed-scope files.
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
- Compression (2026-11-15, netcup): a fresh instance's 5.9 GB of files took 5.9 GB on disk,
  3.5 GB with `compress=zstd:1` (btrfs's guess left most of Chromium and Claude Code
  uncompressed) and 2.8 GB with `compress-force=zstd:1`; the quota counts the compressed size.
  `incus storage set default btrfs.mount_options=...` remounted the pool at once with containers
  running, and the option was back after a reboot.
- With the admin's forward-chain lines removed, `instance add` hung in apt for over 15 minutes
  (apt neither fails nor gives up without a network): now a DNS probe fails it in 40 s.
- A running gateway got the `docker` group only after `incus restart` of its container.
- The OS inside the containers is updated by their own `unattended-upgrades` with Debian's
  defaults, not by thinny.

### Ubuntu (2026-11-13)
In local Multipass VMs **[tested]**: 26.04.1 (kernel 7.0, Incus
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

### Access trace (2026-11-14)
Debian 13, Incus 6.0.4, from Incus's stable-6.0 source and **[tested]** on the test server.

- Lifecycle events (`incus monitor --type lifecycle --format json`: a JSON line, then an empty
  line): `instance-exec` with only the argv as context, `instance-console`. `incus shell` is
  `incus exec -- su -l`. Over the unix socket the requestor is always root, so only the command
  tells thinny's own apart. `incus file` goes through SFTP and sends no event.
- (2026-11-15) Only incusd's log of API requests shows `incus file` (pull, push, edit, mount):
  `Handling API request` with `GET /1.0/instances/<name>/sftp` **[tested]**, not which files.
  `incus monitor --type logging --format json` gets every level, debug too; `--loglevel` works
  only with the pretty format. Snapshots (`instance-snapshot-created`) and exports
  (`instance-backup-created`) name the instance only in their source, `/1.0/instances/<name>/...`;
  the file API's `instance-file-retrieved`, `-pushed` and `-deleted` (other clients than `incus`)
  give the path. Neither `incus copy` nor `incus publish` names the instance it reads **[src]**.
- `systemctl restart incus` ends the monitor (websocket close 1006). Restarted 2 s later, it
  missed the execs of the first ~1.5 s after the restart.
- A disk with `required=false` whose source is missing: the container still starts, and
  `incus import` still takes it. A required one fails both.
- rsyslog is not installed on Debian 13. Installed, it gets the entries from the journal, files
  them in `/var/log/auth.log`, and `$programname == "thinny-trace"` forwarded them
  (`<86>`: authpriv.info).
- A bound Apps Script deployed as a web app has no active spreadsheet (Google's docs), so the
  README's script opens its sheet by ID.

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

### Phone line (2026-11-15)
hermes-3310 on the test server **[tested]**.

- On an instance set up by hand as `instance add` does it now, Asterisk (in Docker in the
  container) registered through the container's NAT, and a call in worked: PIN, Czech speech,
  Piper's replies. Piper's own speed (`length_scale` 1.0) was too slow.
- netcup's firewall policies are stateless: the test server's dropped the replies to SIP until it
  accepted UDP from Odorik (`81.31.45.0/24`, any port).

### Telegram (2026-11-15)
Hermes v0.23.1 **[src]**; managed bots tried with a real manager bot and phone **[tested]**.

- Hermes reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` (numeric user ids only) and
  `TELEGRAM_HOME_CHANNEL` from `.env` when the gateway starts. Without an allowlist an unknown
  sender gets a pairing code and "open a terminal and run `hermes pairing approve ...`"; any
  allowlist turns pairing off. Without a home channel the first message gets a "No home channel
  is set" notice **[src]**.
- One token, one reader: a second gateway polling it gets 409 Conflict. Webhooks have one URL,
  and Hermes routes one bot to several profiles only inside one gateway process **[src]**.
- Managed bots: with *Bot Management Mode* on (BotFather's app), the link
  `https://t.me/newbot/<manager>/<suggested username>?name=<name>` opens a pre-filled "Create
  bot" window. On *Create*, the manager gets a `managed_bot` update (the creator and the new bot)
  and a `managed_bot_created` message, and `getManagedBotToken` gives the bot's token. The new
  bot could write to its creator before they opened it. A message the creator sent it before
  the token was fetched never arrived; one sent later did **[tested]**.
- `getMe` kept answering `can_manage_bots: false` with the mode on, also after the bot was
  created, so thinny does not check it **[tested]**.

### Dashboard (2026-11-16)
Incus 6.0.0, 6.0.4 and 6.0.5, Prometheus 2.45 and 2.53, node exporter 1.7 to 1.10, Grafana 13.3.1
**[src]**; on this repository's workstation (Ubuntu 26.04, Incus 6.0.5) as an unprivileged account
**[tested]**.

- Incus's `/1.0/metrics`, on `core.metrics_address` (8444 by default), takes a certificate added
  with `--type=metrics`, which reads nothing else (6.0.0 and later). Its `server.crt` names
  127.0.0.1 and is readable by any account. Answers are cached 8 s; only running instances, all
  projects, labels `project`, `name` and `type`. Per container: `incus_cpu_seconds_total` (user
  and system) and `incus_cpu_effective_total` (its `limits.cpu`); memory without file cache is
  `MemTotal - MemFree - Active_file - Inactive_file`, as `instance list` counts it;
  `incus_memory_OOM_kills_total`; `incus_procs_total` counts threads too (its `pids.current`, which
  `limits.processes` limits); network per device inside the container; disk I/O per device, the same
  bytes on each device under the pool, so the largest; on btrfs no disk use (6.0.2 and later skip
  it, 6.0.0 reports the whole pool). About 70 series per instance and 26 for the host **[tested]**.
- Debian's and Ubuntu's `prometheus` and `prometheus-node-exporter` (main on Debian, universe on
  Ubuntu; Go packages get limited security support) start at install on every address
  (0.0.0.0:9090, :9100); masked first, they do not. Their `/etc/default` files and
  `prometheus.yml` are conffiles: edited, upgrades would ask, so drop-ins replace `ExecStart`.
  A bad config stops Prometheus for good (`Restart=on-abnormal`): `promtool check config` first.
  The node exporter reads `*.prom` in `/var/lib/prometheus/node-exporter` (a Debian default) and
  sees the pool's loop mount; with only its cpu, meminfo, filesystem and textfile collectors it
  has about 420 series. Its recommended collectors package adds five root timers.
- Grafana's apt repository (`stable` also brings new major versions; its key expires 2027-08-22)
  is not in unattended upgrades' default origins; `site=apt.grafana.com` allows it. 13.3 downloads
  381 MiB and takes about 1.9 GiB with the copy of its plugins in `/var/lib/grafana`. It starts no
  service at install; an upgrade restarts it. Every start downloads plugins from grafana.com unless
  `preinstall_disabled`; analytics, update checks, the news feed, gravatar, public key retrieval
  and external snapshots reach out too. Its 14 built-in backend data sources run as processes:
  825 MiB RSS, 405 MiB with the 13 besides Prometheus disabled **[tested]**. `admin_password` counts
  only when it creates its database. CSRF and its live websocket compare `Origin` with `Host`, which
  Caddy, `tailscale serve` and an SSH tunnel keep. Its sign-in cookie is the host's, for every port.
- Subscription usage (Hermes v0.23.1 and its Claude plugin **[src]**): `hermes usage --json` without `--provider`
  asks the configured provider, the managed scope's included (`load_config` merges it). For Codex it normally
  makes one GET of `chatgpt.com/backend-api/wham/usage`, no model call; it refreshes the credential under
  `auth.lock` in the Hermes home the gateway shares, and retries once after a 401. The Claude plugin starts
  `claude -p` with `get_usage` (8 s; 10 s for the hook) and caches nothing. Codex fails with exit 1 and one line
  for every cause; the plugin with exit 0 and `unavailable_reason`. Nothing keeps the shares on disk. Node
  exporter 1.7 and 1.10 label `node_textfile_mtime_seconds` with the file's full path. From public reports,
  not tried: Codex's own TUI reads its limits every minute; Anthropic's limits readout answers few requests per
  sign-in, and a 429 can last hours, also for the user's own Claude Code.
- Grafana answers about 10 s after it starts (database migrations, provisioning): restarting all
  three at every apply left `doctor` failing and the printed address unanswered, so apply restarts
  only what changed and waits until Prometheus and Grafana answer. Memory with one instance:
  Grafana 233 MiB, Prometheus 28 MiB, the node exporter 6 MiB **[tested]**.

### Email (2026-11-16)
Hermes v0.23.1 **[src]**; Purelymail from its API spec (`news.purelymail.com/api/`), docs and public
issues **[docs]**, not tried.

- Hermes's email gateway is a bundled platform plugin: `EMAIL_ADDRESS`, `EMAIL_PASSWORD`,
  `EMAIL_IMAP_HOST` and `EMAIL_SMTP_HOST` in `.env` turn it on, also with a managed `plugins.enabled`
  (bundled platforms load unless disabled). Ports 993 (TLS) and 587 (STARTTLS) by default **[src]**.
- It trusts a sender only through the topmost `Authentication-Results` header with the authserv-id
  `EMAIL_AUTHSERV_ID`: DMARC pass, or SPF or DKIM pass aligned with From. Without the id it drops all
  mail **[src]**. Purelymail's id is `purelymail.com`; mail between two Purelymail mailboxes carries only
  `auth=pass`, which Hermes would drop **[docs: raw headers in public issues]**.
- `EMAIL_ALLOWED_USERS` takes whole addresses (or `*`); other senders are ignored without an answer and
  marked read. `EMAIL_HOME_ADDRESS` gets scheduled jobs' mail. One conversation per sender **[src]**.
- It polls INBOX for unread mail and, at every start, takes what is already there as old (in memory):
  mail that arrives while the gateway is down is never answered **[src]**.
- Purelymail's API: `POST https://purelymail.com/api/v0/<method>` with a `Purelymail-Api-Token` header;
  errors come with HTTP 200 as `{"type": "error", ...}`. `createUser` (password reset and a welcome mail
  unless turned off), `modifyUser` (`newPassword`), `deleteUser`, `listUser`, `listDomains`
  (`dnsSummary`, `symbolicSubaddressing`); `addDomain` only once the DNS records are there **[docs]**.
  Symbolic subaddressing is on for new domains: dots ignored, addresses cut at symbols **[docs]**.
  About 3000 sends outside Purelymail a day per account **[docs]**.
- On the test server (2026-11-16) **[tested]**: a Gmail message's topmost `Authentication-Results` starts
  with `purelymail.com;` (DMARC, SPF and DKIM pass); `listUser` gives whole addresses; `modifyUser` takes
  the unchanged password; `addDomain` passed at once once the records were on Cloudflare. netcup's default
  firewall policy "netcup Mail block" drops outgoing 25, 465 and 587: Hermes then fails SMTP, takes no mail
  either and retries (1 to 4 min). On its first connect it mailed the home address a startup note, and a
  restart a "shutting down" one; `platforms.<platform>.gateway_restart_notification: false` stops both,
  also from the managed scope, where it leaves `enabled` to the instance **[src]**. thinny's policy pins it
  for email, the phone and Telegram: a block there makes Hermes load that adapter at start, but not
  turn the platform on **[src]**. The reply to the note got an answer in the thread. A reconnect within
  one gateway process takes the mail that came meanwhile; only a new process skips it.
- An older thinny refuses registry fields it does not know: going back to `main` after the test
  needed the registry's empty `mailbox` fields removed **[tested]**.

### Export (2026-11-16)
Hermes v0.23.1 (3b42157) **[src]**, hermes-webui 0.52 and hermes-3310 **[src]**; nothing run.

- `hermes backup -o <zip>` zips the Hermes root (`~/.hermes`, profiles included) without the codebase,
  venvs and dependency trees, browser profiles, `backups/`, `state-snapshots/`, `checkpoints/`, runtime roots
  (`installs`, `tools`, `models`, `runtimes`, `node`), `cache/` but its media and evidence, and secret caches.
  SQLite files go in as `sqlite3.backup()` copies staged beside the zip; symlinks and other non-regular files
  are skipped; a memory provider's state elsewhere under `~` goes in as `_external/<path>`. Exit 1 when the
  zip was written without some files, 2 while another backup runs. Its pruning (`--keep 3`) touches only
  `hermes-backup-*.zip` beside the output.
- It takes the credential stores too: `.env`, `auth.json`, the vault, `mcp-tokens/`, `webapp/session-token`,
  other services' tokens; also WebUI's `webui/.signing_key` (signs its sign-in cookies), `.pbkdf2_key`,
  `.sessions.json` and `passkeys.json`, and the phone plugin's `phone/asterisk/pjsip.conf`, with the SIP
  password. Hermes lists its own stores (`PROFILE_CREDENTIAL_PATHS`) behind
  `hermes_cli.profiles.profile_path_is_private(parts)`, relative to a profile's home and case-folded; its
  profile export also drops `auth.json`, `.env`, `.envrc`, `bot-desktop` and the OS credential folders
  (`agent.file_safety.HOME_CREDENTIAL_DIRS`) at any depth (`_EXPORT_CREDENTIAL_FILES`, private).
- `hermes import <zip>` takes a zip with any of `config.yaml`, `.env`, `state.db`, checks the CRC of every
  member it restores before writing, and asks before overwriting a home (`--force` does not). Without `.env`
  and `auth.json` its user signs in again.
- Other containers filling the storage pool during an export are not the export's to stop.

## 2. Rejected options (and why)

- Docker: docker group = root.
- One Linux account per department with a Hermes home per user: colleagues can read each
  other's chats, ChatGPT tokens and Webapp passwords (host shell, same uid).
- Hermes profiles per user in one account: the Webapp shows all profiles; `-p` does not
  lock it. Fine for sharing an instance, not for privacy between users.
- Hermes's built-in "Web Access" (desktop app): needs a Nous account per user, plain
  HTTP on the LAN.
- Plain HTTP as the LAN default: unencrypted, and the browser turns off Webapp features.
- One shared ChatGPT subscription for several users: against the provider's terms.
- Editing Hermes source: everything works through managed scope, bundled-but-excluded
  plugins and catalog plugins. Keep it that way so updates stay safe.
- Rootless Podman with the official Hermes image per user: one 3.85 GB image copy per
  user, no `sudo` or `apt` for the user, and SSH, Tailscale and the phone plugin need
  workarounds.
- One Linux account per user on a shared `/opt/hermes` install (the previous design):
  borrowing a root-owned install needed workarounds (permissions, the environment resync,
  `pm gc`, the Webapp build as root) and a per-account loopback firewall.
- An Incus managed bridge: containers resolve each other's names through its DNS, reverse
  lookups even with `dns.mode=none`.
- Own addresses under paths of one host: the instances share the browser's origin, so whoever is
  root in one can serve a page whose script acts as another's signed-in user; `Path` on the
  cookies does not stop that. Each gets a host of its own. Sibling hosts are still one site, but
  over HTTPS Hermes names its cookies `__Host-`, which a sibling cannot set **[src]**.
- Telegram through the Webapp's "Create with QR": a Nous-hosted service creates the bot and
  passes its token on; users found it confusing. A bot from BotFather with Hermes's pairing:
  the pairing message tells users to run a command in a terminal. One bot for all instances:
  one gateway per token (section 1, Telegram).
- Mail: a mail server on the host (inbound port 25, and mail from a VPS address lands in spam), Migadu
  (daily sending caps per account), Cloudflare's Email Routing and agent inbox (no mailboxes or IMAP,
  which Hermes needs). DNS records through Cloudflare's API in thinny: a one-time step, and sites use
  other DNS providers.

## 3. Open questions

- WebUI after a real Hermes update (its restart with the gateway is tested with fakes only), and
  with Firefox and over LAN through Caddy.

- The Google Sheet of the access trace is tested against a stand-in only: check a real Apps
  Script web app, its "ok" after the redirect, and the answer of one not open to anyone.
- `instance move` is experimental, tested with fakes only. Between two real servers, check: the
  uid remap of an imported container with `security.idmap.isolated`, a routed NIC changed with
  `device set` before the first start, Docker data, a reboot of either server afterwards, and
  sudo (Ubuntu's sudo-rs too) passing the archive on stdin.
- Package drift: `render.PACKAGES` copies the official image's Dockerfile lists (v0.23.1).
  When Hermes adds a system dependency the list must follow; containers get the list at
  `instance add` and before each Hermes update.
- Phone line: a whole `instance add` with one, and a call from Hermes, are untested on a server.
- Telegram: `instance telegram` against a real instance is untested on a server.
- Access trace: an export's event (`instance-backup-created`, from the source only) and the
  installed service with the wider trace are untested on a server.
- Mailboxes: not yet tried are a scheduled job's mail, mail from another Purelymail mailbox (dropped?),
  `move` and `uninstall` with a mailbox.
- Export: untested on a server: a real `hermes backup` in a container, its time on a large `state.db`, the
  copy with Hermes's real `profile_path_is_private`, and `hermes import` of the result. It follows Hermes's
  list of credentials, but mirrors its private any-depth names: check them when Hermes changes.
- Per-instance SSH and Tailscale are left to the users; SSH needs the host as jump host.
- Not tested: `systemctl restart nftables` with containers running (do Incus's per-NIC
  anti-spoofing rules come back without restarting the containers?).
- The storage pool is sized once at install; growing it is manual.
- Alerting from the server itself: the dashboard sends none, and the watchdog runs off-server.
- The dashboard on Debian 13 and Ubuntu 24.04, and a Grafana upgrade by unattended upgrades.
- Subscription usage on the dashboard is untested on a server: a day with a ChatGPT and a Claude instance
  (time and CPU per check, Anthropic's 429s). Also open: Codex's usage endpoint may answer 429 at 100 % (a gap
  where it matters most); the Claude plugin reads Claude Code's older answer (`utilization`) of 1 or less as a
  fraction, so 1 % or less could show as up to 100 % (upstream); a check that starts `hermes` during the
  nightly update; whether a timed-out `incus exec` leaves the command running in the container.
- Per-instance ChatGPT seats versus one API account per organisation (a local proxy such as
  CLIProxyAPI is parked).
