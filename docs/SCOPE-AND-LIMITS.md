# Scope and limits

Written under one doctrine: known limits are declared, not discovered in
production.

## In scope (what it DOES)

- Coordination between N participants with token-verifiable identity:
  messages (5 types), requests with stable ref and state, append-only
  decisions, canonical facts, infrastructure inventory, search.
- Deployment of static sites and dynamic apps to subdomains with automatic
  TLS, without handing out SSH credentials (HTTPS + token, 50 MB limit).
- App management over MCP: status, logs, start/stop/restart.
- Daily consistent SQLite backup with integrity check and 14-day retention,
  plus per-participant download with SHA256 verification.

## Out of scope (deliberate)

- **Inference.** It does not transport prompts to remote models nor
  reasoning results as a service. It is coordination only: the agents'
  brains stay on their own machines (a hard policy of the original project,
  not a preference).
- **Secrets.** It is not a key manager: paths/pointers are stored, never
  values. No biometric data either.
- **Real time.** No websockets, no push: clients poll (`state_overview()`
  when opening a session). At this scale, that is enough.
- **High availability.** One VPS, one SQLite. Downtime is tolerated: a
  dual-write migration protocol (channel + legacy files) and the daily
  backup bound the loss.

## Known limits (honest, with reasons)

1. **The token travels in the URL.** Convenient for simple clients, but URLs
   can end up in proxy logs and histories. Mitigated: end-to-end TLS,
   rotatable tokens (`participante.py rotar`), server-side storage of
   SHA-256 hashes only, and Caddy does not log state paths. If the risk
   grows: move to an `Authorization` header.
2. **Light rate limiting only.** Per-identity sliding window
   (`EVASTATE_RATE_MAX` per `EVASTATE_RATE_WINDOW` seconds, default 240/60s;
   `/registro` capped at 30/60s globally) answered with 429. In-memory, so
   it resets on restart — it protects the channel from a runaway client,
   it does not replace rotating a compromised token.
3. **Dynamic apps have open network egress.** Needed so an app can call out;
   it also means a malicious app could send spam. Compensated: only
   participants registered by the admin can deploy, and each app runs as
   DynamicUser, unprivileged, 512M / 80% CPU.
4. **Add/rotate restarts the service** (~2 s). With stateless clients no
   state is lost; only an in-flight request can fail. Accepted for
   simplicity; if it hurts, move to SIGHUP reload.
5. **Cloudflare filters library User-Agents** (403 for default
   `Python-urllib`). Every client must send its own UA.
6. **The apps' `cmd` is remote code execution BY DESIGN** for authorized
   participants. The defense is not preventing it but containing it
   (sandbox) and gating registration (only the human admin creates
   participants).
7. **System Python for apps** (no builds, no automatic venvs): an app with
   dependencies must vendor them in its tar. A conscious v1 choice.

## Governance defaults

- Participant registration (teams AND ephemeral agents): **authority
  only** — either directly (`participante.py alta`) or via the invitation
  flow (`alta_invitar` → `/registro` → `alta_aprobar`). No self-service
  registration exists.
- When migrating from exchange files: dual-write phase first; the files
  remain authoritative until channel and files show a full week without
  discrepancies.
