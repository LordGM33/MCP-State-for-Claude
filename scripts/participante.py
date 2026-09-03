#!/usr/bin/env python3
"""Altas y bajas de participantes de state. SOLO admin (decisión de Ricardo,
23-ago-2026: las altas de agentes pasan por él). Imprime el token UNA vez."""
import json, os, secrets, subprocess, sys, datetime

P = os.environ.get("EVASTATE_PARTICIPANTS", "/etc/evastate/participants.json")
SERVICIO = os.environ.get("EVASTATE_SERVICE", "evastate")
GRUPO = os.environ.get("EVASTATE_GROUP", "evastate")

USO = ("uso: participante.py alta <id> <tipo> [nombre] [maquina] | baja <id> | lista\n"
       "                     rotar <id> [dias]      -- token nuevo; el viejo SIGUE valiendo\n"
       "                     cerrar-rotacion [<id>] -- retira el viejo (comprueba antes)\n"
       "                     cartelera <id> si|no   -- si este participante confirma carteles")

def cargar():
    return json.load(open(P, encoding="utf-8")) if os.path.exists(P) else {}

def guardar(d):
    # El chgrp NO puede fallar en silencio. El fichero es 640: si el grupo queda
    # mal, el servicio no puede leerlo y el canal entero se cae al reiniciar —
    # justo dentro de una rotación, que es cuando menos margen hay. Antes iba con
    # check=False y sólo se veía un aviso suelto por pantalla.
    tmp = P + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o640)
    r = subprocess.run(["chgrp", GRUPO, tmp], capture_output=True, text=True)
    if r.returncode != 0:
        os.unlink(tmp)
        sys.exit(f"ABORTO sin tocar nada: no pude poner el grupo '{GRUPO}' al fichero de\n"
                 f"participantes ({r.stderr.strip()}).\n"
                 f"Con el grupo mal, el servicio no puede leerlo y el canal no arranca.\n"
                 f"Comprueba EVASTATE_GROUP (aquí vale '{GRUPO}').")
    os.replace(tmp, P)

def _sha(t):
    import hashlib
    return hashlib.sha256(t.encode()).hexdigest()

def _hash_actual(p):
    """El token vigente, venga guardado en claro o ya en hash."""
    h = p.get("token_sha256")
    if not h and p.get("token") and len(p["token"]) >= 24:
        h = _sha(p["token"])
    return h

def _confirmados():
    """Quién ha confirmado ya su token nuevo, leído de la base del canal.
    Se consulta en SOLO LECTURA: este guion no escribe jamás en la base."""
    import sqlite3
    db = os.environ.get("EVASTATE_DB", "/var/lib/evastate/state.db")
    out = set()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        for r in con.execute("SELECT key FROM items WHERE kind='rotacion'"):
            out.add(r[0])
        con.close()
    except Exception as e:
        print("  (no pude leer la base para comprobar confirmaciones: %s)" % e)
    return out

def main():
    if os.geteuid() != 0: sys.exit("correr con sudo")
    if len(sys.argv) < 2: sys.exit(USO)
    op = sys.argv[1]; d = cargar()
    if op == "lista":
        conf = _confirmados()
        for k, v in d.items():
            rot = ""
            if v.get("token_anterior_sha256"):
                rot = "  ROTANDO(%s)" % ("confirmado" if k in conf else "SIN CONFIRMAR")
            print(f"{k:14} tipo={v.get('tipo'):8} activo={v.get('activo')} "
                  f"maquina={v.get('maquina','?')}{rot}")
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
        # DOS FASES A PROPÓSITO. Sustituir el token de golpe deja al participante
        # incomunicado en el instante exacto en que más falta le hace el canal:
        # para avisar de que no puede entrar. Aquí el viejo sigue abriendo la
        # puerta hasta que él mismo confirme, con su propia mano, que el nuevo
        # funciona. Cerrar la rotación es un acto humano aparte.
        pid = sys.argv[2].strip().lower()
        if pid not in d: sys.exit(f"'{pid}' no existe")
        dias = int(sys.argv[3]) if len(sys.argv) > 3 else 7
        viejo = _hash_actual(d[pid])
        if not viejo:
            sys.exit(f"'{pid}' no tiene token vigente que conservar; usa alta")
        if d[pid].get("token_anterior_sha256"):
            sys.exit(f"'{pid}' ya está rotando. Cierra esa rotación antes de abrir otra "
                     f"(cerrar-rotacion {pid}) o quedarían tres tokens vivos.")
        tok = secrets.token_urlsafe(36)
        d[pid]["token_anterior_sha256"] = viejo
        d[pid]["rota_hasta"] = (datetime.date.today() + datetime.timedelta(days=dias)).isoformat()
        d[pid].pop("token", None)
        d[pid]["token_sha256"] = _sha(tok)
        guardar(d)
        print(f"token nuevo de '{pid}' (no se vuelve a mostrar):\n{tok}\n")
        print(f"El token ANTERIOR sigue valiendo. Se retira cuando '{pid}' llame a")
        print(f"token_confirmar() con el nuevo y tú ejecutes: cerrar-rotacion {pid}")

    elif op == "cerrar-rotacion":
        pid = sys.argv[2].strip().lower() if len(sys.argv) > 2 else None
        conf = _confirmados()
        objetivo = [pid] if pid else [k for k, v in d.items() if v.get("token_anterior_sha256")]
        if not objetivo: sys.exit("no hay ninguna rotación abierta")
        sin = [k for k in objetivo if k not in conf]
        if sin:
            print("NO cierro nada. Estos no han confirmado su token nuevo:")
            for k in sin: print("  - " + k)
            print("\nCerrar ahora los deja fuera del canal, y sin canal no pueden avisar")
            print("de que están fuera. Comprueba antes con rotacion_estado() si llevan")
            print("días sin conectarse: puede que no te ignoren, puede que no hayan vuelto.")
            print("\nSi de verdad hay que forzarlo: --forzar")
            if "--forzar" not in sys.argv: sys.exit(1)
            print("\nFORZANDO por petición explícita.")
        for k in objetivo:
            d[k].pop("token_anterior_sha256", None); d[k].pop("rota_hasta", None)
        guardar(d)
        print("rotación cerrada para: " + ", ".join(objetivo))

    elif op == "cartelera":
        pid = sys.argv[2].strip().lower()
        if pid not in d: sys.exit(f"'{pid}' no existe")
        val = sys.argv[3].lower() if len(sys.argv) > 3 else "si"
        d[pid]["confirma_cartelera"] = (val == "si")
        guardar(d)
        print(f"'{pid}' {'CONFIRMA' if val=='si' else 'NO confirma'} carteles a partir de ahora")

    elif op == "baja":
        pid = sys.argv[2].strip().lower()
        if pid not in d: sys.exit(f"'{pid}' no existe")
        d[pid]["activo"] = False; guardar(d)
        print(f"baja lógica de '{pid}' (el historial se conserva)")
    else:
        sys.exit(USO)
    subprocess.run(["systemctl", "restart", SERVICIO])
    print("evastate reiniciado para aplicar el cambio")

main()
