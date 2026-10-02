# Linux system profile (phase 3)

One Python CLI (`mpm`) + one thin `sh` entry wrapper. Everything below is the
*product* behaviour on Ubuntu 22.04 / 24.04 and Debian 12 with system systemd
(D1/D2). Windows behaviour is unchanged except the fixes listed in
[Phase 3 Windows fixes](#phase-3-windows-fixes-d16d15).

```
linux/
├── bin/mihomo-proxy-management      # thin wrapper: locates the package, execs python
├── python/mpm/                      # the only implementation of the lifecycle logic
├── supply/mihomo.lock.json          # audited, digest-pinned supply lock
├── systemd/mihomo-proxy-management.service.template   # non-secret reference unit
└── tests/                           # host-free suite (run_tests.py orchestrator)
```

## Quick start

```bash
sudo mkdir -p /etc/mihomo-proxy-management
sudoedit /etc/mihomo-proxy-management/subscription.env   # 0600, see format below
sudo MPM_SOURCE_DIR=$PWD/linux ./linux/bin/mihomo-proxy-management install --enable
sudo ./linux/bin/mihomo-proxy-management status
```

`install` performs preflight, verified download, config render, unit install
and start. Start **never** implicitly enables the unit; `--enable` is an
explicit opt-in.

## Secret input format (`subscription.env`)

Plain `KEY=VALUE` lines, one per line, `#` comments allowed. The file must be a
regular file (no symlink), mode **0600**, owned by root. Any group/other bit
makes the CLI refuse (fail closed) before anything is written.

| Key | Required | Meaning |
|---|---|---|
| `MPM_SUBSCRIPTION_URL` | yes | foreign exit subscription URL (a credential) |
| `MPM_CN_SUBSCRIPTION_URL` | no | CN exit subscription URL; enables CN rules |
| `MPM_CN_HEALTHCHECK_URL` | with CN | probe URL used to decide CN liveness |
| `MPM_CONTROLLER_SECRET` | no | controller `secret`; absent/empty → randomly generated and stored 0600 at `/etc/mihomo-proxy-management/controller.secret` |

Same keys may come from the **process environment** of the install/configure
run only. If a key exists in *both* file and environment with different values
the CLI refuses and prints only irreversible fingerprints
(`sha256 12-hex`, never the values). A credential is never a CLI argument:
`--secret-file` takes a *path*.

## Support matrix & exit codes (D1/D9a)

| Item | Policy |
|---|---|
| Distributions | Ubuntu 22.04, Ubuntu 24.04, Debian 12; others → `UNSUPPORTED` (exit 4) with reasons |
| Architecture | `x86_64`: v3 asset when CPU has AVX2, otherwise `compatible`; `aarch64`: arm64 asset |
| systemd | system systemd only (`/run/systemd/system` present); containers without systemd refuse |
| Privileges | root (system profile); non-root preflight reports unsupported |
| Exit codes | 0 OK/DEGRADED, 1 generic failure, 2 fail-closed, 3 not-ready, 4 unsupported; CLI misuse = 2 via argparse |

## TUN → mixed-port DEGRADED (D3)

TUN requires `/dev/net/tun` present and writable. If not, install does **not**
abort and never tries to fix the host (no `modprobe`, no `ip`/`nft`/`iptables`,
no resolv.conf/systemd-resolved changes). It renders a **mixed-port only**
config and shouts a `!!...` DEGRADED banner with the exact reason, persists it
in `state/degraded.json`, and every later `status`/`start` repeats it. In
degraded mode: `tun.enable=false`, no `dns-hijack`, `dns.enhanced-mode` is
`redir-host` and `profile.store-fake-ip: false` — no fake-ip range, filter or
stored mapping is rendered at all, because mixed-port mode hijacks no traffic.

Routing and firewall in TUN mode are owned exclusively by mihomo
`auto-route`/`auto-detect-interface` (D4): the project writes no firewall or
route rules itself.

## CN provider semantics (D8)

Foreign and CN subscriptions are separate `proxy-providers`
(`subscription-foreign` / `subscription-cn`) with isolated `use:` groups.
Health is judged on node **`alive`** state (`/proxies` first, then the
provider's `all` list), never on node count: zero nodes, all-dead nodes or
unreadable liveness all mark the deployment **DEGRADED with an explicit
reason**, and the rendered CN rules become `GEOSITE,cn,REJECT` /
`GEOIP,cn,REJECT,no-resolve`. CN-EXIT therefore never selects a dead node and
never falls back to `DIRECT` or a foreign node (enforced by config audit +
tests); only an actual authenticated health check clears the state back to
`CN-EXIT`, a plain `configure` never does. When CN is not configured at all, no
CN rule of any kind is rendered (status: `DISABLED`).

## Lifecycle semantics (idempotent)

| Command | Re-run behaviour |
|---|---|
| `install` | unit/config `UNCHANGED` if content identical; verification runs again; never a second unit; `start` only when needed |
| `configure` | temp → 0600 → static audit → `mihomo -t -f <candidate> -d <state dir>` → atomic replace; the candidate, never the previous live file, is what gets validated (a first install has no live config); identical output = no write; failure keeps previous config, never restarts |
| `configure --check-only` | ExecStartPre path: validates on-disk config equals rendered config; strictly read-only — no config, no state, no backup and no command executed |
| `start` | refuses if a foreign unit owns mixed/controller/dns ports — reports the owning unit and **never kills it**; already-active = no second instance; waits for authenticated `/version` readiness |
| `stop` / `restart` | `systemctl` on our unit only (`KillMode=control-group`); restart = stop → confirm stopped → start → readiness |
| `status` | active vs enabled shown separately; partial (secret-free) view for non-readers, explicitly flagged |
| `update-subscription` | authenticated PUT + healthcheck for both providers; any failure = non-zero exit, no implicit restart |
| `uninstall` | plain: deletes unit, env file, controller secret, config, providers, backups, staging, libexec, share, CLI — **keeps** `etc/mihomo-proxy-management/overrides.d` (marked non-secret); dry-run first |
| `uninstall --purge --yes` | additionally deletes everything under project prefixes incl. overrides.d; path-guarded (`is_project_path`) |
| `test` | local, offline, authenticated controller checks only (D12); never a real egress-IP service; a recorded DEGRADED explains state but never masks a FAILed check — the report stays `FAILED` (non-zero) |

`ExecStartPre` executes the installed CLI wrapper directly as an absolute
executable — `/usr/local/bin/mihomo-proxy-management configure --check-only
--quiet`. The wrapper is a `#!/bin/sh` script, so prefixing an interpreter
(`python3 …`) there would make systemd parse a shell script as Python and the
unit could never start; `unit.audit()` rejects an interpreter-prefixed or
relative `ExecStartPre`. `ExecStart` is exactly `mihomo -d /var/lib/...` — no
`Environment=`, no secret, no negative `ConditionPathExists`, no pkill-style
process scanning. `systemd-analyze verify` gates the unit at install; if the
tool is absent the suite/install reports `NOT_RUN` and the content audit is
used instead (never silently "passed").

## Supply chain (D9b, fail closed)

`linux/supply/mihomo.lock.json` pins tag, exact asset name, full sha256 and
byte size for the three mihomo binaries and both geo files; official GitHub
host + https are enforced by the lock parser. Install steps (all abortible, in
order): exact-name asset selection (0 or >1 = fail) → 0700 staging download →
digest match → pinned size match → gzip magic → single-file extract (tar
members must be exactly one, traversal refused) → ELF magic + `e_machine`
architecture check → move into version dir → atomic `current` symlink switch.
Any failure installs nothing and starts nothing. Release metadata comes from a
read-only, credential-free GitHub API call; **an API failure never degrades
into an unverified download**. `meta-rules-dat` uses the rolling tag `latest`
— what pins it are the committed sha256 + size (see `notes` in the lock).
Real downloads are exercised only via mocks in tests; CI never uses a real
subscription.

## Overrides

`/etc/mihomo-proxy-management/overrides.d/*.conf` accepts a small allow-list
(`log_level`, `interval_minutes`, `fake_ip_range`, …). Security boundaries —
`allow-lan`, `external-controller`, `secret`, `mixed-port`, `tun`,
`dns-hijack`, any `url` — are denied. Files must be plain, non-symlink, no
group/other write, and values are validated (no credentialed URLs, bools,
CIDRs). Overrides.d is non-secret by contract, kept by plain uninstall, deleted
only by `--purge`.

## Phase 3 Windows fixes (D16/D15)

Fixed in this patch:
- `scripts/lib/common.ps1`: new `Invoke-MihomoApi` — **every** controller call
  (readiness, version, `/providers/proxies`, `/proxies`, PUT refresh in
  status.ps1 / update-subscription.ps1) now sends the `Bearer` secret from
  `subscription.env`; an empty secret throws (no unauthenticated fallback).
- `scripts/update-subscription.ps1`: controller failures exit non-zero and
  **never** auto-restart mihomo to hide the failure.
- `scripts/convert_sub.py`: subscription URLs are reduced to `scheme://host`
  before printing; node `server:port` pairs are hidden by default and only
  shown (masked, `***.example.invalid`) behind `--show-endpoints`; errors are
  scrubbed of the URL/credentials. No new protocol parsers were added (the
  `TODO: ss/trojan/tuic/vless` remains out of scope).
- `.gitignore` rewritten recursively (`**/subscription.env`, `**/*.env` with
  `!**/*.env.example`, `**/config.yaml`, provider yaml caches, geo/log/db,
  Linux `__pycache__`, Windows droppings), plus `.gitattributes` scoping
  `eol=lf` to Linux text only. Windows `.ps1` files keep their UTF-8 BOM —
  byte-level encoding churn is deliberately out of scope.

**Deferred (unchanged, disclosed):** the Windows supply chain — `install.ps1`
still downloads "latest" mihomo/wintun/geo without digest pinning and keeps
its best-effort fallback download behaviour. Phase 3 does not fix that; do not
treat a Windows install as supply-chain verified.

## Testing

```bash
cd linux && python3 tests/run_tests.py            # full battery + honest NOT_RUN
cd linux && python3 -m unittest discover -s tests -t tests
```

Everything is host-free: throwaway root prefixes, fake executors for
`systemctl`/`systemd-analyze`/`mihomo -t`, mock payloads/transport, and
runtime-generated `example.invalid` canaries that must never appear in any
output, state file, unit or log. Checks that need tools absent from a host
(e.g. `shellcheck`) are reported `NOT_RUN`, never silently skipped.
