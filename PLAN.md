# hermes-manager: plan

Status: agreed after review. Next: round 0 (spike on the test server). No code yet.

## 1. Goal

Turn a Debian 13 server into a shared Hermes Agent server for a small organisation
of non-programmers, with one command, and add people
with a second command (`hermes-manager`).

The design below grew out of a hand-built test server (section 4). This project turns
it into a reproducible, public tool.

## 2. Requirements

In priority order.

1. **Person isolation is non-negotiable.** Every person has their own Linux account. One
   person must never reach another person's data: files, chats, credentials, Webapp,
   sandbox, browser. The operating system enforces it: separate accounts, hidepid, our own
   nft table, a root-only Tailscale socket where Tailscale is used. Hermes's own settings
   (managed scope, sandbox) are defence in depth inside one account. The model provider
   seeing traffic is accepted. There is no file sharing between people through Hermes.
2. **One central install.** One Hermes code tree (`/opt/hermes`) and one config
   (`/etc/hermes`, Hermes "managed scope") for everyone. One nightly update for everyone.
3. **Central policy:** pinned model and reasoning effort, model allowlist enforced by a
   plugin, approvals mode, plugin list, sandbox settings. People cannot change it from the
   Webapp or their settings. It is a guardrail, not enforcement: a signed-in Webapp user
   has a host shell as their own account and can get around it. `check` reports it when
   they do.
4. **Agent tools run in rootless Podman sandboxes.** No Docker daemon (docker group = root).
5. **Built-in Hermes Webapp per person** on loopback, always behind sign-in. How each person
   reaches it is chosen per person when they are added (section 6.6). The tool assumes
   nothing about the site's network.
6. **Unattended operation:** optional automatic nightly Hermes update with careful one-by-one
   gateway restarts; everything comes back after a reboot. OS updates and the reboot
   window are the admin's (section 6.3).
7. **Non-programmer users:** UI language and Webapp look preset per person.
8. **Admin stays root.**
9. **Public repository from day one** (section 3).

Non-goals: hiding data from the server admin (impossible while the admin is root on the
machine that runs the agent), multi-server fleets, a web UI for the admin, hardening the
base server (a documented prerequisite that `check` verifies), making the policy
tamper-proof against a person with a shell.

## 3. Public from day one

- No personal or site data in the repo: no IPs, hostnames, tailnet names, e-mails,
  organisation names, tokens, provider server ids. Site data lives on the server in
  `/etc/hermes-manager/site.toml`; the repo ships `site.example.toml` with placeholders.
- Secrets are generated on the server (Webapp sign-in) or entered by the admin on the
  server (ChatGPT device login). Never logged, never committed, never printed except the
  one-time hand-over of a person's Webapp password.
- Code, comments, docs and CLI output in English. User-facing strings the server shows
  people (UI language, the model-guard message) come from settings.
- Opinionated defaults (pinned model, approvals `smart`, browser on) are settings with
  defaults, not hardcoded.
- Provider-specific extras (netcup snapshots, morning watchdog) live in `extras/`, off
  unless configured.
- `local/` is git-ignored and holds the hand-built prototype for reference
  (`local/prototype/`: config and scripts pulled from the test server, the snapshot and
  watchdog scripts). It contains site data and must never be committed.

## 4. What we know

Hermes v0.23.1, Debian 13, rootless Podman 5.4. Sources are marked: **[proto]** seen on the
hand-built test server, **[tested]** tried on the test server during the review
(2026-11-12), **[src]** read in the Hermes source (also present in v0.23.1 unless noted),
**[static]** read in the source but not run.

The prototype ran one Linux account with two Hermes homes, so isolation between accounts
was never tested beyond the Webapp firewall rule (another account gets no connection
**[tested]**).

### Layout
- Hermes installed with the official installer into `/opt/hermes` (`HERMES_HOME` of the
  install, code in `/opt/hermes/hermes-agent`); `/usr/local/bin/hermes` is a 2-line
  launcher. Users borrow the code read-only **[proto]**.
- **`/opt/hermes` must be mode 711.** Hermes chmods its `HERMES_HOME` to 700 on almost every
  command (`_secure_dir`), so root's `hermes` with `HERMES_HOME=/opt/hermes` breaks every
  user's `hermes` (running gateways keep working, so it goes unnoticed) **[proto]**.
  `HERMES_HOME_MODE=0711` in root's environment keeps it **[src, newer source; verify on
  v0.23.1]**. `auth.json` saves chmod the parent regardless **[static]**.
- **One person = one Linux account with Hermes's default home `~/.hermes`.** The gateway
  unit is the standard `hermes-gateway.service` (`hermes gateway install`, needs linger)
  **[tested]**. This is the topology upstream supports: one gateway per OS user.
- **Each home builds its own Python venv** via `hermes pm repair` (15 s, 440 MB for a new
  account **[tested]**; ~0.9 GB per generation in newer source **[static]**). It cannot be
  shared across accounts. The automatic resync at launch after a code update calls
  `refuse_foreign_owned_venv(checkout)`, which may refuse for a root-owned checkout;
  `hermes pm repair` does not **[static]**. So the update runs `pm repair` per person.
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
- Ways around it for a person with a shell **[src/static]**: `HERMES_MANAGED_DIR` or
  `HERMES_BUNDLED_PLUGINS` in their own `.env` (read before the managed dir is resolved),
  `--safe-mode` (skips all plugins, model-guard included), `/yolo`, `/model` and
  `/reasoning` per session, a same-named plugin in `~/.hermes/plugins`, editing their own
  gateway unit. `check` looks for these.
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
- On the host as the person's account: the Webapp's host terminal and `shell.exec`, MCP
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
  person.
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
- **Loopback is shared by all Linux accounts.** An nft output rule on `lo` lets ports
  9120-9199 through only for the owner uid, root and listed proxy accounts **[proto, tested]**.
- Webapp look (light mode, text size, simple interface, visible models) is server-side
  `~/.hermes/ui-preferences.json`; applied from a template through
  `hermes_cli.ui_preferences.write_preferences`, run as the user with the home's venv
  Python **[proto]**. The template must include `known-models`. Some settings (e.g. the Bots
  plugin toggle) are browser localStorage only.
- Cookie names carry a hash of the home, so two Webapps on one hostname do not clash
  **[src]**.
- Without a ChatGPT login, the Webapp's first screen invites the person to connect other
  providers **[tested]**: do the login before the hand-over.
- `hermes -p <profile> webapp` does not lock a Webapp to one profile; profiles of one gateway
  share it **[docs]**.

### Network
- nft output rules: non-root accounts may not reach the tailnet (100.64.0.0/10,
  fd7a:115c:a1e0::/48) except MagicDNS port 53; root exempt (`meta skuid 0`) **[proto]**.
  Rootless Podman traffic leaves as the user, so sandboxes are covered.
- Tailscale socket root-only (drop-in: `RuntimeDirectoryMode=0700`, chmod 600 on the
  socket) **[proto]**; the plan's claim that any user could otherwise drive `tailscale
  serve` is verified in round 0.
- **Debian's `systemctl restart nftables` runs `nft flush ruleset`** and removes every table,
  Tailscale's included; restarting `tailscaled` restores them **[tested]**. Never restart
  `nftables.service`; load our table with `nft -f`.
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
- UNVERIFIED: a real update to a new release end to end.

### Per-person ChatGPT login
- `hermes auth add openai-codex --type oauth --no-browser` as the person; device code,
  ~15 min expiry **[proto]**. Tokens in `~/.hermes/auth.json`, mode 600 **[src]**. Never copy
  `auth.json`: refresh tokens are single-use.

### Capacity (measured on the prototype)
- ~0.8 GB RAM per idle person (gateway ~0.45 + sandbox ~0.35).
- Disk per person: venv 0.45-0.9 GB + sandbox image ~4 GB.

## 5. Rejected options (and why)

- Docker: docker group = root.
- One Linux account per department with a Hermes home per person: colleagues can read each
  other's chats, ChatGPT tokens and Webapp passwords (host shell, same uid).
- Hermes profiles per person in one account: the Webapp shows all profiles; `-p` does not
  lock it.
- Hermes's built-in "Web Access" (desktop app): needs a Nous account per person, plain
  HTTP on the LAN.
- Plain HTTP as the LAN default: unencrypted, and the browser turns off Webapp features.
- Third-party web UI (hermes-webui): worse than the built-in Webapp for this use.
- One shared ChatGPT subscription for several people: against the provider's terms.
- Editing Hermes source: everything works through managed scope, bundled-but-excluded
  plugins and catalog plugins. Keep it that way so updates stay safe.

## 6. Approach

### 6.1 Repository layout

```
install.sh                    thin bootstrap: packages, files, then `hermes-manager apply`
hermes-manager                admin CLI, Python 3 stdlib only, runs as root
site.example.toml             every site setting, commented
files/                        installed as-is
  hermes-podman
  hermes-nightly-update (+ .service, .timer)
  model-guard/
templates/                    rendered from site.toml: managed config + .env, UI template,
                              nft table + its unit, Caddy sites
extras/                       later: netcup snapshot, morning watchdog
tests/                        pytest: rendering, registry, port allocation, name validation
```

### 6.2 State on the server

```
/etc/hermes-manager/site.toml          admin-edited settings
/var/lib/hermes-manager/people.json    registry written by hermes-manager
/etc/hermes/                           Hermes managed scope, rendered from site.toml
/etc/hermes-manager/hermes-manager.nft our own nft table, regenerated from the registry
```

- Registry per person: Linux account, uid, Webapp port, access method, URL, state
  (`pending`, `active`, `disabled`). Uids and ports are never reused.
- Our nft table uses numeric uids, is checked with `nft -c` before loading, is loaded with
  `nft -f` by our own unit at boot, and never touches the admin's tables. `check` verifies
  it is loaded.
- Root never writes inside a person's home: everything there runs as the person
  (`runuser`), so a planted symlink cannot redirect a root write.
- `hermes-manager` and the nightly update share one lock.

### 6.3 Prerequisites (the admin's)

Documented in the README, verified by `check` with the exact fix:
- warnings: SSH key-only and on a private network, firewall input policy, unattended
  upgrades with a reboot window after the update window;
- **FAIL** (they are part of person isolation): `/proc` hidepid, root-only Tailscale socket
  when Tailscale is installed.

### 6.4 `install.sh`

Idempotent; a rerun repairs and never destroys data or updates Hermes:
packages (podman, uidmap, passt, fuse-overlayfs, git, curl, python3; Caddy when an access
method needs it), the official Hermes installer pinned by version into `/opt/hermes` only
when no checkout exists, `HERMES_HOME_MODE=0711` for root's `hermes`, launchers, wrappers,
model-guard + language pack (pinned sha) + git excludes, nightly timer, `hermes-manager`
itself, then `hermes-manager apply` renders `/etc/hermes` and the rest.

### 6.5 `hermes-manager`

```
hermes-manager add-person <name> [--access lan|tailscale|url=<URL>|local]
                                     account, linger, home, venv, port, Webapp password,
                                     language, look, gateway, access, firewall
hermes-manager access <name> [...]   change the access method (restarts the gateway)
hermes-manager login <name>          ChatGPT device login (before the hand-over)
hermes-manager remove-person <name>  disable: gateway stopped, linger off, account locked,
                                     access removed, firewall regenerated, sandboxes removed
hermes-manager list                  people, ports, URLs, gateway state
hermes-manager check                 isolation and health checks (section 7)
hermes-manager apply                 re-render /etc/hermes, firewall, access; restart
                                     gateways one by one where needed
```

- Names: lowercase ASCII, validated (Linux account and URL safe).
- `add-person` records `pending` first and can be rerun to finish after a failure.
- Gateway restarts happen only where unavoidable, and are announced.

### 6.6 Access per person

`add-person` asks (or takes `--access`; `site.toml` holds the default):

```
How will alice open her Webapp?
  1) Office network (LAN)  https://<lan-ip>:<port>/   built-in HTTPS proxy
  2) Tailscale             https://<machine>.<tailnet>.ts.net:<port>/
  3) Own address           you enter the URL (Cloudflare, your proxy, ...)
  4) Local only            no network access; open via an SSH tunnel
Choice [1]:
```

- **LAN:** a Caddy site on the LAN address and the person's port with Caddy's local CA
  (`tls internal`); `public_url` set. The README explains trusting the CA on office PCs.
- **Tailscale:** `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`; offered only
  when Tailscale is installed.
- **Own address:** `public_url` = the URL entered; asks which account runs the proxy (root by
  default) for the firewall; prints "forward to `http://127.0.0.1:<port>`".
- **Local only:** `public_url = http://127.0.0.1:<port>/`; `ssh -L <port>:127.0.0.1:<port>`
  (to be verified in round 0).
- One method per person (a Webapp accepts only its own `public_url` address).

### 6.7 Settings (`site.toml`, sketch)

```toml
[hermes]
min_release_age_hours = 48
auto_update = true
update_time = "03:00"

[policy]
model_provider = "openai-codex"
model = "<model id>"
allowed_models = ["<model id>"]
reasoning_effort = "high"
approvals = "smart"
browser = true
language = "cs"
language_pack = { name = "<plugin>", sha = "<commit>" }

[sandbox]
image = "docker.io/nousresearch/hermes-sandbox:desktop"
cpu = 2
memory_mb = 4096

[webapp]
port_range = [9120, 9199]

[access]
default = "lan"                  # lan | tailscale | url | local
lan_address = "<server LAN IP>"
proxy_users = []                 # extra accounts allowed to reach Webapp ports

[network]
blocked_networks = []            # e.g. the office LAN; tailnet blocked when Tailscale is used
```

### 6.8 Nightly update

Ported from the prototype with these changes:
- `auto_update = false` only checks and writes the status file.
- People from the registry, not from unit files.
- If the snapshot extra is configured, that night's snapshot must have succeeded.
- After `hermes update`: `hermes pm repair` as each person, then `gateway restart --yes` one
  by one with the checks; stop at the first failure.
- Pull the sandbox image per person and recreate idle sandboxes.
- After the update: model-guard and the language pack still enabled.
- Report when the OS needs a reboot (`/run/reboot-required`); never reboot.

## 7. Checks (`hermes-manager check`)

- Install: `/opt/hermes` 711, launchers present, model-guard + language pack enabled for
  every person, nightly timer enabled, last update status.
- Per person: gateway active, `public_url` set and the Webapp answering at it, Webapp port
  refused for another account, no bypass (`HERMES_MANAGED_DIR`/`HERMES_BUNDLED_PLUGINS` in
  `.env`, own `model-guard`, changed gateway unit), unexpected loopback listeners.
- Sandboxes: running ones reach the internet, `pasta` in a `run-*.scope` under
  `hermes-sandboxes.slice`; blocked networks unreachable.
- Firewall: our table loaded.
- Prerequisites (section 6.3) with fixes.

## 8. Rounds

Each round ends with a stop for review and a commit.

0. **Spike on the test server** (disposable): loopback isolation between accounts;
   a real update from an older release with person accounts (`pm repair`, `restart --yes`,
   model-guard and language pack after it); our own nft table loading at boot and on
   reload; the Tailscale socket claim; whether a sandbox reaches host ports; access option
   4 through an SSH tunnel (loopback `public_url`; one-use link through a root tunnel);
   Caddy's certificate warning in a real browser; `HERMES_HOME_MODE` on v0.23.1.
1. Skeleton, `site.example.toml`, settings + registry code, rendering (managed config, nft
   table, Caddy, units), unit tests, README section "what is and is not protected".
2. `hermes-manager` add/access/login/remove/list/check/apply and the thin `install.sh`,
   tested on the test server from a clean-base snapshot.
3. Nightly update, tested with a real update.
4. Full run from the clean base: install twice, 2-3 people, every access method, `check`,
   isolation, update, reboot.
5. README, LICENSE (MIT), extras without personal data, final scan for leaked site data.

Test server: Hermes removed down to a "clean base" (Tailscale, SSH, monitoring and the
prerequisites stay), snapshotted, and restored before each installer test.

## 9. Open questions

- Alerting from the server itself (no mail sender today; the watchdog runs off-server).
- A shared read-only image store for rootless Podman if disk becomes a problem.
- Per-person ChatGPT seats versus one API account per organisation (a local proxy such as
  CLIProxyAPI is parked).
