#!/usr/bin/env python3
"""Respaldo diario de state: copia consistente del SQLite (API backup), más
participants.json y Caddyfile. Conserva 14 días. Verifica integridad."""
import datetime, glob, gzip, os, shutil, sqlite3, subprocess, sys

DB = "/var/lib/evastate/state.db"; DEST = "/var/backups/evastate"
os.makedirs(DEST, exist_ok=True)
sello = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")

src = sqlite3.connect(DB); dst = sqlite3.connect(f"{DEST}/state-{sello}.db")
src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close(); src.close()
if ok != "ok": sys.exit(f"integridad FALLO: {ok}")
with open(f"{DEST}/state-{sello}.db", "rb") as fi, gzip.open(f"{DEST}/state-{sello}.db.gz", "wb") as fo:
    shutil.copyfileobj(fi, fo)
os.remove(f"{DEST}/state-{sello}.db")
for extra in ("/etc/evastate/participants.json", "/etc/caddy/Caddyfile"):
    if os.path.exists(extra):
        shutil.copy2(extra, f"{DEST}/{os.path.basename(extra)}-{sello}")
limite = (datetime.datetime.now() - datetime.timedelta(days=14)).timestamp()
for f in glob.glob(f"{DEST}/*"):
    if os.path.getmtime(f) < limite: os.remove(f)
print(f"respaldo ok: state-{sello}.db.gz (integridad ok, retencion 14 dias)")
