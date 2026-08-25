#!/usr/bin/env python3
"""Cliente mínimo del MCP de estado compartido. Solo stdlib.

Config por entorno:
    STATE_URL_BASE    p. ej. https://state.example.com   (obligatoria)
    STATE_TOKEN_FILE  ruta al archivo con TU token       (obligatoria)
    STATE_UA          User-Agent propio (recomendado: nombre-de-tu-agente/1.0)

Nota: si el servidor está tras Cloudflare, el User-Agent por defecto de
urllib/requests devuelve 403 — manda siempre uno propio.
"""
import json, os, sys, urllib.request

BASE = os.environ.get("STATE_URL_BASE") or sys.exit("define STATE_URL_BASE")
TOKEN_FILE = os.environ.get("STATE_TOKEN_FILE") or sys.exit("define STATE_TOKEN_FILE")
UA = os.environ.get("STATE_UA", "estado-mcp-cliente/1.0")

def state(tool, args=None):
    tok = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(
        f"{BASE.rstrip('/')}/{tok}/mcp",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": tool, "arguments": args or {}}}).encode(),
        {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "User-Agent": UA})
    r = json.load(urllib.request.urlopen(req, timeout=15))
    return r["result"]["content"][0]["text"]

if __name__ == "__main__":
    print(state(sys.argv[1] if len(sys.argv) > 1 else "whoami",
                json.loads(sys.argv[2]) if len(sys.argv) > 2 else None))
