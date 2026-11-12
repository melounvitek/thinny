# hermes-manager: plan

Status: plan for review. No code yet.

## 1. Goal

Turn a fresh Debian 13 server into a shared Hermes Agent server for a small organisation
of non-programmers, with one command,
and add departments and people with a second command (`hermes-manager`).

The design below already runs by hand on a test server (section 4). This project turns
that hand-built setup into a reproducible, public tool.

## 2. Requirements

In priority order.

1. **Department isolation is non-negotiable.** One department must never reach another
   department's data: files, chats, credentials, Webapp, sandbox. Inside a department,
   colleagues reaching each other's files is acceptable (sometimes wanted); each person
   still has their own conversations. The model provider seeing traffic is accepted.
2. **One central install.** One Hermes code tree (`/opt/hermes`) and one locked config
   (`/etc/hermes`, Hermes "managed scope") for everyone. One nightly update for everyone.
3. **Locked policy users cannot change:** pinned model and reasoning effort, model
   allowlist enforced by a plugin, approvals mode, plugin list, sandbox settings.
4. **Agent tools run in rootless Podman sandboxes.** No Docker daemon (docker group = root).
5. **Built-in Hermes Webapp per person**, private: no public inbound ports on the server.
   Exposure through Tailscale today; Cloudflare tunnel + Access later (section 6.6).
6. **Unattended operation:** nightly Hermes update with careful one-by-one gateway restarts,
   OS security updates with a reboot window, everything comes back after a reboot.
7. **Non-programmer users:** UI language and Webapp look preset per person.
8. **Admin stays root**, SSH only over the private network.
9. **Public repository from day one** (section 3).

Non-goals: hiding data from the server admin (impossible while the admin is root on the
machine that runs the agent), multi-server fleets, a web UI for the admin.

## 3. Public from day one

- No personal or site data in the repo: no IPs, hostnames, tailnet names, e-mails,
  organisation names, tokens, provider server ids. Site data lives on the server in
  `/etc/hermes-manager/site.toml`; the repo ships `site.example.toml` with placeholders.
- Secrets are generated on the server (Webapp sign-in) or entered by the admin on the
  server (monitoring token, ChatGPT device login). Never logged, never committed,
  never printed except the one-time hand-over of a person's Webapp password.
- Code, comments, docs and CLI output in English. The Hermes UI language is a setting.
- Opinionated defaults (pinned model, approvals `smart`, browser on) are settings with
  defaults, not hardcoded.
- Provider-specific extras (netcup snapshots, morning watchdog) live in `extras/`, off
  unless configured.
- `local/` is git-ignored and holds the hand-built prototype for reference
  (`local/prototype/`: config and scripts pulled from the test server, the snapshot and
  watchdog scripts). It contains site data and must never be committed.

## 4. What the prototype taught us (verified on the test server)

Hermes v0.23.1, Debian 13, rootless Podman 5.4.

### Layout
- Hermes installed with the official installer into `/opt/hermes` (`HERMES_HOME` of the
  install, code in `/opt/hermes/hermes-agent`); `/usr/local/bin/hermes` is a 2-line
  launcher. Users borrow the code read-only.
- **`/opt/hermes` must be mode 711.** Any `hermes` command run as root resets it to 700
  and every user's `hermes` then fails with "Permission denied" (running gateways keep
  working, so it goes unnoticed). Run root `hermes` with `HERMES_HOME=/opt/hermes`.
- **Each person needs a standalone `HERMES_HOME`, not a profile.** Profiles inside one
  home are multiplexed into one gateway and the Webapp lists all of them; a standalone
  home sees only itself and gets its own systemd user unit `hermes-gateway-<hash>`.
- **Each home builds its own Python venv** (~430 MB, `<home>/installs/*/environments/*/venv`)
  via `hermes pm repair`; `hermes` fails in a new home until that is done. This is how
  Hermes's package manager works for borrowed installs; it cannot be shared. After a code
  update every home's venv must be re-synced (any `hermes` call as that user does it).
- Department = Linux account with linger (`loginctl enable-linger`), home dir 700.
  Rootless Podman images are per account (~4 GB sandbox image each).
- `/proc` mounted `hidepid=invisible` with a `proc-viewers` group for logind.

### Managed scope (`/etc/hermes`)
- `config.yaml` values win over each user's config: `model.provider: openai-codex`,
  `model.default`, `agent.reasoning_effort: high`, `plugins.enabled`, `plugins.disabled: []`,
  `terminal.backend: docker` with the Podman image and limits, `security.redact_secrets`,
  `approvals.mode: smart`.
- Managed `plugins.enabled` **replaces** the user's list wholesale, so it must list every
  plugin that must be on, and users cannot disable them.
- `.env` is world-readable: never secrets. It sets `HERMES_DOCKER_BINARY`.
- `approvals.mode` must not be `off`: with approvals off, computer_use runs
  "unrestricted" and looks for `cua-driver` on the host instead of in the sandbox, which
  breaks the Bot Screen.

### Model guard
- Bundled plugin `model-guard` copied into `/opt/hermes/hermes-agent/plugins/`, listed in
  `.git/info/exclude`, enabled from the managed scope. Its `pre_llm_call` hook blocks any
  turn whose model is not in `/etc/hermes/allowed-models.txt` (fail closed when empty).
- Known bypass: a user plugin with the same name in the user's own plugins dir shadows it.
  Effort cannot be enforced by the hook (it does not receive the effort).

### UI language
- Catalog plugin `hermes-lang-cs` (pinned by commit sha) in the bundled plugins dir +
  managed `plugins.enabled`; `display.language cs` per home. Some placeholder strings stay
  English (not in the pack).

### Sandboxes
- **Sandbox network dies on gateway restart** unless the container is started outside the
  gateway's cgroup: rootless `pasta`/`conmon` are children of the `podman run` the gateway
  spawns, so the unit's restart SIGKILLs them and the container keeps running with no
  network. Fix: `HERMES_DOCKER_BINARY=/usr/local/bin/hermes-podman`, a wrapper that runs
  `podman run|start|restart` via `systemd-run --user --scope` in `hermes-sandboxes.slice`.
  **The wrapper's basename must contain "podman"** (Hermes detects the runtime by name).
- Sandboxes start on first tool use, not at boot.
- A container whose conmon died lands in "Exited (-1)" after the first `rm -f`; a second
  `rm -f --ignore` removes it.
- Chromium in a sandbox was once found stopped (state T) and the browser tool timed out on
  the CDP handshake; `kill -CONT` fixed it. Root cause unknown.

### Webapp
- Per home: `webapp.port` (we use 9120-9199), `webapp.host 127.0.0.1`, `webapp.public_url`,
  basic auth via `HERMES_DASHBOARD_BASIC_AUTH_USERNAME/PASSWORD/SECRET` in the home's `.env`.
  Generate fresh values per person; never copy another home's `.env`.
- **Loopback is shared by all Linux accounts**: any account can reach any Webapp's sign-in
  page. Fixed with an nft output rule on `lo`: ports 9120-9199 only for the owner uid and
  root (root runs `tailscale serve`, later the reverse proxy). Sandboxes cannot reach host
  loopback anyway.
- Webapp look (light mode, text size, simple interface, visible models) is server-side
  `<home>/ui-preferences.json`; applied from a template through Hermes's own
  `hermes_cli.ui_preferences.write_preferences`, run with the home's venv Python as the
  user (system python3 cannot import it). The template must include `known-models` or the
  model picker shows every model as new. Some settings (e.g. the Bots plugin toggle) are
  browser localStorage only.
- Two Webapps on the same hostname (different ports) share cookies in the browser.

### Network
- nft output rules: non-root users may not reach the tailnet (100.64.0.0/10,
  fd7a:115c:a1e0::/48) except MagicDNS port 53; root exempt (`meta skuid 0`). Rootless
  Podman traffic leaves as the user, so sandboxes are covered. Tailscale ACLs cannot do
  this (they do not distinguish Linux users on one node).
- Tailscale socket root-only (drop-in: `RuntimeDirectoryMode=0700`, chmod 600 on the
  socket), otherwise any user can drive `tailscale serve` etc.
- A monitoring agent (Beszel) needs one explicit tailnet exception for its hub.
- nft rules were applied with an auto-rollback timer (`systemd-run --on-active=240`) and
  verified (SSH, Webapps, internet from user and sandbox) before persisting.

### Nightly update (`hermes-nightly-update`, root timer 03:00)
- Plain `hermes update` restarts only gateways in its own scope; other accounts' homes keep
  running old code. Hence a custom script: wait until the latest stable release is 48 h old,
  `hermes gateway migrate --dry-run` per home (DB migrations make a downgrade impossible),
  back up `/etc/hermes` + local plugins, `hermes update --yes --no-gateway-restart` with
  `HERMES_HOME=/opt/hermes`, fix permissions, restore local plugins if the update removed
  them, pre-sync each home's venv while its gateway runs, then restart gateways one by one
  (`hermes gateway restart` drains the running turn), check unit active + Webapp 2xx/3xx +
  model-guard enabled, stop at the first failure, then check every sandbox's network and
  remove dead ones. Status file + log. Modes `--check`, `--now`, `--restart-only`.
- Homes are discovered from `/home/*/.config/systemd/user/hermes-gateway*.service`.
- UNVERIFIED: a real update to a new release end to end (only `--check` and
  `--restart-only` have run).

### Per-person ChatGPT login
- `hermes auth add openai-codex --type oauth --no-browser` as the user in the person's home;
  device code, ~15 min expiry. Never copy `auth.json`: refresh tokens are single-use, a copy
  logs the original out.

### Capacity (measured)
- ~0.8 GB RAM per idle person (gateway ~0.45 + sandbox ~0.35).
  Disk: ~0.45 GB venv per person, ~4 GB image per department.

## 5. Rejected options (and why)

- Docker: docker group = root.
- One Linux account with Hermes profiles per person: the Webapp shows all profiles, no lock.
- Third-party web UI (hermes-webui): worse than the built-in Webapp for this use.
- One shared ChatGPT subscription for several people: against the provider's terms.
- Editing Hermes source: everything above works through managed scope, bundled-but-excluded
  plugins and catalog plugins. Keep it that way so updates stay safe.

## 6. Proposed approach

### 6.1 Repository layout

```
install.sh                    bootstrap (base part + Hermes part), idempotent
hermes-manager                admin CLI, Python 3 stdlib only, runs as root
site.example.toml             every site setting, commented
files/                        installed as-is
  hermes-podman
  hermes-nightly-update (+ .service, .timer)
  hermes-seed-ui
  model-guard/
  ui-preferences.template.json
templates/                    rendered from site.toml (managed config, nft table, units)
extras/
  netcup-snapshot/            nightly online snapshot via the netcup SCP API (runs off-server)
  nightly-watch/              morning check of the nightly jobs, alerts only on failure
tests/                        pytest: rendering, registry, port allocation, name validation
```

### 6.2 State on the server

```
/etc/hermes-manager/site.toml          admin-edited settings
/var/lib/hermes-manager/people.json    registry written by hermes-manager
/etc/hermes/                           Hermes managed scope, rendered from site.toml
/etc/nftables.d/hermes-manager.nft     own nft table, regenerated from the registry
```

Own nft table instead of editing the admin's `/etc/nftables.conf`, so the base firewall
stays the admin's and `hermes-manager apply` can regenerate its part at any time.

Homes: `/home/<dept>/hermes/<person>` (one standalone `HERMES_HOME` per person).

### 6.3 `install.sh`

Idempotent; a rerun repairs and never destroys data. Two parts, each step optional via
`site.toml`:

- **Base:** SSH key-only, unattended upgrades with a reboot window after the nightly update,
  hidepid, Tailscale root-only socket, base nft input policy (drop, SSH only on the private
  interface). Skippable as a whole for servers the admin already hardened.
- **Hermes:** packages (podman, uidmap, passt, fuse-overlayfs, git, curl, python3), official
  Hermes installer pinned to the stable channel into `/opt/hermes`, permissions, launchers,
  `/etc/hermes` rendered, model-guard + language pack (pinned sha) + git excludes, wrappers,
  nightly timer, `hermes-manager` itself.

### 6.4 `hermes-manager`

```
hermes-manager add-dept <dept>             Linux account, linger, home 700, image pull
hermes-manager add-person <dept> <person>  home, venv, port, Webapp password, language,
                                           look, gateway unit, exposure, firewall, then
                                           offers the ChatGPT device login
hermes-manager login <dept> <person>       ChatGPT device login again
hermes-manager remove-person <dept> <person> [--keep-data]
hermes-manager remove-dept <dept> [--keep-data]
hermes-manager list                        people, ports, URLs, gateway state
hermes-manager check                       isolation and health checks (section 7)
hermes-manager apply                       re-render /etc/hermes, firewall, exposure
```

Names: lowercase ASCII, validated (Linux account and URL safe). Ports allocated from the
registry. Gateway restarts are never done implicitly except where unavoidable and announced.

### 6.5 Settings (`site.toml`, sketch)

```toml
[hermes]
channel = "stable"
min_release_age_hours = 48
update_time = "03:00"

[policy]
model_provider = "openai-codex"
model = "<model id>"
allowed_models = ["<model id>"]
reasoning_effort = "high"
approvals = "smart"
browser = true
language = "cs"

[sandbox]
image = "docker.io/nousresearch/hermes-sandbox:desktop"
cpu = 2
memory_mb = 4096

[webapp]
port_range = [9120, 9199]
exposure = "tailscale"          # tailscale | cloudflare | none

[exposure.tailscale]
hostname = "<machine>.<tailnet>.ts.net"

[base]
enabled = true
ssh_interface = "tailscale0"
reboot_time = "04:00"
```

### 6.6 Exposure

Pluggable per site:
- `tailscale`: one `tailscale serve --https=<port>` per person (today's setup).
- `cloudflare` (later, out of scope for rounds 1-4): one `cloudflared` tunnel, one wildcard
  DNS record, a root-run reverse proxy mapping `<person>-<org>.<domain>` to
  `127.0.0.1:<port>`, one Cloudflare Access policy per person via API, `webapp.public_url`
  per home. Subdomains rather than `/<person>` paths: separate browser origins per person,
  and the Webapp under a subpath is untested. Free Universal SSL covers one subdomain level.
- `none`: local only.

## 7. Checks (`hermes-manager check`)

What we verify by hand today:
- `/opt/hermes` 711, launchers present, model-guard + language pack enabled in every home.
- Per home: gateway unit active, Webapp 302 for owner and root, connection refused for
  another account, `hermes profile list` shows only itself.
- Per department: running sandboxes reach the internet, `pasta` is in a `run-*.scope` under
  `hermes-sandboxes.slice`, not in a gateway unit; tailnet hosts unreachable as the user.
- Tailscale socket 600, hidepid active, nightly timer enabled, last update status.

## 8. Rounds

Each round ends with a stop for review and a commit.

1. Skeleton, `site.example.toml`, settings + registry code, rendering of the managed config
   and the nft table, unit tests. Local only.
2. `install.sh` base and Hermes parts. Local dry checks only.
3. `hermes-manager` add/remove/list/check/apply. Local tests with fakes where possible.
4. On the test server: wipe only Hermes (keep Tailscale, SSH, monitoring), run the installer,
   create two departments with people, run `check`, verify isolation, Webapps, sandboxes,
   `hermes-nightly-update --restart-only`, reboot.
5. README (install, add people, what is and is not protected, rejected options), LICENSE,
   extras without personal data, final scan for leaked site data.

## 9. Open questions

- License (MIT, like Hermes Agent?).
- How much of the base part belongs in this project versus a documented prerequisite.
- Cloudflare exposure: which domain scheme per organisation.
- Per-person ChatGPT seats versus one API account per organisation.
- Alerting from the server itself (no mail sender today; the watchdog runs off-server).
