# Architecture

## Overview

    client (agent, any machine)
        │  HTTPS  POST /<TOKEN>/mcp          (JSON-RPC, stateless)
        │         PUT  /<TOKEN>/deploy/<n>   (tar.gz static site)
        │         PUT  /<TOKEN>/app/<n>      (tar.gz dynamic app)
        ▼
    Cloudflare (proxy, bot filtering)
        ▼
    Caddy :443  ── on-demand TLS ──►  GET 127.0.0.1:8787/tls-check
        │ reverse_proxy state → 127.0.0.1:8787
        │ file_server  <n>.domain → /var/www/sites/<n>/
        │ import apps.d/*.caddy  → reverse_proxy 127.0.0.1:<app port>
        ▼
    evastate (server.py, unprivileged user, systemd sandbox)
        │ SQLite WAL  /var/lib/evastate/state.db   (single `items` table:
        │   kind + key + JSON data — the "schema" lives in the data)
        │ spool ────► eva-appd.path (root) ────► eva-app-ctl
        ▼                                        (units + Caddy snippets)
    dynamic apps: eva-app-<n>.service, DynamicUser, 512M / 80% CPU

## The five decisions that define the design

**1. Identity is sealed by the server.** The token travels in the path
(`/<TOKEN>/mcp`); an ASGI dispatcher resolves token→identity BEFORE entering
the MCP layer and pins it in a `contextvars.ContextVar`. No tool accepts a
`from` parameter: the signature belongs to the server. Consequence:
impersonation requires stealing a token — lying in an argument is not enough.

**2. Single table, schema in the data.** `items(kind, key, JSON data)` with
upsert by `(kind, key)` and append for sequential kinds. Migrating the
message schema touches no DDL. The cost (no FKs, no per-field indexes) is
acceptable at this scale (tens of participants, thousands of messages).

**3. Privileges through a spool, not sudo.** `evastate` runs under
`ProtectSystem=strict` + `NoNewPrivileges`; inside that sandbox sudo CANNOT
write /etc (it inherits the read-only namespace). Root operations
(installing app units, reloading Caddy) are requested by dropping a JSON
into a spool; a systemd `.path` unit wakes the root helper (`eva-app-ctl`),
which validates EVERYTHING again — the helper is the security boundary and
does not trust the requester.

**4. On-demand TLS with a gate.** Caddy only issues a certificate for a
subdomain if `/tls-check` approves it, and it only approves subdomains
RESERVED in the database. Nobody can trigger issuance by pointing foreign
DNS at the server. Lesson learned in the original deployment: a name listed
in its own block AND covered by a present wildcard ends up with NO
certificate and no log — hence the apex stands alone and everything else is
wildcard + on-demand.

**5. Dynamic apps = hardened systemd unit, not a container.** No Docker:
`DynamicUser=yes`, no privileges, read-only FS except its own folder,
RAM/CPU/task limits, automatic restart. The `eva-app.toml` manifest declares
`cmd` (one line, no single quotes, ≤300 chars — validated by the root
helper) and the process listens on the `$PORT` assigned by the server.

## Life of a message

`msg_send(para='agent-b', tipo='solicitud', ...)` → the token's identity
becomes the sender → ref `SOL-NNN` from a persisted sequence → state
`abierta` → the recipient's `msg_inbox()` lists it → their
`msg_send(tipo='respuesta', responde_a='SOL-NNN')` marks the request
`respondida` → `msg_hilo(ref)` returns the full thread; closing the request
with `sol_cerrar` also marks its answers as handled. `msg_desde(date)`
returns the delta since a date.

## Participants

`/etc/evastate/participants.json` (root:evastate 640) is the source of truth
for tokens; it is mirrored WITHOUT tokens into the database (kind
`participant`) so it can be queried. Add/remove/rotate with
`scripts/participante.py` (admin only; restarts the service). Types: cowork |
agente | servicio | humano — the same namespace is designed to also hold N
ephemeral agents with server-assigned immutable ids.
