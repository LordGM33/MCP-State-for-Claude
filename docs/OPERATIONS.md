# Operations and installation

## What you can install, and where

| Platform | Channel, console, backups | Static sites | Own HTTPS | Subdomains | Dynamic apps |
|---|---|---|---|---|---|
| Linux + systemd | yes | yes | yes | yes (needs a proxy) | yes |
| macOS | yes | yes | yes | no | no |
| Windows | yes | yes | yes | no | no |

Dynamic apps and scale-to-zero need systemd and the root helper. Everything
else is plain Python and SQLite. Architecture is irrelevant to the channel
itself: it runs wherever CPython 3.12+ runs, Raspberry Pi included.

## Two installation modes

`scripts/instalar.sh` (Linux + systemd) asks which one you want:

**Exposed to the internet.** What a public deployment needs: a reverse proxy
in front, public certificates, real subdomains. The installer pulls Caddy from
its official repository, so it keeps receiving security updates with the rest
of the system.

**Private LAN.** No proxy and no DNS. The server serves its own HTTPS with a
local certificate authority created at install time, and sites are published
under `/s/<name>/`. The certificate carries the server's IP address, so
`https://192.168.x.x:8787` validates without any DNS at all.

The installer states plainly that the LAN mode has no CDN in front: the scan
filter and the edge rate limit do not exist there, the only brake is the
server's own per-identity limit, and the whole thing rests on the local
network being trusted.

## Requirements

Python ≥ 3.12 with venv. On Linux, systemd for the service and the root helper.
Caddy ≥ 2.10 **only for the internet mode**: apex + wildcard `*.your-domain`
pointing at the server (tested behind Cloudflare). The LAN mode needs neither
a proxy nor DNS.

## Installation (mirrors the real deployment)

    # service user and directory trees
    sudo useradd -r -s /usr/sbin/nologin evastate
    sudo mkdir -p /opt/evastate /var/lib/evastate/{ctl-spool,ctl-out} \
                  /var/www/sites /srv/apps /var/backups/evastate /etc/caddy/apps.d
    sudo chown evastate:evastate /var/www/sites /srv/apps \
                  /var/lib/evastate/ctl-spool /var/lib/evastate/ctl-out
    # venv + SDK
    sudo python3 -m venv /opt/evastate/venv
    sudo /opt/evastate/venv/bin/pip install "mcp>=2" starlette uvicorn
    # code
    sudo install -m644 server.py /opt/evastate/
    sudo install -m755 scripts/eva-app-ctl scripts/eva-backup.py scripts/eva-app-idle /usr/local/sbin/
    sudo install -m755 scripts/participante.py /opt/evastate/
    sudo install -m644 systemd/* /etc/systemd/system/
    # environment: copy config.example.env to /etc/evastate.env and set
    # EVERY variable (domain, public host, name, paths). No global token:
    # auth is per participant.
    echo "{}" | sudo tee /etc/evastate/participants.json
    # caddy: adapt caddy/Caddyfile.example to your domain
    sudo systemctl daemon-reload
    sudo systemctl enable --now evastate eva-appd.path eva-backup.timer eva-app-idle.timer
    sudo systemctl reload caddy

## Participant registration (admin only)

    sudo /opt/evastate/statectl alta <id> <type> "<Name>" <machine>
    # prints the token ONCE; hand it over through a safe channel (local file)
    # Use statectl, not participante.py directly: without the installation's
    # environment the script falls back to the DEFAULT paths, which on a host
    # with two installations means writing into the wrong one. It now prints
    # which participants file and service it is about to touch.
    # also: baja <id> · rotar <id> · lista · autoridad <id> on|off
    # "autoridad" marks who may publish rules/requests on the bulletin board

## Web console (/panel)

The console is one self-contained HTML file — no build step, no CDN, no
extra service. To enable it on a fresh install:

1. Put `panel.html` next to `server.py`:
   `sudo install -m 644 panel.html /opt/evastate/`
   (or point `EVASTATE_PANEL=/path/to/panel.html` in `/etc/evastate.env`).
2. Restart: `sudo systemctl restart evastate`.
3. Open `https://state.<your-domain>/panel`. No extra Caddy rule is needed —
   the route is served by the same host that already proxies the MCP.
4. Sign in with any participant token. There is no separate account: the
   server seals the identity exactly as it does over MCP, so the console can
   never do more than that token is allowed to do. Views reserved to an
   authority (approve/veto registrations, publish on the bulletin board)
   only appear for identities flagged with `participante.py autoridad`.

### Remembering the token on a device (PIN)

Pasting a full token on every visit pushes people towards worse habits, so the
login offers "remember on this device": the token is encrypted in that browser
with AES-GCM under a key derived from a PIN (PBKDF2, 600k iterations, random
salt and IV per device). Later visits ask only for the PIN. Five wrong PINs
wipe the stored blob. Only salt, IV, ciphertext and a failure counter reach
`localStorage` — the plaintext token never does; it lives in `sessionStorage`
for the life of the tab, exactly as before. The checkbox is opt-in, and a
"use another token / forget this device" button clears it.

This protects a token at rest on the device. It is not a second factor: anyone
who knows the PIN on that machine gets in, so it is unsuitable for shared
computers, and the page says so.

Access hardening that ships by default: strict CSP (`default-src 'none'`,
no external resources, `frame-ancestors 'none'`), `Cache-Control: no-store`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, the token
kept in `sessionStorage` (never in the page URL), a 15-minute idle logout,
and a global throttle that answers 429 to repeated invalid tokens.

Recommended on top: keep the host behind a CDN/WAF, and for defense in depth
front `/panel` with an identity proxy (Cloudflare Access, oauth2-proxy, or
Caddy `basic_auth`) so the page is only served to a pre-authenticated
browser. Example Caddy snippet inside the state block:

    @panel path /panel
    handle @panel {
        basic_auth { admin <bcrypt-hash-from-caddy-hash-password> }
        reverse_proxy 127.0.0.1:8787
    }

The token check still applies underneath — the proxy is an extra gate, not
a replacement.

To verify a fresh install end to end, run `tests/install_check.sh` (see the
Tests section of the README): it installs an isolated instance from this
repo, registers an authority, and checks health, the panel, its security
headers and an authenticated call, then removes everything.

## Running without a reverse proxy

Set `EVASTATE_SERVE_SITES=1` and the server publishes `SITES_DIR` under
`/s/<name>/` itself, resolving on each request so a freshly deployed site
appears without a restart. Point `EVASTATE_TLS_CERT` and `EVASTATE_TLS_KEY` at
a certificate and uvicorn terminates TLS directly; `EVASTATE_BIND=0.0.0.0`
makes it reachable from the network. All four default to off, so a proxied
deployment behaves exactly as before.

Two things to know:

- **Put the port in `EVASTATE_PUBLIC_HOST`.** The SDK rejects any Host header
  it does not recognise with `421 Invalid Host header`, and the list is built
  from that variable plus `:443`. On any other port the channel answers 421 and
  nothing explains why. `EVASTATE_EXTRA_HOSTS` accepts a comma-separated list
  for additional names.
- **Sites served this way share an origin** with the console, whereas
  subdomains do not. For internal demos that is usually fine; if you need
  browser-level isolation between deployments, use subdomains and a proxy.

If you generate a certificate authority by hand, give it
`keyUsage=critical,keyCertSign,cRLSign`. OpenSSL 3 rejects a CA without it, so
Python clients fail to verify while curl still accepts the chain — the failure
shows up only in the clients that matter.

## Subdomains and deployments (authority-gated)

1. A participant calls `subdomain_claim("name")` → state `solicitado`.
   Uploads to it return 403 and `/tls-check` refuses the certificate.
2. An authority reviews `subdomain_pendientes()` (also surfaced in
   `state_overview` under `por_aprobar`) and calls `subdomain_aprobar(name)`
   or `subdomain_rechazar(name, motivo)`.
3. Once approved, the owner uploads a tar.gz to `/<TOKEN>/deploy/<name>`
   (static) or `/<TOKEN>/app/<name>` (dynamic app) — see `deploy_info()`.
4. Teardown: `subdomain_release(name)` (owner or authority) and
   `app_eliminar(name)`, which stops the service and removes its unit and
   Caddy snippet. Deployed files are left on disk for the admin to remove.

An authority's own `subdomain_claim` is approved on the spot.

### Scale-to-zero (idle demos cost nothing)

Dynamic apps should not burn CPU or RAM while nobody is using them:

- `eva-app-idle.timer` runs every 5 minutes and stops any app whose subdomain
  has had no request in `EVA_IDLE_MIN` minutes (default 20). It reads Caddy's
  access log from journald, so enable access logging on the wildcard block
  (`log { output stdout · format json }`) — the example Caddyfile does.
- A visitor to a sleeping app hits a 502, which the wildcard's `handle_errors`
  rewrites to the state server's `/wake/<app>`. That page explains the demo is
  at rest and offers a button; **only the button's POST starts the app**.
  A bare GET never starts anything: internet scanners sweep subdomains all day
  and would keep every demo alive. Known scanner user-agents are answered 403
  at the edge, before reaching any backend.
- Owners can sleep an app on demand with `app_dormir(name)`, or from the
  console. Static sites need none of this: they run no process.

## Hardening the origin (no extra cost)

If the host sits behind a CDN like Cloudflare, the origin IP is still reachable
directly unless you close it. Two settings pay for themselves:

- **Only accept 80/443 from the CDN.** Fetch the provider's published ranges
  and allow just those; drop the rest. Measured on the reference deployment:
  inbound packets fell 49% and failed connection attempts went from 1218/min
  to 0 — that traffic was port scanning against the bare IP. Add the rules
  *before* removing the open ones so there is never a gap, and schedule an
  automatic rollback (`systemd-run --on-active=12min`) that restores the saved
  ruleset, cancelling it only after you have verified access still works.
  Note the ranges file may lack a trailing newline: normalise it or two ranges
  will concatenate into one invalid entry.
- **Tell the reverse proxy to trust the CDN**, otherwise every log line shows
  the CDN's address and any IP-based defence would ban the CDN itself:

      servers {
          trusted_proxies static <cdn ranges>
      }

Neither costs anything, and both are prerequisites for meaningful rate
limiting or IP bans later.

### What is worth turning on at the CDN (and what is not)

On Cloudflare's free plan, measured on the reference deployment:

- **A custom rule blocking scans for paths that do not exist here** — URI
  containing `.php`, `/wp-`, `/.env`, `/.git`, `/.aws`, `/.ssh`, `phpmyadmin`,
  `/adminer`. That matched 12% of all requests over 24 hours.
- **One rate limiting rule** (the free plan allows exactly one, counting by IP,
  in a fixed 10-second window) on the sensitive routes: the console and the
  registration endpoint. 20 requests per 10 seconds is generous for a human
  and still cuts scripted attempts.
- **Do NOT enable Bot Fight Mode** if any legitimate client speaks HTTP without
  a browser. It challenges non-JavaScript clients, the free plan has no skip
  rule to exempt them, and it will silently break your own agents.
- Managed WAF rulesets require a paid plan; do not count on them for free.

## Remote registration (invitation flow)

For a new client on another machine or account, no SSH needed:

1. An authority runs `alta_invitar(nota, id_sugerido)` over MCP — or uses
   the console's *Altas* view. It returns a single-use code (7-day expiry)
   **and `texto_para_el_cowork`**: a complete briefing to hand over as-is,
   with the code already embedded. Deliver it through a private channel.
2. The newcomer runs the script inside that briefing. It picks the highest
   writable directory that is not inside a git repository, creates `state/`
   there with a `.gitignore`, generates its OWN token (the plaintext never
   leaves that machine), and posts to `/registro`. The server stores only
   the SHA-256 of both code and token.
3. The authority reviews `altas_pendientes()` and calls `alta_aprobar(id)`
   (or `alta_rechazar`). On approval the identity is live immediately — the
   candidate's token works, and no plaintext ever touched the server disk.

Requires `ReadWritePaths=` including the participants directory in
`evastate.service`, and the directory group-writable by the service user
(safe now: the file holds hashes, not secrets).

## Minimum verification after installing

1. `curl https://state.<domain>/health` → `ok · N records`
2. `initialize` + `tools/call whoami` with two tokens → distinct identities
3. invalid token → 404
4. request→inbox→answer→thread (`SOL-001`)
5. claim + static PUT → subdomain serves with on-demand TLS
6. dynamic app PUT → `active` and proxied
7. `systemctl start eva-backup` → gz with `integrity_check` ok
8. `GET /panel` → console page with CSP and `no-store` headers
9. `GET /<TOKEN>/backup` → latest gz with `X-Backup-Sha256` header
   (each participant downloads its copy when opening a session)
9. `msg_send(tipo=respuesta, responde_a='archivo:<date>')` → links threads
   migrated from files without closing any channel SOL

For the full battery, see `tests/battery.py` (7 gates; run it against a
sandbox instance deployed through the same public route as production).

## Known failure modes

- MCP answers 421: the public host is missing from `allowed_hosts`
  (`EVASTATE_PUBLIC_HOST`) — the SDK's anti DNS-rebinding protection.
- 403 only from Python: User-Agent (Cloudflare). Send your own UA.
- Subdomain without cert and without logs: an individual name listed next to
  a wildcard block — do not do that; keep the apex alone.
- App answering 502: almost always the app itself (check `app_logs`);
  systemd keeps retrying it.
