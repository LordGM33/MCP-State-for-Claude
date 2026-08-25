# Operations and installation

## Requirements

Ubuntu ≥ 24.04, Caddy ≥ 2.10, Python ≥ 3.12 with venv, systemd. DNS: apex +
wildcard `*.your-domain` pointing at the server (tested behind Cloudflare).

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
    sudo install -m755 scripts/eva-app-ctl scripts/eva-backup.py /usr/local/sbin/
    sudo install -m755 scripts/participante.py /opt/evastate/
    sudo install -m644 systemd/* /etc/systemd/system/
    # environment: copy config.example.env to /etc/evastate.env and set
    # EVERY variable (domain, public host, name, paths). No global token:
    # auth is per participant.
    echo "{}" | sudo tee /etc/evastate/participants.json
    # caddy: adapt caddy/Caddyfile.example to your domain
    sudo systemctl daemon-reload
    sudo systemctl enable --now evastate eva-appd.path eva-backup.timer
    sudo systemctl reload caddy

## Participant registration (admin only)

    sudo /opt/evastate/venv/bin/python /opt/evastate/participante.py alta <id> <type> "<Name>" <machine>
    # prints the token ONCE; hand it over through a safe channel (local file)
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

## Remote registration (invitation flow)

For a new client on another machine or account, no SSH needed:

1. An authority runs `alta_invitar()` over MCP → single-use code (7-day
   expiry). Hand it to the candidate through a private channel.
2. The candidate generates its OWN token (32-128 url-safe chars) and calls
   `POST https://state.<domain>/registro` with JSON
   `{codigo, id, tipo, nombre, maquina, token_propuesto}`. The server
   stores only the SHA-256 of both code and token.
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
