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
- **Verifiable backup**: daily on the server and on demand per participant
  (`GET /<TOKEN>/backup`, SHA256 in a response header).
- **Authority bulletin board**: participants flagged as *authority*
  (configurable at install: `participante.py autoridad <id> on`) publish
  rules, conditions and information requests on a bulletin (`cartelera`).
  Recipients confirm integration individually (`cartel_confirmar`); replies
  to information requests go privately to the issuing authority — the server
  rejects public replies. Per-recipient state, not global.
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

1. A VPS with Python 3.12+, Caddy and systemd. Create the `evastate` user.
2. `server.py` to `/opt/evastate/`, scripts to `/usr/local/sbin/`, units
   from `systemd/` to `/etc/systemd/system/`.
3. Copy `config.example.env` to `/etc/evastate.env` and set **all** the
   variables (code defaults are neutral: unconfigured, the server answers
   for `state.example.com`).
4. Adapt `caddy/Caddyfile.example` to your domain (apex + wildcard with
   on-demand TLS approved by the server's `/tls-check` endpoint).
5. Register the first participant, and flag your authority (the identity
   allowed to publish on the bulletin board):
   `sudo /opt/evastate/venv/bin/python /opt/evastate/participante.py alta <id> <type> "<Name>" <machine>`
   (types: cowork|agente|servicio|humano; prints the token ONCE), then
   `sudo ... participante.py autoridad <id> on`.
6. Client: `examples/client.py` (configured via environment). Recommended
   first call of every session: `state_overview()`.
7. Optional web console: put `panel.html` next to `server.py` (or set
   `EVASTATE_PANEL`) and open `https://state.<your-domain>/panel`. Put the
   whole host behind a CDN/WAF; for an extra gate, front `/panel` with an
   identity proxy (e.g. Cloudflare Access) — the token check stays either way.

Full detail: `docs/OPERATIONS.md` · design rationale: `docs/ARCHITECTURE.md`
· known limits: `docs/SCOPE-AND-LIMITS.md`.

## Tests

`tests/battery.py` (stdlib only): 7 gates — smoke, protocol/conformance,
identity/security, functional, persistence/backup, regression and light
load. Meant to run against a **sandbox instance** (same server, own
port/database/participants) exposed through the same public route as
production; against production only `--humo` (smoke) is allowed. Configured
via environment: see the file header and `config.example.env`.

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
