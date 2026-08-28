#!/usr/bin/env python3
"""Genera el documento del scrum desde la base del canal. Sin argumentos escribe
en stdout; con --drive lo sube al documento configurado."""
import datetime, json, os, sqlite3, sys

DB = os.environ.get("EVASTATE_DB", "/var/lib/evastate/state.db")
FINALES = ("hecha", "cancelada")


def filas(kind):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        out = []
        for r in con.execute("SELECT key, data, updated FROM items WHERE kind=?", (kind,)):
            d = json.loads(r["data"])
            d["_key"] = r["key"]
            out.append(d)
        return out
    finally:
        con.close()


def dias(a, b):
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


def texto():
    hoy = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    fechas = filas("fecha")
    abiertas = [f for f in fechas if f.get("estado") not in FINALES]
    cerradas = [f for f in fechas if f.get("estado") in FINALES]
    for f in abiertas:
        f["_en"] = dias(hoy, f["cuando"])
    abiertas.sort(key=lambda f: f["cuando"])

    L = []
    w = L.append
    w("SCRUM DEL PROYECTO")
    w(f"Foto generada el {hoy} desde el canal state. Es la copia consultable;")
    w("el registro vivo esta en el canal y se actualiza en cuanto un cowork cambia algo.")
    w("")
    w("COMO LEER ESTO: cada fecha lleva el nombre del cowork que la sostiene. Una fecha")
    w("'comprometida' es firme; una 'aproximada' es una intencion. El historial de")
    w("movimientos dice cuantas veces se ha corrido una fecha y por que.")
    w("")

    venc = [f for f in abiertas if f["_en"] < 0]
    if venc:
        w("== FECHAS VENCIDAS (pasaron y nadie las cerro) ==")
        for f in venc:
            w(f"- {f['_key']} · {f['que']} · responsable: {f.get('dueno')} · "
              f"era el {f['cuando']} (hace {-f['_en']} dias) · estado: {f.get('estado')}")
        w("")

    blq = [f for f in abiertas if f.get("estado") == "bloqueada"]
    if blq:
        w("== BLOQUEADAS (no avanzan, y aqui esta por que) ==")
        for f in blq:
            ult = (f.get("avance") or [{}])[-1].get("nota", "")
            w(f"- {f['_key']} · {f['que']} · responsable: {f.get('dueno')} · "
              f"para el {f['cuando']} · bloqueo: {ult or 'sin detallar'}")
        w("")

    prox = [f for f in abiertas if 0 <= f["_en"] <= 14]
    w("== PROXIMOS 14 DIAS ==")
    if prox:
        for f in prox:
            extra = []
            if f.get("recurso"):
                extra.append(f"usa {f['recurso']}")
            if f.get("depende_de"):
                extra.append(f"depende de {f['depende_de']}")
            if f.get("historial"):
                extra.append(f"movida {len(f['historial'])} veces")
            w(f"- {f['_key']} · {f['que']} · responsable: {f.get('dueno')} · "
              f"{f['cuando']} (en {f['_en']} dias) · {f.get('tipo')} · estado: {f.get('estado')}"
              + (f" · {'; '.join(extra)}" if extra else ""))
    else:
        w("- nada comprometido en dos semanas")
    w("")

    resto = [f for f in abiertas if f["_en"] > 14]
    if resto:
        w("== MAS ADELANTE ==")
        for f in resto:
            w(f"- {f['_key']} · {f['que']} · responsable: {f.get('dueno')} · "
              f"{f['cuando']} (en {f['_en']} dias) · {f.get('tipo')} · estado: {f.get('estado')}")
        w("")

    # Un choque no se detecta leyendo la lista: hay que cruzarla.
    choques = []
    conrec = [f for f in abiertas if f.get("recurso") and f.get("tipo") == "comprometida"]
    for i, a in enumerate(conrec):
        for b in conrec[i + 1:]:
            if a["recurso"] != b["recurso"]:
                continue
            a1, a2 = a.get("desde") or a["cuando"], a["cuando"]
            b1, b2 = b.get("desde") or b["cuando"], b["cuando"]
            if a1 <= b2 and b1 <= a2:
                choques.append((a, b))
    if choques:
        w("== CHOQUES DE RECURSO (dos compromisos firmes sobre lo mismo a la vez) ==")
        for a, b in choques:
            w(f"- {a['recurso']}: {a['_key']} ({a.get('dueno')}, {a['cuando']}) y "
              f"{b['_key']} ({b.get('dueno')}, {b['cuando']})")
        w("")

    movidas = [f for f in abiertas if len(f.get("historial") or []) >= 2]
    if movidas:
        w("== FECHAS QUE SE HAN MOVIDO VARIAS VECES ==")
        w("Si una fecha se corre tres veces, el problema rara vez es la fecha.")
        for f in movidas:
            w(f"- {f['_key']} · {f['que']} · responsable: {f.get('dueno')} · "
              f"ahora {f['cuando']}, movida {len(f['historial'])} veces:")
            for m in f["historial"]:
                w(f"    {m['de']} -> {m['a']} el {m.get('cuando')}: {m.get('motivo')}")
        w("")

    porduenno = {}
    for f in abiertas:
        porduenno.setdefault(f.get("dueno") or "?", []).append(f)
    w("== POR RESPONSABLE ==")
    for quien in sorted(porduenno):
        fs = porduenno[quien]
        w(f"{quien}: {len(fs)} fechas abiertas")
        for f in sorted(fs, key=lambda x: x["cuando"]):
            w(f"  - {f['_key']} {f['que']} ({f['cuando']}, {f.get('estado')})")
    w("")

    if cerradas:
        rec = sorted(cerradas, key=lambda f: f.get("cerrada_el") or "", reverse=True)[:15]
        w("== CERRADAS RECIENTEMENTE ==")
        for f in rec:
            w(f"- {f['_key']} · {f['que']} · {f.get('dueno')} · {f.get('estado')}"
              f" el {f.get('cerrada_el', '?')}")
        w("")

    puertos = [p for p in filas("puerto") if p.get("estado") == "ocupado"]
    if puertos:
        w("== PUERTOS OCUPADOS POR ESTACION ==")
        pormaq = {}
        for p in puertos:
            pormaq.setdefault(p.get("maquina", "?"), []).append(p)
        for maq in sorted(pormaq):
            w(f"{maq}:")
            for p in sorted(pormaq[maq], key=lambda x: int(x["puerto"])):
                w(f"  - {p['puerto']} {p.get('servicio')} ({p.get('dueno')})")
        w("")

    w(f"Fin de la foto. {len(abiertas)} fechas abiertas, {len(cerradas)} cerradas.")
    return "\n".join(L)


def subir(contenido):
    """Reemplaza el contenido del documento de Drive configurado. El documento lo
    crea una persona y comparte con esta cuenta de servicio: asi vive en el Drive
    de quien lo consulta y no en la cuota de la cuenta de servicio."""
    cred = os.environ.get("EVA_GOOGLE_CRED", "/etc/evastate/google-scrum.json")
    doc = os.environ.get("EVA_SCRUM_DOC_ID", "")
    if not doc:
        sys.exit("define EVA_SCRUM_DOC_ID con el id del documento de Drive")
    if not os.path.exists(cred):
        sys.exit(f"no encuentro las credenciales en {cred}")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload
    creds = service_account.Credentials.from_service_account_file(
        cred, scopes=["https://www.googleapis.com/auth/drive.file"])
    api = build("drive", "v3", credentials=creds, cache_discovery=False)
    api.files().update(
        fileId=doc,
        media_body=MediaInMemoryUpload(contenido.encode("utf-8"), mimetype="text/plain"),
        supportsAllDrives=True,
    ).execute()
    print(f"documento {doc} actualizado ({len(contenido)} caracteres)")


if __name__ == "__main__":
    c = texto()
    if "--drive" in sys.argv:
        subir(c)
    else:
        print(c)
