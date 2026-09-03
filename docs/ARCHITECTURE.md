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

## Making silence legible

Two agents can hold opposite, confident beliefs about whether they have
communicated — and a channel that only stores messages cannot settle it. The
sender infers "they are ignoring me" from an empty thread; the recipient reads,
concludes, and never writes back. Both are certain, one is wrong, and neither
can check. A rule asking people to reply does not fix this, because the failure
is that the data needed to notice was never shown.

The channel closes this structurally, in three places:

- **Read receipts.** Every path that shows a participant a message addressed to
  them (`msg_inbox`, `msg_hilo`, `state_overview`, and the connection greeting)
  stamps `visto` on it, once, the first time. The sender's `state_overview`
  therefore distinguishes *has not opened it* from *opened it and did not
  answer*. A read path that forgets to stamp simply fails to stamp; it can never
  stamp something that was not shown.
- **Non-epistolary activity.** A participant's inbox only shows messages
  addressed to them, so reserving a port, confirming a notice, or committing a
  date all read as silence. `state_overview` and `participantes()` therefore
  carry two separate columns per participant: `ultima_conexion` (stamped at the
  single point every authenticated request passes through) and
  `ultima_escritura` (**derived** from the items table, not from a counter that
  a future record type could forget to increment).
- **Permissions travel with the object.** Each open request in
  `esperando_respuesta` carries `puedes_cerrarla_tu` and the exact call. An
  affordance that is not shown next to the thing it applies to gets guessed at,
  and a guess that hardens into "you are not allowed to" leaves work open
  forever.

Regression cases `D11` in `tests/battery.py` fix all three; they are written to
fail against a build without them.

## Workstation-scoped records

Most records are global to the channel, but ports are not: a port number only
means something on one machine. The `puerto` kind is keyed `<machine>:<port>`
and every read and write is filtered by the caller's own machine, taken from
`participants.json` — never from a parameter. The same number can therefore be
in use on two workstations at once without conflict, and an agent running on a
client's box cannot enumerate anyone else's. Scope by identity, not by
convention: the server enforces it the same way it seals a message's sender.

## Participants

`/etc/evastate/participants.json` (root:evastate 640) is the source of truth
for tokens; it is mirrored WITHOUT tokens into the database (kind
`participant`) so it can be queried. Add/remove/rotate with
`scripts/participante.py` (admin only; restarts the service). Types: cowork |
agente | servicio | humano — the same namespace is designed to also hold N
ephemeral agents with server-assigned immutable ids.
