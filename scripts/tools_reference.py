#!/usr/bin/env python3
"""Generate docs/TOOLS.md from server.py, and fail if any tool is undocumented.

WHY THIS EXISTS. On 2026-09-09 the server exposed 77 tools and 19 of them
appeared in no document at all -- including the whole access-pass mechanism,
which is a security feature. Nothing had gone wrong: the docs were written by
hand, the code kept moving, and hand-written docs drift silently.

A document that quietly stops being true is worse than a missing one. A missing
document is visibly missing; a stale one is confidently wrong.

So the reference is generated, and this script EXITS NON-ZERO when a tool has no
English summary here. Adding a tool without describing it now breaks the check
instead of quietly shipping an undocumented capability.

    python scripts/tools_reference.py            write docs/TOOLS.md
    python scripts/tools_reference.py --check    only verify, change nothing

The summaries live here rather than in the source because the code is commented
in Spanish and this repository is English; keeping the translation beside the
generator means the two cannot disagree about which tools exist.

Standard library only.
"""
import os
import re
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(RAIZ, "server.py")
SALIDA = os.path.join(RAIZ, "docs", "TOOLS.md")

GRUPOS = [
    ("Identity and session", """Who you are and what the channel looks like right
now. Every write is sealed with the identity behind the token in the URL, never
with a parameter the caller supplies.""", [
        "whoami", "state_overview", "participantes", "parametros", "search",
    ]),
    ("Messages", """Messages replace exchange files on disk. `state_overview`
returns headers only; bodies are fetched on demand, because the greeting used to
weigh 193 KB and every participant paid it every session.""", [
        "msg_send", "msg_inbox", "msg_leer", "msg_desde", "msg_hilo", "msg_ack",
        "msg_historial", "sol_cerrar",
    ]),
    ("Noticeboard", """How the authority propagates a rule and sees who has
actually adopted it. A notice can declare which workstation it belongs to, so a
rule about one machine is not put to people who work elsewhere.""", [
        "cartel_publicar", "cartelera", "cartel_confirmar", "cartel_estado",
        "cartel_cerrar",
    ]),
    ("Dates and commitments", """A date somebody committed to, what it depends
on, and every time it moved and why. Moving a date without its history turns a
slipped plan into an invisible one.""", [
        "fecha_comprometer", "fecha_mover", "fecha_estado", "fecha_list",
        "fecha_quien", "fecha_hilo",
    ]),
    ("Ports", """Ports are registered per workstation. Ask before killing a
process: the register exists so that "something is on 8080" has an owner.""", [
        "puerto_reservar", "puerto_list", "puerto_quien", "puerto_liberar",
    ]),
    ("Shared resources", """A resource is something finite on one machine -- GPU
memory, typically. Reporting a real measurement is separate from claiming a
share, because a claim is an intention and a measurement is a fact.""", [
        "recurso_declarar", "recurso_tomar", "recurso_soltar", "recurso_estado",
        "recurso_medir",
    ]),
    ("Shared long-lived tools", """A process several agents use at once, such as
a local model server. Anyone may start one; stopping it needs a human to confirm
that nobody else is mid-turn, because the cost of a wrong stop falls on someone
who is not in the room.""", [
        "herramienta_declarar", "herramienta_estado", "herramienta_parada_pedir",
        "herramienta_parada_autorizar", "herramienta_parada_cerrar",
    ]),
    ("Facts, decisions and infrastructure", """Canonical values everyone should
quote identically, decisions that outlive the conversation, and pointers to
machines. Never secrets: this store is shared and readable by every
participant.""", [
        "fact_set", "fact_get", "fact_list", "decision_log", "decision_list",
        "infra_put", "infra_list",
    ]),
    ("Subdomains and access passes", """Each subdomain declares what it is
(public, temporary with an expiry date, or restricted) instead of everything
being loosely called a demo. A restricted one sits behind a door; passes are
revocable and scoped to a single host.""", [
        "subdomain_claim", "subdomain_tipo", "subdomain_list", "subdomain_release",
        "subdomain_pendientes", "subdomain_aprobar", "subdomain_rechazar",
        "pase_crear", "pase_anular", "pase_list",
    ]),
    ("Apps and deployment", """Static sites and dynamic apps are deployed over
HTTPS with your own token. Dynamic apps sleep when idle and wake on the first
visitor.""", [
        "deploy_info", "app_list", "app_status", "app_logs", "app_restart",
        "app_stop", "app_start", "app_dormir", "app_eliminar",
    ]),
    ("Membership and tokens (authority)", """Joining, leaving and key rotation.
The ones marked with a passphrase need a second factor the authority holds and
that is never stored on disk, because they move what somebody else can see or
do.""", [
        "alta_invitar", "altas_pendientes", "alta_aprobar", "alta_rechazar",
        "participante_baja", "participante_estacion", "participante_cartelera",
        "rotacion_invitar", "rotacion_estado", "rotacion_anular", "rotacion_cerrar",
        "token_confirmar", "intentos_frase",
    ]),
]

RESUMEN = {
    "whoami": "The identity this client writes as, sealed by the server.",
    "state_overview": "Opening snapshot for your identity: pending message headers, "
                      "noticeboard items awaiting you, your dates, your ports, who is "
                      "active. `completo=True` returns full message bodies as before.",
    "participantes": "Everyone registered, with last connection and last write, so "
                     "nobody has to infer from their own inbox whether another agent "
                     "is still alive.",
    "parametros": "What each tool accepts, read from the running server rather than "
                  "from documentation that may have drifted.",
    "search": "Free-text search across the whole shared state.",
    "msg_send": "Send a message. `para` takes a participant id or 'todos'. A "
                "`solicitud` gets a stable SOL-N reference; a `respuesta` must name "
                "what it answers. A `ref` on any other type is refused rather than "
                "quietly dropped.",
    "msg_inbox": "What is open and addressed to you (or to everyone).",
    "msg_leer": "Read one or several whole messages by number. Applies the same "
                "recipient filter as the inbox: message numbers are consecutive, so "
                "without it anyone could reach another's mail by guessing.",
    "msg_desde": "Everything written since a date. An unparseable date is rejected "
                 "rather than answered with an empty list, because empty reads as "
                 "'nothing happened'.",
    "msg_hilo": "The whole thread behind a reference: the request and every reply.",
    "msg_ack": "Mark a message addressed to you as handled.",
    "msg_historial": "Full direct history between you and one other participant.",
    "sol_cerrar": "Close a request as answered or discarded.",
    "cartel_publicar": "Publish a rule, condition, request or notice (authority only). "
                       "`estacion` limits it to one workstation.",
    "cartelera": "The noticeboard as your identity sees it, each item carrying whether "
                 "you still owe a confirmation.",
    "cartel_confirmar": "Confirm you have integrated a notice into your local practice.",
    "cartel_estado": "Who confirmed, who is missing, who it was addressed to, and which "
                     "workstation it applies to (authority only).",
    "cartel_cerrar": "Stop a notice from demanding action; it stays in the history.",
    "fecha_comprometer": "Commit to a date, optionally against a resource or another "
                         "date. Returns FECHA-N.",
    "fecha_mover": "Move a date keeping the history of every move and its reason.",
    "fecha_estado": "Progress on one of your dates: pending, in progress, blocked, done.",
    "fecha_list": "Dates across the project, filterable by owner and horizon.",
    "fecha_quien": "Who has a resource committed, or what is committed for a date.",
    "fecha_hilo": "Everything that happened to a date: every move, with its reason.",
    "puerto_reservar": "Register a port or range you occupy on your workstation.",
    "puerto_list": "Ports registered on your workstation.",
    "puerto_quien": "Who owns a port here. Ask before killing anything.",
    "puerto_liberar": "Release a port you hold.",
    "recurso_declarar": "Declare that a finite shared resource exists on your "
                        "workstation (authority only).",
    "recurso_tomar": "Record that you are using part of a shared resource.",
    "recurso_soltar": "Release what you were holding.",
    "recurso_estado": "Who holds what, how much is left, and since when.",
    "recurso_medir": "Report a real measurement of current usage, distinct from a "
                     "claim, which is only an intention.",
    "herramienta_declarar": "Declare a long-lived process several agents share "
                            "(authority only).",
    "herramienta_estado": "Shared tools on your workstation and whether any may be "
                          "stopped right now.",
    "herramienta_parada_pedir": "Ask permission to stop a shared tool. Does not stop it.",
    "herramienta_parada_autorizar": "A human confirms nobody else is mid-turn. Only "
                                    "humans, and the authorisation expires.",
    "herramienta_parada_cerrar": "Close a stop request, done or withdrawn. Anyone may: "
                                 "withdrawing leaves things as they are.",
    "fact_set": "Set a canonical value everyone should quote identically. No secrets.",
    "fact_get": "Read one canonical value.",
    "fact_list": "List canonical values, optionally by prefix.",
    "decision_log": "Record a lasting decision. Append-only: to change one, supersede it.",
    "decision_list": "Decisions, newest first.",
    "infra_put": "Register a machine or service. Pointers only, never secrets.",
    "infra_list": "Registered servers and services.",
    "subdomain_claim": "Reserve a subdomain, declaring what it is and, if temporary, "
                       "when it expires. A temporary one with no expiry is refused: "
                       "that is permanence nobody decided on.",
    "subdomain_tipo": "Declare what an existing subdomain is, and until when.",
    "subdomain_list": "Registered subdomains, with type, owner and whether any have "
                      "expired. Nothing shuts itself off: expiry becomes visible, "
                      "not automatic.",
    "subdomain_release": "Release one of yours. Does not delete files or apps.",
    "subdomain_pendientes": "Subdomains awaiting approval (authority only).",
    "subdomain_aprobar": "Approve a requested subdomain: enables deployment and TLS.",
    "subdomain_rechazar": "Reject a requested subdomain.",
    "pase_crear": "Issue a pass so somebody outside can enter one restricted "
                  "subdomain. Look-only unless explicitly marked otherwise.",
    "pase_anular": "Revoke a pass. The next request no longer gets through.",
    "pase_list": "Passes issued, with their real state. Expired ones are marked: an "
                 "expired pass and a forged one are different problems for the person "
                 "holding them.",
    "deploy_info": "How to deploy a static site or a dynamic app.",
    "app_list": "Registered dynamic apps and their state.",
    "app_status": "systemd state of an app you own.",
    "app_logs": "Last log lines of an app you own.",
    "app_restart": "Restart an app you own.",
    "app_stop": "Stop an app you own; the subdomain falls back to static content.",
    "app_start": "Start a stopped app you own.",
    "app_dormir": "Put an app to sleep without uninstalling it. It wakes on the first "
                  "visitor.",
    "app_eliminar": "Remove a dynamic app: stop it, delete its unit and its proxy "
                    "snippet.",
    "alta_invitar": "Issue a single-use invitation, valid seven days (authority only).",
    "altas_pendientes": "Join requests awaiting approval (authority only).",
    "alta_aprobar": "Approve a join request and activate the identity (passphrase).",
    "alta_rechazar": "Reject a pending join request.",
    "participante_baja": "Deactivate a participant: their token stops working "
                         "(passphrase).",
    "participante_estacion": "Correct which machine a participant is on (passphrase). "
                             "It is not a label: the workstation decides which ports, "
                             "resources and notices that identity sees.",
    "participante_cartelera": "Set whether a participant confirms notices. Turn it off "
                              "for services, and for the authority the rules come from.",
    "rotacion_invitar": "Issue a single-use code so a participant can set a new token.",
    "rotacion_estado": "Who has already confirmed their new token and who has not.",
    "rotacion_anular": "Cancel a live rotation code so another can be issued. Touches "
                       "no token: it only invalidates an unused permission.",
    "rotacion_cerrar": "Retire the old tokens once everyone has moved (passphrase).",
    "token_confirmar": "Confirm you are now using your new token.",
    "intentos_frase": "Failed passphrase attempts (authority only). A failure nobody "
                      "can see is a failure nobody investigates.",
}


def herramientas():
    src = open(SERVER, encoding="utf-8").read()
    pat = re.compile(r"@mcp\.tool\(\)\s*\ndef (\w+)\(([^)]*)\)\s*->\s*str:")
    out = {}
    for m in pat.finditer(src):
        firma = re.sub(r"\s*:\s*[\w\[\], ]+?(?=\s*=|\s*,|$)", "",
                       " ".join(m.group(2).split()))
        out[m.group(1)] = firma
    return out


def main(argv):
    solo_comprobar = "--check" in argv
    tools = herramientas()

    agrupadas = {n for _, _, ns in GRUPOS for n in ns}
    sin_resumen = sorted(t for t in tools if t not in RESUMEN)
    sin_grupo = sorted(t for t in tools if t not in agrupadas)
    fantasmas = sorted((set(RESUMEN) | agrupadas) - set(tools))

    problemas = []
    if sin_resumen:
        problemas.append("tools with no English summary: " + ", ".join(sin_resumen))
    if sin_grupo:
        problemas.append("tools in no section: " + ", ".join(sin_grupo))
    if fantasmas:
        problemas.append("described but no longer in server.py: " + ", ".join(fantasmas))

    if problemas:
        print("docs/TOOLS.md is out of date:")
        for p in problemas:
            print("  -", p)
        print("\nAdd them to scripts/tools_reference.py. This check exists because on "
              "2026-09-09\n19 of 77 tools were documented nowhere, including the whole "
              "access-pass\nmechanism. Nothing had gone wrong: hand-written docs simply "
              "drift, and a\nstale document is confidently wrong where a missing one is "
              "visibly missing.")
        return 1

    if solo_comprobar:
        print("docs/TOOLS.md is up to date: %d tools, all described." % len(tools))
        return 0

    lineas = [
        "# Tool reference",
        "",
        "**Generated by `scripts/tools_reference.py` - do not edit by hand.**",
        "Run `python scripts/tools_reference.py --check` in CI: it fails when a tool",
        "has no description, so a new capability cannot ship undocumented.",
        "",
        "Every tool returns JSON as text. Identity is never a parameter: the server",
        "seals each call with the identity behind the token in the URL.",
        "",
        "%d tools." % len(tools),
        "",
    ]
    for titulo, intro, nombres in GRUPOS:
        lineas += ["## " + titulo, "", " ".join(intro.split()), ""]
        for n in nombres:
            lineas += ["### `%s(%s)`" % (n, tools[n]), "",
                       " ".join(RESUMEN[n].split()), ""]

    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    with open(SALIDA, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lineas))
    print("written: docs/TOOLS.md (%d tools in %d sections)" % (len(tools), len(GRUPOS)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
