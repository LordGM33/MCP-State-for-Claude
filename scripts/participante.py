#!/usr/bin/env python3
"""Participant add/remove/rotate (admin only). Prints the token ONCE."""
import json, os, secrets, subprocess, sys, datetime

P = os.environ.get("EVASTATE_PARTICIPANTS", "/etc/evastate/participants.json")
SERVICIO = os.environ.get("EVASTATE_SERVICE", "evastate")
GRUPO = os.environ.get("EVASTATE_GROUP", "evastate")

def cargar():
    return json.load(open(P, encoding="utf-8")) if os.path.exists(P) else {}

def guardar(d):
    tmp = P + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o640)
    subprocess.run(["chgrp", GRUPO, tmp], check=False)
    os.replace(tmp, P)

def main():
    if os.geteuid() != 0: sys.exit("correr con sudo")
    if len(sys.argv) < 2: sys.exit("uso: participante.py alta <id> <tipo> [nombre] [maquina] | baja <id> | lista | rotar <id>")
    op = sys.argv[1]; d = cargar()
    if op == "lista":
        for k, v in d.items():
            print(f"{k:14} tipo={v.get('tipo'):8} activo={v.get('activo')} maquina={v.get('maquina','?')}")
        return
    if op == "alta":
        pid = sys.argv[2].strip().lower()
        tipo = sys.argv[3] if len(sys.argv) > 3 else "cowork"
        if tipo not in ("cowork", "agente", "servicio", "humano"): sys.exit("tipo: cowork|agente|servicio|humano")
        if pid in d and d[pid].get("activo"): sys.exit(f"'{pid}' ya existe y está activo (usa rotar)")
        tok = secrets.token_urlsafe(36)
        d[pid] = {"token": tok, "tipo": tipo,
                  "nombre": sys.argv[4] if len(sys.argv) > 4 else pid,
                  "maquina": sys.argv[5] if len(sys.argv) > 5 else "",
                  "desde": datetime.date.today().isoformat(), "activo": True}
        guardar(d)
        print(f"alta de '{pid}'. TOKEN (guardalo, no se vuelve a mostrar):\n{tok}")
    elif op == "rotar":
        pid = sys.argv[2].strip().lower()
        if pid not in d: sys.exit(f"'{pid}' no existe")
        tok = secrets.token_urlsafe(36)
        d[pid]["token"] = tok; guardar(d)
        print(f"token nuevo de '{pid}':\n{tok}")
    elif op == "baja":
        pid = sys.argv[2].strip().lower()
        if pid not in d: sys.exit(f"'{pid}' no existe")
        d[pid]["activo"] = False; guardar(d)
        print(f"baja lógica de '{pid}' (el historial se conserva)")
    else:
        sys.exit("operacion desconocida")
    subprocess.run(["systemctl", "restart", SERVICIO])
    print("evastate reiniciado para aplicar el cambio")

main()
