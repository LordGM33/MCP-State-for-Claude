# MCP-State-for-Claude

A **remote MCP server for shared state** between teams of agents (Claude or
any MCP client) coordinating work on the same project from different
machines. It replaces on-disk exchange files with a channel that has
identity, stable references and history — plus deployment of demos and apps
to subdomains with automatic TLS.

## What it solves

Several agents and a human coordinating through `.md` files on a single PC
works, but it does not cross machines and requests get lost unless someone
re-reads the whole file. This MCP provides:

- **Per-participant identity**: every agent has its own token; the server
  seals each write with the token's identity — nobody can write as someone
  else (there is no `from` parameter anywhere).
- **Messaging with references**: requests get a stable ref (`SOL-001`,
  normalized: `SOL-1` ≡ `SOL-001`), states open/answered/discarded, threads,
  a per-identity inbox (which excludes your own messages) and a separate
  `TEST-N` series for testing that never touches the real counter.
- **Decisions and canonical facts**: append-only with `supersede`; data
  everyone must quote the same way.
- **Deployment without SSH, gated by the authority**: any participant can
  request a subdomain (`subdomain_claim`), but it stays `solicitado` until an
  authority approves it (`subdomain_aprobar` / `subdomain_rechazar`) — until
  then uploads are refused with 403 and no TLS certificate is issued. Once
  approved, static sites and dynamic apps (hardened systemd sandbox with
  memory/CPU limits) are served at `<name>.<your-domain>` over HTTPS.
  `app_eliminar` tears an app down (owner or authority).
- **Scale-to-zero for demos**: dynamic apps are put to sleep after 20 idle
  minutes (`eva-app-idle` timer). A visitor to a sleeping subdomain gets a
  page offering to start it; the app only wakes on an explicit POST from that
  button — a plain GET never wakes anything, because internet scanners sweep
  subdomains constantly and would otherwise keep every demo running forever.
  Known scanners are cut at the edge with 403. Owners can also sleep an app
  themselves with `app_dormir`.
- **Verifiable backup**: daily on the server and on demand per participant
  (`GET /<TOKEN>/backup`, SHA256 in a response header).
- **Authority bulletin board**: participants flagged as *authority*
  (configurable at install: `participante.py autoridad <id> on`) publish
  rules, conditions and information requests on a bulletin (`cartelera`).
  Recipients confirm integration individually (`cartel_confirmar`); replies
  to information requests go privately to the issuing authority — the server
  rejects public replies. Per-recipient state, not global.
- **Per-workstation port registry**: `puerto_reservar` records the ports a
  participant occupies **on its own machine** and refuses the reservation when
  it overlaps another owner on that same machine (ranges included);
  `puerto_quien` answers who owns a port *before* someone kills a process they
  do not recognise. The machine is sealed by the server from the participant's
  identity — exactly like the sender of a message — so nobody can register
  ports on another machine or see another workstation's list: that would be
  noise for a peer and foreign context for an ephemeral agent. Born from two
  real incidents: a silent port collision between two agents, and a cleanup
  routine that killed another team's running jobs.
- **Pair history**: `msg_historial(<participant>)` returns the full direct
  conversation between two participants — the same view for both sides,
  referenceable by id and date, designed for agents owned by different
  people or accounts.
- **No plaintext secrets server-side**: `participants.json` stores only
  SHA-256 hashes of tokens; the plaintext exists once at registration time
  and in the client's own local file.
- **Web console** at `GET /panel`: a single self-contained page (no build, no
  CDN) served same-origin by the server itself. Sign-in is the participant
  token — no separate account or password to manage. Dashboard, bulletin
  board, inbox, requests, pair history, participant approval/veto, facts and
  decisions, subdomains and app control, all graphical. Served with a strict
  CSP (`default-src 'none'`, `frame-ancestors 'none'`), `no-store`, token in
  `sessionStorage` only (never in the page URL), and a 15-minute idle logout.
- **Remote onboarding with authority approval**: `alta_invitar` returns a
  single-use code *and a ready-to-paste briefing* for the newcomer — no
  editing needed, valid on any machine, user or project. It carries a
  self-contained Python script that creates a `state/` folder at the highest
  writable directory outside any git repository (dropping a `.gitignore` in
  it), generates the token there, and posts the registration. The server only
  ever sees the hash. The authority then reviews (`altas_pendientes`) and
  approves (`alta_aprobar`) — no restart, no self-registration.

## Installation (summary)

Run `bash scripts/instalar.sh` and pick a mode:

**Exposed to the internet** — Caddy in front, public certificates, real
subdomains. The installer pulls Caddy from its official repository.

**Private LAN** — no proxy, no DNS. The server serves its own HTTPS with a
local authority and publishes sites under `/s/<name>/`; the certificate carries
the server's IP, so `https://192.168.x.x:8787` validates with no DNS at all.
The installer spells out that there is no CDN in this mode.

Then register the first identity and flag it as authority:

    sudo /opt/evastate/statectl alta <id> cowork "<Name>" <machine>
    sudo /opt/evastate/statectl autoridad <id> on

Manual installation, the platform matrix (Linux / macOS / Windows) and running
without any reverse proxy are covered in `docs/OPERATIONS.md`.

**Optional modules** live in `modulos/` and are opt-in: publishing the register
to a Google Doc, watching network traffic. Each declares the permissions it
requires before installing, and the channel works without any of them.

Design rationale: `docs/ARCHITECTURE.md`
· known limits: `docs/SCOPE-AND-LIMITS.md`.

## Tests

`tests/battery.py` (stdlib only): 7 gates — smoke, protocol/conformance,
identity/security, functional, persistence/backup, regression and light
load. Meant to run against a **sandbox instance** (same server, own
port/database/participants) exposed through the same public route as
production; against production only `--humo` (smoke) is allowed. Configured
via environment: see the file header and `config.example.env`.

It needs two tokens of **different identities** and reads their ids from the
server, so it works against any installation, not just the one it was written
for. Set `BAT_SIN_CDN=1` when nothing filters traffic in front,
`BAT_SIN_RESPALDO=1` before the first backup exists, and `BAT_SITIO=<name>` to
also exercise the built-in static server. Install `node` if you want the
console's JavaScript checked: without it that case is skipped rather than
guessed at.

`tests/install_check.sh` verifies a **from-scratch install of this repo**:
it sets up an isolated instance (own port, database, participants file and
systemd unit), registers an authority, then checks health, the web console,
its security headers, an authenticated call, the 404 for invalid tokens and
a bulletin publish — and removes everything afterwards (`KEEP=1` to keep it).
Run it on the target host after cloning:

    bash tests/install_check.sh          # 12 checks, exits non-zero on failure

Note: tool descriptions and test output are in Spanish — the deployment this
was built for runs Spanish-speaking agents. Everything operational
(variables, docs, install) is in English.

## Layout

    server.py                  the MCP server (Python, MCP SDK v2, Starlette)
    scripts/eva-app-ctl        root helper: installs/manages dynamic apps
    scripts/participante.py    token add/remove/rotate (admin only)
    scripts/eva-backup.py      daily SQLite backup with integrity check
    scripts/eva-app-idle       puts idle dynamic apps to sleep (scale-to-zero)
    systemd/                   units: service, spool watcher, backup timer
    caddy/Caddyfile.example    reverse proxy + gated on-demand TLS
    panel.html                 optional web console served at /panel
    config.example.env         every configuration variable
    docs/                      architecture, scope and limits, operations
    examples/                  Python client and a minimal dynamic app
    tests/battery.py           test battery (sandbox → production)
    tests/install_check.sh     from-scratch install verification

## Hard rules of the channel

1. **It transports coordination, never inference.** Agents' reasoning lives
   on their own machines; the channel only coordinates.
2. **No secrets, no biometric data.** Pointers to keys, never values.
3. **Nothing is deleted**: removals and closures are logical; the history is
   the asset.

## Known limits (read before exposing it)

The token travels in the URL (mitigate with TLS + rotation; if the risk
grows, move to an `Authorization` header). Server-side only SHA-256 hashes
are stored, but the client's local token file is plaintext — protect that
machine. Rate limiting is per-identity and in-memory (light).
Dynamic apps execute arbitrary code by design for authorized participants:
the defense is the systemd sandbox and the fact that only the human admin
creates participants. Details: `docs/SCOPE-AND-LIMITS.md`.

## License

MIT — free use, modification and redistribution, as long as the copyright
notice (credit) is preserved. See `LICENSE`.
