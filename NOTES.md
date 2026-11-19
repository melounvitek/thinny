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
- A timed-out command (2026-11-17, Incus 6.0.5 on this workstation): killing `incus exec`, as thinny's
  timeout does, leaves the command running in the container under its init (`runuser` goes, its child
  stays). Its output pipe is closed, so it dies at its next write (a shell from SIGPIPE).
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
- One pool per instance (2026-11-19, the workstation's Incus 6.0.5 and the test server's 6.0.4 on
  Debian 13's kernel 6.12): in the shared pool, `df` and statfs in a container showed the whole pool
  (481G), not its quota. btrfs reports no qgroup in statfs, Incus intercepts no statfs (only bpf,
  mknod, mount, sched_setscheduler, setxattr and sysinfo) and LXCFS covers /proc, /sys and cgroups
  only. In a btrfs pool of its own (`size=3GiB`, thinny's mount options) both showed 3.0G. Filling it
  gave "No space left on device", the container went on, and deleting freed it.
  `incus storage set <pool> size=4GiB` grew it at once with the container running, its `df` too;
  shrinking: "Pool cannot be shrunk". Docker inside ran `hello-world` (workstation).
  `incus move <name> --storage <pool>` of a stopped fresh container took 3-7 s and kept its files.
  It took the profile's root `size` along into the container's own root disk, a qgroup quota then,
  as did `incus import --storage` of an archive whose root disk was the profile's; `incus init
  --storage` gives a root disk without one. A pool unpacks the image again (about 190 MB), and
  Incus deletes that with the pool. `/1.0/storage-pools/<pool>/resources` has the used and total
  bytes `df` shows in the container, also while it is stopped.
- What a container still saw of the server and the others (2026-11-19, the test server, LXCFS on): its
  own CPUs and memory in `/proc` (LXCFS), but the server's memory in `/sys/devices/system/node` and
  `/sys/devices/system/memory`, its 16 CPU folders in `/sys/devices/system/cpu`, its pressure in
  `/proc/pressure`, memory and CPU counters in `/proc/vmstat`, `zoneinfo`, `buddyinfo`, `schedstat`,
  `interrupts` and `softirqs`, every disk's size in `/proc/partitions` and its reads and writes in
  `/sys/block/*/stat`, and the root filesystem's writes in `/sys/fs/ext4`. With a pool each, also the
  others': their pool files (`thinny-<name>.img`) in `/sys/block`, and the pools' names (their btrfs
  labels) and use in `/sys/fs/btrfs`. `df` on `/etc/hermes` and `/var/log/thinny` showed the
  server's disk. Not what is on the others' disks: there are no `/dev/loop*` nodes. `deny <path> r,`
  lines in the profile's `raw.apparmor` hide those files, but the CPU folders and the server's disk
  size: `incus profile edit` applied them to running containers, root in a container cannot lift
  them, they log nothing, and Docker inside still ran `hello-world` (workstation).
- With the admin's forward-chain lines removed, `instance add` hung in apt for over 15 minutes
  (apt neither fails nor gives up without a network): now a DNS probe fails it in 40 s.
- A running gateway got the `docker` group only after `incus restart` of its container.
- The OS inside the containers is updated by their own `unattended-upgrades` with Debian's
  defaults, not by thinny.
- Reboots (2026-11-18): the containers share the host's kernel and their users are root in a user
  namespace with nesting on, so a kernel privilege escalation is the likely way out. A fixed kernel
  protects nothing until the server reboots into it: `doctor` warns when `Unattended-Upgrade::Automatic-Reboot`
  is off and whenever `/run/reboot-required` exists, also when the automatic reboot comes that night.
- Who creates `/run/reboot-required` after a kernel upgrade, read in the packages **[static]**: on Debian 13
  only the hook of `unattended-upgrades` 2.12, `/etc/kernel/postinst.d/unattended-upgrades`, which the
  kernel's postinst runs through `linux-run-hooks` (linux-base 4.12.1, without the `touch-reboot-required`
  hook of later versions). On Ubuntu (26.04 on this workstation) that hook, linux-base's and
  update-notifier's. unattended-upgrades reboots at the end of its own run, only when the file exists, and
  `apt.systemd.daily` runs it only with `APT::Periodic::Unattended-Upgrade` on.
- On netcup (Debian 13), read there on 2026-11-18 **[tested]**: the hook is there and touches
  `/var/run/reboot-required`. `dpkg.log`: `2026-11-14 06:55:07 install linux-image-6.12.117+deb13-amd64`;
  `unattended-upgrades.log`: `2026-11-14 06:55:30 WARNING Found /var/run/reboot-required, rebooting`, then
  `Reboot scheduled for Sun 2026-11-15 04:00:00 CET`. `journalctl --list-boots`: the server was rebooted on
  Sat 2026-11-14 at 20:10, before that, and not at 04:00 on the 15th. So the file and the scheduling were
  seen, the automatic reboot itself was not. Its settings: `Automatic-Reboot "true"`,
  `Automatic-Reboot-WithUsers "true"`, `Automatic-Reboot-Time "04:00"`; no `/run/reboot-required`, running
  6.12.117+deb13-amd64, up 3 days. Neither warning would show there.
- A strict umask (2026-11-17, thinny 2a1f9a3). In a Debian 13 VM, the README's one-line
  installer as an account with sudo, without a terminal and under umask 077, then `install.sh`
  again: both exit 0, the clone 700 with 600 files, `/usr/local/lib/thinny` 755 with 644 files;
  as that account without sudo, `thinny status` and `thinny complete` worked. On this
  workstation (Incus 6.0.5), `instance add` under umask 077: the policy folder 755 with 644
  files; inside, the instance's account read `/etc/hermes/config.yaml` (its owner shows as
  `nobody`) and model-guard's files. It took 441 s.

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
- (2026-11-17) The installed service, on this workstation and in a Debian 13 VM **[tested]**: a
  run of thinny was noted once per instance (`thinny instance export <name> ran its commands in
  <name>`; in a move, the new server's `thinny instance receive ...`), a direct `incus exec` in
  full, `incus file push` as `incus file (pull, push, edit or mount) <name>`, and the move's
  export, in the instance's trace on the old server, as `incus export <name>`
  (`instance-backup-created`).

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
- Keypad approvals (hermes-3310 38c638e) **[src]**: the plugin waits `approval_timeout` (120 s)
  for a key once the approval was said, but Hermes's `approvals.timeout` (300 s, left as it is)
  counts from the request and still wins. One approval needs about 140 s; a later one in a queue
  may expire before its turn, and a group with `approvals.timeout` under about 140 s shortens the
  caller's time. The plugin needs Hermes v0.23.1 or later (v0.23.0 lacks
  `restrict_gateway_approval_card_to_deny`, so the plugin would not load).

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
- Usage checks during an update (Hermes v0.23.1 **[src]**): every `hermes` run first checks that its
  dependencies match the checkout (`uv.lock`, `pyproject.toml`, the Python pin; v0.23.0 to v0.23.2 each changed
  one). Started between the installer's move of the checkout and its `pm install`, `hermes usage` would find them
  stale and finish the update itself under Hermes's update lock, building for minutes, past thinny's timeout
  (see Incus containers); the installer's build step then fails at once on that lock, and the update with it. So
  the check skips the instance `update-unfinished` names: the update names it before touching it (apt and
  `git fetch` take seconds before the checkout moves), keeps it until the next instance or the end, and after a
  failure until a run finishes it. Not thinny's lock: it waits for nobody, so a check holding it at the wrong
  moment would fail the update or a command. A `hermes` started meanwhile by hand, by `thinny doctor` or
  `thinny instance usage` can still do it. The credential refresh is safe alongside: nothing in the update
  writes `auth.json`, and Hermes writes it atomically under `auth.lock`, as the gateway does.
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
- On the production server (2026-11-18, netcup, Debian 13, three instances) **[tested]**: with "netcup Mail
  block" removed, outgoing 25 is open too: `gmail-smtp-in.l.google.com:25` answered the host (not a
  container) with its 220 banner. `/proc/net/nf_conntrack` held no flow to 25, 465, 587 or 2525: one look,
  not a history. thinny's mail uses only 587 and 993 **[static]**, so its table drops TCP to port 25 from
  the containers: spam from one would get the server's shared address blocked.
- An older thinny refuses registry fields it does not know: going back to `main` after the test
  needed the registry's empty `mailbox` fields removed **[tested]**.

### Mail from the agent (2026-11-18)
Hermes v0.23.1 **[src]**; himalaya v1.2.0 and v2.2.1 **[static]**, its binary run on this workstation where
noted **[tested]**.

- Hermes has no agent tool that sends messages, on purpose (`tools/send_message_tool.py`); `hermes send
  --to email` sends over SMTP as plain text with a fixed subject, without attachments. Outside mail chats
  the prompt never mentions the mailbox, in Telegram chats only among scheduled jobs' destinations. So
  agents told users that no email was connected **[src]**.
- Hermes bundles a himalaya skill (and `email-inbox-triage`, which uses it). It lists the skill without
  checking that `himalaya` is installed, and the skill installs the latest into `~/.local/bin`, first on the
  instance's `PATH` **[src]**.
- himalaya v2 (2026-07) dropped `template`, MML and `--output`, which the skill uses; v1.2.0 has them. Its
  releases publish no checksums; GitHub's API gives each file's sha256 **[static]**.
- himalaya v1.2.0 reads one file, `~/.config/himalaya/config.toml`; `-a` goes after the last subcommand
  (`himalaya --account x ...` fails) **[tested]**. `auth.cmd` runs through `sh -c` and takes the first line.
  It saves a copy to Sent after SMTP: when that fails, it exits non-zero with the mail sent. `message read
  --preview` leaves mail unread; `read` without it, `message export`, `attachment download` and `template
  reply`/`forward` mark it read **[static]**.
- The gateway takes unread mail in INBOX every 15 s and marks it read **[src]**.
- Hermes removes `EMAIL_*` from the terminal's environment, and its file tool refuses `.env`; `grep` in the
  terminal reads it **[src]**.
- `agent.environment_hint`, also from the managed scope, is in the prompt on every platform;
  `HERMES_ENVIRONMENT_HINT` wins over it **[src]**.
- A chat reuses the prompt it started with. `hermes sessions refresh-prompts` has each chat of the profile
  rebuild it once, on its next turn, its history kept, also with the gateway running **[src]**.
- thinny's account in the v1.2.0 binary: it read the password from a `.env` as `set_env` writes it and
  reached `imap.purelymail.com`, which refused the made-up password **[tested]**.
- On the test server (2026-11-18, Hermes v0.23.2) **[tested]**: a new Purelymail mailbox has Archive, Drafts,
  INBOX, Junk, Sent and Trash. Asked in the Webapp for a short PDF by mail, the agent loaded the himalaya
  skill, read the user's address from `.env` with Python, and sent the PDF once with `/usr/local/bin/himalaya
  template send -a <address>` and an MML part; it arrived at Gmail.

### Export (2026-11-16)
Hermes v0.23.1 (3b42157) **[src]**, hermes-webui 0.52 and hermes-3310 **[src]**; run only where **[tested]**.

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
- On this workstation (2026-11-17, thinny 1ad9f29, a WebUI instance) **[tested]**: `thinny instance export`
  took 2 s, Hermes's backup 1 s of them: a 7.0 MiB zip of 488 files (17.2 MiB unpacked), every member's CRC
  right, its `state.db` 1.4 MB. Besides skills, plugins, `cron`, the databases, `config.yaml`, `SOUL.md` and
  `ui-preferences.json` it holds Hermes's logs (25 files under `logs/`) and lock files (`gateway.lock`,
  `auth.lock` and the like).
- It left out 11 files: `.env`, `auth.json`, `gateway-connections.json`, `webapp/session-token`, the vault's
  `.vault.lock`, `vault.key` and `vault.json.enc`, WebUI's `.pbkdf2_key`, `.login_attempts.json` and
  `.sessions.json`, and one more that thinny did not name. None of the instance's 11 real sign-in values was
  in any member: Claude Code's and Codex's access and refresh tokens, the Webapp's password hash and secret,
  WebUI's password and the rest of `auth.json`'s long values. Only the provider's base URL was, no secret
  (`state.db`, `context_length_cache.yaml`, `logs/agent.log`).
- `hermes import --force <zip>` as the account of a fresh Webapp instance of thinny, its gateway stopped
  first: it checked the archive, restored 485 of the 488 files in 3.2 s, kept 3 runtime files of its own
  (`gateway.lock`, `gateway_state.json`, a cached document), started the gateway service itself and exited 0.
  The instance still had its own `.env` and no `auth.json`; `state.db` and `config.yaml` had the archive's
  sizes. After `thinny config apply`, which restarted its gateway, `doctor` was all OK and the instance had
  the exported one's `model.default`. Inferred, not looked at: the archive's `config.yaml` brought the
  exported instance's `webapp` settings, and apply put its own back.

### Firewall check (2026-11-17)
**[tested]** in a VM of Ubuntu 24.04 (systemd 255) and one of Debian 13 (systemd 257), with thinny's table and
the check's two units but without Incus, unless marked.

- `nft flush ruleset`, and `nft -f` of a file that starts with it (Debian's own `/etc/nftables.conf`), remove
  thinny's table without restarting `nftables.service`, so the incus.service drop-in does not load it again.
  `thinny-firewall.timer` runs `thinny firewall-check` every minute: when `nft list tables` lacks
  `table inet thinny`, it loads `/etc/thinny/thinny.nft` as it is. After a flush the table was back within the
  minute. Only thinny's: Incus's per-NIC rules and Tailscale's tables stay gone.
- `OnBootSec=1min` with `OnUnitActiveSec=1min` ran the check every 60 to 61 s, the first time a minute after
  boot, or at once when the timer was enabled later. `AccuracySec=1s`: by default a timer may run up to a
  minute late **[docs]**.
- At boot the drop-in loads the table when incus.service starts. Where it has not by the first check (a boot
  of over a minute, or one where nothing starts incusd), the check loads the table and logs its line, once per
  boot.
- systemd logs three or four lines for each run of a oneshot service ("Starting", "Deactivated successfully",
  "Finished", "Consumed ... CPU time"), all as info. `LogLevelMax=notice` drops them, and with them what the
  command prints, logged as info by default: the check's line reached `thinny.log` but not the journal.
  `SyslogLevel=warning` logs what it prints as warnings, which pass. A failing run still shows: thinny's error,
  "Main process exited" (notice), "Failed with result" (warning) and "Failed to start" (err).
- With Incus (thinny 7a4444d): in a Debian 13 VM (Incus 6.0.4, one container running, no Tailscale, no
  firewall of its own) the timer ran every minute and wrote nothing to the journal while the table was
  loaded. At one boot incus.service started 40 s before the first check, which logged nothing. On this
  workstation (Ubuntu 26.04, Incus 6.0.5, two containers, Tailscale) the timer was active after the update
  and its service wrote no journal line in its first 25 minutes; nothing was flushed there.
- `nft flush ruleset` in that VM removed `inet thinny` and `inet incus`. Until the check ran, the container
  could not reach the internet and could open the server's SSH port at the server's own address (blocked
  before). The table was back after 52 s, the internet with it and the SSH port blocked again; one warning
  line in the journal, the same in `thinny.log`.
- `systemctl restart nftables` there (the service was disabled and inactive): incus.service restarted with
  it and loaded thinny's table, and the check logged nothing; ten seconds later the container had the
  internet and the SSH port was blocked.
- Incus's per-NIC anti-spoofing rule came back after neither, nor after `systemctl restart incus` (the
  container kept running): only after `incus restart <name>`. `thinny doctor` does not look for it:
  `checks.firewall` compares `inet thinny` only, so it says OK while the rule is gone **[static]**.

### Move (2026-11-17)
**[tested]** once, thinny 7a4444d on both servers: from this workstation (Ubuntu 26.04, Incus 6.0.5) to a
Debian 13 VM on it (QEMU/KVM, 4 CPUs, 6 GiB, Incus 6.0.4, sudo 1.9.16p2, no Tailscale, no firewall of its
own), through `sudo -n` there. The instance: Webapp, local access, no group, lock, mailbox or phone line,
2.8 GiB of disk (3.3 GiB on the new server).

- Exit 0 after 626 s, about ten and a half minutes of them down: about 7 min from the start to the end of
  `incus export` (the stop included), 25 s for the copy and the transfer over SSH, 2 min 30 s for
  `incus import`, 27 s for the setup (from both servers' logs and traces). Debian's sudo passed the 2.9 GB
  archive on stdin.
- The new server gave it another port and address and the `host_name` `thinny-<port>`; its MAC address
  stayed. `volatile.idmap.base` changed, and everything under its home was still its account's
  (model-guard's mount point `nobody`'s). systemd-networkd had the new address and the container reached
  the internet.
- Kept: `.env` (same checksum, so the password) and `SOUL.md` identical, `state.db` the same size;
  `config.yaml` had the new `webapp.port` and `public_url`. The gateway was active, the Webapp answered 302
  and its login page 200, `doctor` exit 0 with all the instance's checks OK. The new server had no release
  and got the old one's in `/var/lib/thinny/release`.
- Docker data: a busybox container (`--restart unless-stopped`, overlay2) with a named volume, made in the
  instance before the move, was up after it, with the volume's file.
- A reboot of the new server: SSH back after about 36 s; the instance running, its gateway active, the
  Docker container up, both nft tables loaded, thinny's units active, `doctor` exit 0.
- The old server afterwards: the instance disabled, its container stopped with `boot.autostart` false,
  nothing of the archive left in `/var/lib/thinny`, `doctor` all OK. It was not rebooted.
- The new server logs `resuming the unfinished setup of <name>` for the instance it just received.

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
- Mail from the agent through `hermes send`: plain text only. himalaya v2: the skill's commands are gone.
  The user's address in the policy: thinny would have to keep it, e.g. in the registry; the agent greps it.
  Copies to Sent: one that fails after sending has the agent send the mail again.
- `df` in a container showing its disk, other than with a pool each: ZFS, whose quota statfs shows,
  is DKMS on Debian (rebuilt at each kernel update; a failed build leaves every instance down after a
  reboot) and needs a signed module under Secure Boot. LVM thin gives each container a filesystem of
  its own, but without compression (about 6 GB instead of 3 GB for an idle instance), and a full
  thin pool can damage every one of them. A `df` wrapper in the containers: only `df` would see it.
- Mail: a mail server on the host (inbound port 25, and mail from a VPS address lands in spam), Migadu
  (daily sending caps per account), Cloudflare's Email Routing and agent inbox (no mailboxes or IMAP,
  which Hermes needs). DNS records through Cloudflare's API in thinny: a one-time step, and sites use
  other DNS providers.

## 3. Open questions

- WebUI after a real Hermes update (its restart with the gateway is tested with fakes only), and
  with Firefox and over LAN through Caddy.

- The Google Sheet of the access trace is tested against a stand-in only: check a real Apps
  Script web app, its "ok" after the redirect, and the answer of one not open to anyone.
- `instance move` is experimental: one small Webapp instance moved once (section 1, Move). Not
  tried: a move that fails on the way, a large instance, WebUI, `lan`, `tailscale` or `url`
  access, a phone line, a group, a lock or the new server's model lock, sudo-rs on the new
  server, and a reboot of the old one.
- Package drift: `render.PACKAGES` copies the official image's Dockerfile lists (v0.23.1).
  When Hermes adds a system dependency the list must follow; containers get the list at
  `instance add` and before each Hermes update.
- Phone line: a whole `instance add` with one, and a call from Hermes, are untested on a server.
- Telegram: `instance telegram` against a real instance is untested on a server.
- Access trace: snapshots, `incus console --show-log` and other clients' file events are
  untested with the installed service.
- Mailboxes: not yet tried are a scheduled job's mail, mail from another Purelymail mailbox (dropped?),
  `move` and `uninstall` with a mailbox.
- Mail from the agent is untried on a server outside the Webapp (Telegram, a mail chat), and so are the
  gateway answering mail while the agent reads INBOX, and `--new-password`.
- Export: not tried are its time on a large `state.db` (2 s with 1.4 MB) and `hermes import` into a Hermes
  outside thinny. It follows Hermes's list of credentials, but mirrors its private any-depth names: check
  them when Hermes changes.
- Per-instance SSH and Tailscale are left to the users; SSH needs the host as jump host.
- The firewall check: a flush with Tailscale's tables around is untried (the VM had no Tailscale, and nothing
  was flushed on the workstation).
- A disk cannot shrink (nor can a pool): a smaller one means moving the container into a new pool by
  hand.
- Instances still see the server's number of CPUs, and the size and use of its disk through the
  folders thinny mounts into them (the policy and the access trace).
- Alerting from the server itself: the dashboard sends none, and the watchdog runs off-server.
- The dashboard on Debian 13 and Ubuntu 24.04, and a Grafana upgrade by unattended upgrades.
- Subscription usage on the dashboard is untested on a server: a day with a ChatGPT and a Claude instance
  (time and CPU per check, Anthropic's 429s). Also open: Codex's usage endpoint may answer 429 at 100 % (a gap
  where it matters most); the Claude plugin reads Claude Code's older answer (`utilization`) of 1 or less as a
  fraction, so 1 % or less could show as up to 100 % (upstream).
- Per-instance ChatGPT seats versus one API account per organisation (a local proxy such as
  CLIProxyAPI is parked).
