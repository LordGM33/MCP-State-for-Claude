#!/usr/bin/env python3
"""Minimal client for the shared-state MCP. Stdlib only.
Env: STATE_URL_BASE, STATE_TOKEN_FILE, STATE_UA (send your own User-Agent:
Cloudflare returns 403 to default library UAs)."""
import json, os, sys, urllib.request

BASE = os.environ.get("STATE_URL_BASE") or sys.exit("set STATE_URL_BASE")
TOKEN_FILE = os.environ.get("STATE_TOKEN_FILE") or sys.exit("set STATE_TOKEN_FILE")
UA = os.environ.get("STATE_UA", "estado-mcp-client/1.0")

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
