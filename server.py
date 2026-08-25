#!/usr/bin/env python3
"""Shared-state MCP server: identity-sealed messaging, facts, decisions,
subdomain/app deployment. All config via EVASTATE_* env vars."""
import json, os, re, sqlite3, sys, datetime, contextlib, contextvars, io, tarfile, subprocess, hashlib, secrets

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.routing import Route

DB_PATH = os.environ.get("EVASTATE_DB", "/var/lib/evastate/state.db")
PARTICIPANTS_PATH = os.environ.get("EVASTATE_PARTICIPANTS", "/etc/evastate/participants.json")
DOMAIN = os.environ.get("EVASTATE_DOMAIN", "example.com")
PUBLIC_HOST = os.environ.get("EVASTATE_PUBLIC_HOST", f"state.{DOMAIN}")
SERVER_NAME = os.environ.get("EVASTATE_NAME", "estado-mcp")
SITES_DIR = os.environ.get("EVASTATE_SITES", "/var/www/sites")
APPS_DIR = os.environ.get("EVASTATE_APPS", "/srv/apps")
CTL = "/usr/local/sbin/eva-app-ctl"
MAX_UPLOAD = 50 * 1024 * 1024  # 50 MB
NOMBRE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
RESERVADOS = {"www", "state", "mail", "smtp", "autodiscover", "_dmarc"}

CURRENT = contextvars.ContextVar("participante", default=None)

def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()

def cargar_participantes():
    with open(PARTICIPANTS_PATH) as f:
        data = json.load(f)
    idx = {}
    for pid, p in data.items():
        if not p.get("activo", True): continue
        h = p.get("token_sha256")
        if not h and p.get("token") and len(p["token"]) >= 24:
            h = _sha(p["token"])
        if h: idx[h] = pid
    return data, idx

PARTICIPANTES, TOKEN_INDEX = cargar_participantes()

def _recargar_participantes():
    global PARTICIPANTES, TOKEN_INDEX
    PARTICIPANTES, TOKEN_INDEX = cargar_participantes()
    sembrar_participantes()

def _guardar_participantes():
    tmp = PARTICIPANTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(PARTICIPANTES, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o640)
    os.replace(tmp, PARTICIPANTS_PATH)

def ident():
    v = CURRENT.get()
    if not v:
        raise RuntimeError("sin identidad: la peticion no vino por una URL con token")
    return v

def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=8000")
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT,
            data TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)""")
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_kind_key
                       ON items(kind, key) WHERE key IS NOT NULL""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_kind ON items(kind)")
    sembrar_participantes()

def sembrar_participantes():
    """Espeja participants.json (SIN tokens) en la base, para consultas."""
    for pid, p in PARTICIPANTES.items():
        pub = {k: v for k, v in p.items() if k not in ("token", "token_sha256")}
        pub["id"] = pid
        _put("participant", pid, pub)

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def _put(kind, key, data):
    t = now()
    with db() as con:
        row = con.execute("SELECT id FROM items WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row:
            con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), t, row["id"]))
            return {"accion": "actualizado", "id": row["id"], "kind": kind, "key": key}
        cur = con.execute("INSERT INTO items(kind,key,data,created,updated) VALUES(?,?,?,?,?)",
                          (kind, key, json.dumps(data, ensure_ascii=False), t, t))
        return {"accion": "creado", "id": cur.lastrowid, "kind": kind, "key": key}

def _append(kind, data):
    t = now()
    with db() as con:
        cur = con.execute("INSERT INTO items(kind,key,data,created,updated) VALUES(?,NULL,?,?,?)",
                          (kind, json.dumps(data, ensure_ascii=False), t, t))
        return {"accion": "creado", "id": cur.lastrowid, "kind": kind}

def _rows(kind, limit=300, order="ASC"):
    with db() as con:
        cur = con.execute(
            f"SELECT id,key,data,created,updated FROM items WHERE kind=? ORDER BY id {order} LIMIT ?",
            (kind, limit))
        out = []
        for r in cur.fetchall():
            d = json.loads(r["data"]); d["_id"] = r["id"]
            if r["key"]: d["_key"] = r["key"]
            d["_creado"] = r["created"]
            if r["updated"] != r["created"]: d["_actualizado"] = r["updated"]
            out.append(d)
        return out

def _get(kind, key):
    with db() as con:
        r = con.execute("SELECT id,data FROM items WHERE kind=? AND key=?", (kind, key)).fetchone()
    if not r: return None, None
    return r["id"], json.loads(r["data"])

def _next_ref(prefijo="SOL"):
    with db() as con:
        _, d = _get("seq", prefijo)
        n = (d or {}).get("n", 0) + 1
    _put("seq", prefijo, {"n": n})
    return f"{prefijo}-{n:03d}"

_REF_RE = re.compile(r"(?i)^(SOL|TEST|CART)-(\d+)$")
def _norm_ref(s):
    """Forma canonica de una ref del canal (SOL-007 == SOL-7). None si no es ref."""
    m = _REF_RE.match((s or "").strip())
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}" if m else None

def _jd(x): return json.dumps(x, ensure_ascii=False, indent=2)

def es_autoridad(pid):
    return bool(PARTICIPANTES.get(pid, {}).get("autoridad"))

init_db()

mcp = MCPServer(SERVER_NAME)

# ───────────── IDENTIDAD ─────────────
@mcp.tool()
def whoami() -> str:
    """Devuelve la identidad con la que este cliente escribe (la sella el servidor)."""
    pid = ident()
    p = {k: v for k, v in PARTICIPANTES[pid].items() if k not in ("token", "token_sha256")}
    return _jd({"id": pid, **p})

@mcp.tool()
def participantes() -> str:
    """Lista de participantes registrados (coworks, agentes, servicios, humanos)."""
    return _jd(_rows("participant"))

# ───────────── ARRANQUE ─────────────
@mcp.tool()
def state_overview() -> str:
    """Foto del estado compartido para MI identidad. Llamar al iniciar sesión."""
    me = ident()
    pend = [m for m in _rows("msg", 500)
            if m.get("para") in (me, "todos") and m.get("de") != me
            and m.get("estado") not in ("atendido", "respondida", "descartada")]
    mias = [m for m in _rows("msg", 500) if m.get("de") == me
            and m.get("tipo") == "solicitud" and m.get("estado") == "abierta"]
    cart_pend = []
    for c in _rows("cartel", 300, order="DESC"):
        if c.get("estado") != "activo": continue
        if me in c.get("dirigido_a", []) and me not in c.get("confirmaciones", {}) \
                and c.get("requiere") in ("confirmacion", "respuesta"):
            cart_pend.append({"ref": c["ref"], "tipo": c["tipo"], "de": c["de"],
                              "asunto": c["asunto"], "requiere": c["requiere"]})
    return _jd({
        "yo": me,
        "cartelera_pendiente": cart_pend,
        "esperando_respuesta": mias,
        "mensajes_pendientes": pend,
        "decisiones_recientes": _rows("decision", 8, order="DESC"),
        "hechos": _rows("fact", 100),
        "infraestructura": _rows("infra", 100),
        "subdominios": _rows("subdomain", 200),
        "apps": _rows("app", 100),
        "nota": "Sin secretos ni datos personales. Coordinación, nunca inferencia (R-007).",
    })

# ───────────── MENSAJES (sustituyen a los archivos INTERCAMBIO) ─────────────
@mcp.tool()
def msg_send(para: str, asunto: str, cuerpo: str, tipo: str = "aviso", responde_a: str = "", ref: str = "") -> str:
    """Envía un mensaje. `para`: id de participante o 'todos' (por defecto úsalo:
    R-023 prohíbe filtrar por adelantado). `tipo`: aviso|solicitud|respuesta|bitacora|alerta.
    Si tipo=solicitud se asigna ref estable (SOL-NNN) y estado=abierta; se puede
    pasar `ref` explicita (SOL-N) para migrar solicitudes numeradas en los
    archivos puente — debe ser unica y el contador avanza para no chocar.
    Si tipo=respuesta, indicar `responde_a` con la ref de la solicitud."""
    me = ident()
    tipo = tipo.strip().lower()
    if tipo not in ("aviso", "solicitud", "respuesta", "bitacora", "alerta"):
        return "ERROR: tipo debe ser aviso|solicitud|respuesta|bitacora|alerta"
    para = para.strip().lower()
    if para != "todos" and para not in PARTICIPANTES:
        return f"ERROR: destinatario '{para}' no existe. Ver participantes()."
    d = {"de": me, "para": para, "asunto": asunto, "cuerpo": cuerpo, "tipo": tipo}
    if tipo == "solicitud":
        if ref:
            nref = _norm_ref(ref)
            if not nref:
                return "ERROR: la ref explicita debe tener forma SOL-N o TEST-N (ej. SOL-010). TEST-N es la serie de pruebas: no toca el contador SOL."
            ref = nref
            with db() as con:
                for r_ in con.execute("SELECT data FROM items WHERE kind='msg'").fetchall():
                    if _norm_ref(json.loads(r_["data"]).get("ref")) == ref:
                        return f"ERROR: la ref {ref} ya existe en el canal."
            if ref.startswith("SOL-"):
                n = int(ref.split("-")[1])
                _, sq = _get("seq", "SOL")
                if n > (sq or {}).get("n", 0):
                    _put("seq", "SOL", {"n": n})
            d["ref"] = ref
        else:
            d["ref"] = _next_ref("SOL")
        d["estado"] = "abierta"
    elif tipo == "respuesta":
        if not responde_a: return "ERROR: una respuesta necesita `responde_a` (ref SOL-N o texto libre, p.ej. 'archivo:2026-08-13')."
        ra = responde_a.strip()
        # solo se normaliza a mayusculas si es una ref del canal; el texto libre se respeta
        d["responde_a"] = _norm_ref(ra) or ra
        if str(d["responde_a"]).startswith("CART-"):
            cart = next((c for c in _rows("cartel", 300) if _norm_ref(c.get("ref")) == d["responde_a"]), None)
            if not cart: return f"ERROR: no existe el cartel {d['responde_a']}."
            if para != cart.get("de"):
                return (f"ERROR: las respuestas a la cartelera van EN PRIVADO a la autoridad emisora "
                        f"(para='{cart.get('de')}'), nunca a 'todos' ni a terceros.")
        d["estado"] = "pendiente"
    else:
        d["estado"] = "pendiente"
    res = _append("msg", d)
    if tipo == "respuesta" and str(d.get("responde_a", "")).startswith("CART-"):
        with db() as con:
            for r in con.execute("SELECT id,data FROM items WHERE kind='cartel'").fetchall():
                dc = json.loads(r["data"])
                if _norm_ref(dc.get("ref")) == d["responde_a"]:
                    dc.setdefault("confirmaciones", {})[me] = {"tipo": "respuesta", "msg_id": res["id"], "fecha": now()}
                    con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                (json.dumps(dc, ensure_ascii=False), now(), r["id"]))
    if tipo == "respuesta" and _REF_RE.match(d["responde_a"]):
        # la solicitud del canal pasa a respondida (el hilo conserva todo);
        # las refs externas (archivo:...) no cierran nada: son enlace, no estado
        with db() as con:
            for r in con.execute("SELECT id,data FROM items WHERE kind='msg'").fetchall():
                dd = json.loads(r["data"])
                if _norm_ref(dd.get("ref")) == d["responde_a"] and dd.get("tipo") == "solicitud":
                    dd["estado"] = "respondida"
                    con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                (json.dumps(dd, ensure_ascii=False), now(), r["id"]))
    return _jd({**res, **({"ref": d.get("ref")} if d.get("ref") else {})})

@mcp.tool()
def msg_inbox(incluir_atendidos: bool = False) -> str:
    """Qué hay abierto dirigido a MÍ (o a 'todos')."""
    me = ident()
    rows = _rows("msg", 500, order="DESC")
    out = [m for m in rows if m.get("para") in (me, "todos") and m.get("de") != me
           and (incluir_atendidos or m.get("estado") not in ("atendido", "respondida", "descartada"))]
    # los envios propios no son "bandeja": se consultan con search o msg_hilo (D1, 25-ago)
    return _jd(out)

@mcp.tool()
def msg_desde(fecha_iso: str) -> str:
    """Todo lo escrito desde una fecha (ISO: 2026-08-23 o 2026-08-23T15:00:00+00:00)."""
    rows = _rows("msg", 1000, order="ASC")
    return _jd([m for m in rows if m["_creado"] >= fecha_iso])

@mcp.tool()
def msg_hilo(ref: str) -> str:
    """El hilo completo de una ref (SOL-007): la solicitud y todas sus respuestas."""
    nref = _norm_ref(ref) or ref.strip()
    rows = _rows("msg", 1000, order="ASC")
    def _eq(v): return bool(v) and (_norm_ref(v) or v.strip()) == nref
    return _jd([m for m in rows if _eq(m.get("ref")) or _eq(m.get("responde_a"))])

@mcp.tool()
def msg_ack(id: int, nota: str = "") -> str:
    """Marca un mensaje dirigido a mí como atendido."""
    with db() as con:
        r = con.execute("SELECT data FROM items WHERE id=? AND kind='msg'", (id,)).fetchone()
        if not r: return f"ERROR: no existe el mensaje {id}."
        d = json.loads(r["data"])
        d["estado"] = "atendido"; d["atendido_por"] = ident()
        if nota: d["nota_cierre"] = nota
        con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                    (json.dumps(d, ensure_ascii=False), now(), id))
    return _jd({"accion": "atendido", "id": id})

@mcp.tool()
def sol_cerrar(ref: str, estado: str = "respondida", nota: str = "") -> str:
    """Cierra una solicitud: estado respondida|descartada."""
    if estado not in ("respondida", "descartada"):
        return "ERROR: estado debe ser respondida|descartada"
    ref = _norm_ref(ref) or ref.strip().upper()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='msg'").fetchall():
            d = json.loads(r["data"])
            if _norm_ref(d.get("ref")) == ref and d.get("tipo") == "solicitud":
                me = ident()
                if me not in (d.get("de"), d.get("para")) and d.get("para") != "todos":
                    return f"ERROR: {ref} es de {d.get('de')} para {d.get('para')}; solo los involucrados pueden cerrarla."
                d["estado"] = estado; d["cerrada_por"] = me
                if nota: d["nota_cierre"] = nota
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                # D2 (25-ago): al cerrar la solicitud, sus respuestas quedan atendidas
                atendidas = 0
                for r2 in con.execute("SELECT id,data FROM items WHERE kind='msg'").fetchall():
                    d2 = json.loads(r2["data"])
                    if d2.get("tipo") == "respuesta" and _norm_ref(d2.get("responde_a")) == ref \
                            and d2.get("estado") == "pendiente":
                        d2["estado"] = "atendido"; d2["atendido_por"] = f"cierre:{ref}"
                        con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                    (json.dumps(d2, ensure_ascii=False), now(), r2["id"]))
                        atendidas += 1
                return _jd({"accion": estado, "ref": ref, "respuestas_atendidas": atendidas})
    return f"ERROR: no existe la solicitud {ref}."

# ───────────── CARTELERA (divulgación de la autoridad) e HISTORIAL ─────────────
@mcp.tool()
def cartel_publicar(tipo: str, asunto: str, cuerpo: str, requiere: str = "", formato_respuesta: str = "") -> str:
    """Publica en la cartelera (SOLO autoridad). tipo: regla|condicion|peticion|aviso.
    `requiere` (default por tipo): confirmacion — cada participante confirma que la
    integró a su regencia local; respuesta — respuesta PRIVADA a la autoridad en
    `formato_respuesta`; nada. Recibe ref estable CART-N."""
    me = ident()
    if not es_autoridad(me):
        return "ERROR: solo la autoridad publica en la cartelera; lo tuyo va por msg_send."
    tipo = tipo.strip().lower()
    if tipo not in ("regla", "condicion", "peticion", "aviso"):
        return "ERROR: tipo debe ser regla|condicion|peticion|aviso"
    req = (requiere or {"regla": "confirmacion", "condicion": "confirmacion",
                        "peticion": "respuesta", "aviso": "nada"}[tipo]).strip().lower()
    if req not in ("confirmacion", "respuesta", "nada"):
        return "ERROR: requiere debe ser confirmacion|respuesta|nada"
    if tipo == "peticion" and req == "respuesta" and not formato_respuesta:
        return "ERROR: una peticion de informacion declara `formato_respuesta` (el formato acordado)."
    ref = _next_ref("CART")
    dirigidos = [p for p, v in PARTICIPANTES.items() if v.get("activo", True) and p != me]
    d = {"de": me, "tipo": tipo, "asunto": asunto, "cuerpo": cuerpo, "ref": ref,
         "requiere": req, "formato_respuesta": formato_respuesta,
         "dirigido_a": dirigidos, "confirmaciones": {}, "estado": "activo"}
    res = _append("cartel", d)
    return _jd({**res, "ref": ref, "dirigido_a": dirigidos})

@mcp.tool()
def cartelera(incluir_cerrados: bool = False) -> str:
    """La cartelera de la autoridad vista por MI identidad: cada item trae mi_estado
    (confirmar, responder en privado, o al día). Prioridad máxima del canal."""
    me = ident()
    out = []
    for c in _rows("cartel", 300, order="DESC"):
        if c.get("estado") != "activo" and not incluir_cerrados: continue
        conf = c.get("confirmaciones", {})
        if me == c.get("de"): mi = "emisor"
        elif me not in c.get("dirigido_a", []): mi = "no dirigido"
        elif me in conf: mi = "al dia"
        elif c.get("requiere") == "confirmacion":
            mi = "PENDIENTE: confirmar integracion con cartel_confirmar"
        elif c.get("requiere") == "respuesta":
            mi = f"PENDIENTE: responder EN PRIVADO a {c.get('de')} (msg_send responde_a={c.get('ref')})"
        else: mi = "al dia"
        c.pop("confirmaciones", None)
        c["mi_estado"] = mi
        out.append(c)
    return _jd(out)

@mcp.tool()
def cartel_confirmar(ref: str, nota: str = "integrada en la regencia local") -> str:
    """Confirma que integraste a tu regencia local la regla/condición del cartel."""
    me = ident()
    ref = _norm_ref(ref) or ref.strip().upper()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='cartel'").fetchall():
            d = json.loads(r["data"])
            if _norm_ref(d.get("ref")) == ref:
                if me not in d.get("dirigido_a", []):
                    return f"ERROR: {ref} no esta dirigido a {me}."
                if d.get("requiere") == "respuesta":
                    return f"ERROR: {ref} pide respuesta privada a {d.get('de')} (msg_send responde_a={ref}), no confirmacion."
                d.setdefault("confirmaciones", {})[me] = {"tipo": "integrada", "fecha": now(), "nota": nota}
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "confirmado", "ref": ref, "por": me})
    return f"ERROR: no existe el cartel {ref}."

@mcp.tool()
def cartel_estado(ref: str) -> str:
    """(SOLO autoridad) Matriz de un cartel: quién confirmó/respondió y quién falta."""
    me = ident()
    if not es_autoridad(me): return "ERROR: cartel_estado es de la autoridad."
    ref = _norm_ref(ref) or ref.strip().upper()
    for c in _rows("cartel", 300):
        if _norm_ref(c.get("ref")) == ref:
            conf = c.get("confirmaciones", {})
            pend = [p for p in c.get("dirigido_a", []) if p not in conf]
            return _jd({"ref": ref, "tipo": c.get("tipo"), "requiere": c.get("requiere"),
                        "estado": c.get("estado"), "confirmados": conf, "pendientes": pend})
    return f"ERROR: no existe el cartel {ref}."

@mcp.tool()
def cartel_cerrar(ref: str, nota: str = "") -> str:
    """(SOLO autoridad) Cierra un cartel: deja de exigir acción; queda en historial."""
    me = ident()
    if not es_autoridad(me): return "ERROR: cartel_cerrar es de la autoridad."
    ref = _norm_ref(ref) or ref.strip().upper()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='cartel'").fetchall():
            d = json.loads(r["data"])
            if _norm_ref(d.get("ref")) == ref:
                d["estado"] = "cerrado"; d["cerrado_por"] = me
                if nota: d["nota_cierre"] = nota
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "cerrado", "ref": ref})
    return f"ERROR: no existe el cartel {ref}."

@mcp.tool()
def msg_historial(con_quien: str, limite: int = 200) -> str:
    """Historial completo de mensajes directos entre MI identidad y otro participante,
    cronológico. La MISMA vista para ambas partes: transparente y referenciable
    por _id y fecha, aunque pertenezcan a personas o cuentas distintas."""
    me = ident(); otro = con_quien.strip().lower()
    if otro not in PARTICIPANTES: return f"ERROR: '{otro}' no existe. Ver participantes()."
    rows = _rows("msg", 2000, order="ASC")
    par = [m for m in rows if {m.get("de"), m.get("para")} == {me, otro}
           or (me == otro and m.get("de") == me and m.get("para") == me)]
    return _jd({"entre": sorted([me, otro]), "total": len(par), "mensajes": par[-limite:]})

# ───────────── ALTAS REMOTAS (invitación + aprobación de la autoridad) ─────────────
@mcp.tool()
def alta_invitar(nota: str = "") -> str:
    """(SOLO autoridad) Emite una invitación de UN SOLO USO (caduca en 7 días) para
    que un cliente nuevo solicite su alta vía POST /registro. El código se muestra
    UNA vez: entrégalo al candidato por un canal privado."""
    me = ident()
    if not es_autoridad(me): return "ERROR: alta_invitar es de la autoridad."
    codigo = secrets.token_urlsafe(18)
    _append("invitacion", {"codigo_sha256": _sha(codigo), "emitida_por": me,
                           "estado": "emitida", "nota": nota, "emitida": now()})
    return _jd({"codigo": codigo,
                "instrucciones": f"El candidato hace POST https://{PUBLIC_HOST}/registro con JSON "
                                 "{codigo, id, tipo, nombre, maquina, token_propuesto} — el token lo "
                                 "genera EL CANDIDATO (32-128 chars url-safe); el servidor solo guarda "
                                 "su hash. Queda pendiente hasta alta_aprobar de la autoridad."})

@mcp.tool()
def altas_pendientes() -> str:
    """(SOLO autoridad) Solicitudes de alta esperando aprobación."""
    me = ident()
    if not es_autoridad(me): return "ERROR: altas_pendientes es de la autoridad."
    out = []
    for i in _rows("invitacion", 200):
        if i.get("estado") == "solicitada":
            out.append({k: v for k, v in i.items() if k not in ("codigo_sha256", "token_sha256")})
    return _jd(out)

@mcp.tool()
def alta_aprobar(id: str, nota: str = "") -> str:
    """(SOLO autoridad) Aprueba una solicitud de alta: activa la identidad con el
    token que el candidato propuso (aquí solo vive su hash)."""
    me = ident()
    if not es_autoridad(me): return "ERROR: alta_aprobar es de la autoridad."
    pid = id.strip().lower()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("estado") == "solicitada" and d.get("id") == pid:
                if pid in PARTICIPANTES and PARTICIPANTES[pid].get("activo", True):
                    return f"ERROR: '{pid}' ya existe y esta activo."
                PARTICIPANTES[pid] = {"token_sha256": d["token_sha256"], "tipo": d.get("tipo", "cowork"),
                                      "nombre": d.get("nombre", pid), "maquina": d.get("maquina", ""),
                                      "desde": datetime.date.today().isoformat(), "activo": True,
                                      "alta_via": "registro", "aprobada_por": me}
                _guardar_participantes()
                _recargar_participantes()
                d["estado"] = "aprobada"; d["aprobada_por"] = me; d["aprobada_fecha"] = now()
                if nota: d["nota_aprobacion"] = nota
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "aprobada", "id": pid,
                            "aviso": "el candidato ya puede usar el token que propuso"})
    return f"ERROR: no hay solicitud pendiente para '{pid}'."

@mcp.tool()
def alta_rechazar(id: str, motivo: str = "") -> str:
    """(SOLO autoridad) Rechaza una solicitud de alta pendiente."""
    me = ident()
    if not es_autoridad(me): return "ERROR: alta_rechazar es de la autoridad."
    pid = id.strip().lower()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("estado") == "solicitada" and d.get("id") == pid:
                d["estado"] = "rechazada"; d["rechazada_por"] = me
                if motivo: d["motivo_rechazo"] = motivo
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "rechazada", "id": pid})
    return f"ERROR: no hay solicitud pendiente para '{pid}'."

async def registro_post(request):
    """Alta remota sin token: requiere codigo de invitacion vigente de la autoridad."""
    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse({"error": "JSON invalido"}, status_code=400)
    codigo = str(body.get("codigo", "")); pid = str(body.get("id", "")).strip().lower()
    tok = str(body.get("token_propuesto", "")); tipo = str(body.get("tipo", "cowork")).strip().lower()
    if tipo not in ("cowork", "agente", "servicio", "humano"):
        return JSONResponse({"error": "tipo: cowork|agente|servicio|humano"}, status_code=400)
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,19}", pid):
        return JSONResponse({"error": "id invalido: minusculas, [a-z][a-z0-9-]{1,19}"}, status_code=400)
    if pid in PARTICIPANTES:
        return JSONResponse({"error": "id no disponible"}, status_code=409)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", tok):
        return JSONResponse({"error": "token_propuesto: 32-128 caracteres url-safe, generado por ti"}, status_code=400)
    h = _sha(codigo)
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("estado") == "solicitada" and d.get("id") == pid:
                return JSONResponse({"error": "id no disponible"}, status_code=409)
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("codigo_sha256") == h and d.get("estado") == "emitida":
                try:
                    ed = datetime.datetime.fromisoformat(d.get("emitida"))
                    caducada = (datetime.datetime.now(datetime.timezone.utc) - ed).days >= 7
                except Exception:
                    caducada = False
                if caducada:
                    d["estado"] = "caducada"
                    con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                    return JSONResponse({"error": "invitacion caducada"}, status_code=410)
                d.update({"estado": "solicitada", "id": pid, "tipo": tipo,
                          "nombre": str(body.get("nombre", pid))[:80],
                          "maquina": str(body.get("maquina", ""))[:40],
                          "token_sha256": _sha(tok), "solicitada": now()})
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return JSONResponse({"ok": True, "id": pid,
                                     "estado": "pendiente de aprobacion por la autoridad"})
    return JSONResponse({"error": "invitacion no valida"}, status_code=404)

# ───────────── DECISIONES / HECHOS / INFRA (sellados con identidad) ─────────────
@mcp.tool()
def decision_log(titulo: str, decision: str, motivo: str, proyecto: str = "", supersede: int = 0) -> str:
    """Registra una decisión duradera (append-only; para cambiarla, nueva con `supersede`)."""
    d = {"de": ident(), "titulo": titulo, "decision": decision, "motivo": motivo, "proyecto": proyecto}
    if supersede: d["supersede"] = supersede
    return _jd(_append("decision", d))

@mcp.tool()
def decision_list(proyecto: str = "", limite: int = 25) -> str:
    """Decisiones, de la más reciente a la más antigua."""
    rows = _rows("decision", 300, order="DESC")
    if proyecto: rows = [r for r in rows if r.get("proyecto", "").lower() == proyecto.lower()]
    superseded = {r["supersede"] for r in rows if r.get("supersede")}
    for r in rows:
        if r["_id"] in superseded: r["_estado"] = "superada"
    return _jd(rows[:limite])

@mcp.tool()
def fact_set(clave: str, valor: str, fuente: str = "") -> str:
    """Fija un hecho canónico (dato que todos deben citar igual). Sin secretos."""
    return _jd(_put("fact", clave.strip().lower(),
                    {"clave": clave, "valor": valor, "fuente": fuente, "de": ident()}))

@mcp.tool()
def fact_get(clave: str) -> str:
    """Consulta un hecho canónico."""
    _, d = _get("fact", clave.strip().lower())
    return _jd(d) if d else f"No existe el hecho '{clave}'. Usa fact_list()."

@mcp.tool()
def fact_list(prefijo: str = "") -> str:
    """Lista los hechos canónicos."""
    rows = _rows("fact")
    if prefijo: rows = [r for r in rows if r.get("clave", "").lower().startswith(prefijo.lower())]
    return _jd(rows)

@mcp.tool()
def infra_put(id: str, tipo: str, descripcion: str, detalles: str = "{}") -> str:
    """Registra/actualiza un recurso de infraestructura. `detalles` JSON. Solo punteros, no secretos."""
    try:
        extra = json.loads(detalles) if detalles else {}
    except json.JSONDecodeError as e:
        return f"ERROR: detalles no es JSON: {e}"
    return _jd(_put("infra", id, {"id": id, "tipo": tipo, "descripcion": descripcion,
                                  "registrado_por": ident(), **extra}))

@mcp.tool()
def infra_list() -> str:
    """Servidores y servicios registrados."""
    return _jd(_rows("infra"))

@mcp.tool()
def search(texto: str, limite: int = 30) -> str:
    """Busca texto libre en todo el estado compartido."""
    q = f"%{texto.lower()}%"
    with db() as con:
        cur = con.execute("SELECT id,kind,key,data,updated FROM items WHERE lower(data) LIKE ? "
                          "ORDER BY id DESC LIMIT ?", (q, limite))
        out = [{"_id": r["id"], "_tipo": r["kind"], "_clave": r["key"],
                "_actualizado": r["updated"], **json.loads(r["data"])} for r in cur.fetchall()]
    return _jd(out)

# ───────────── SUBDOMINIOS Y DESPLIEGUE ─────────────
@mcp.tool()
def subdomain_claim(nombre: str, notas: str = "") -> str:
    """Reserva un subdominio para MI identidad. El despliegue es por HTTPS con tu
    token (ver deploy_info()), no hace falta SSH."""
    me = ident()
    nombre = nombre.strip().lower()
    if not NOMBRE_RE.match(nombre): return "ERROR: solo minúsculas, números y guiones (max 31)."
    if nombre in RESERVADOS: return f"ERROR: '{nombre}' está reservado."
    _, d = _get("subdomain", nombre)
    if d and d.get("estado") == "ocupado" and d.get("dueno") != me:
        return f"ERROR: '{nombre}' ya es de '{d.get('dueno')}'."
    res = _put("subdomain", nombre, {"nombre": nombre, "dueno": me, "estado": "ocupado",
                                     "url": f"https://{nombre}.{DOMAIN}", "notas": notas})
    return _jd({**res, "url": f"https://{nombre}.{DOMAIN}",
                "siguiente_paso": "PUT del tar.gz a /<TU_TOKEN>/deploy/" + nombre + " (estático) o /<TU_TOKEN>/app/" + nombre + " (dinámico)"})

@mcp.tool()
def subdomain_list(solo_ocupados: bool = False) -> str:
    """Subdominios registrados."""
    rows = _rows("subdomain")
    if solo_ocupados: rows = [r for r in rows if r.get("estado") == "ocupado"]
    return _jd(rows)

@mcp.tool()
def subdomain_release(nombre: str) -> str:
    """Libera un subdominio propio (no borra archivos ni apps)."""
    me = ident()
    nombre = nombre.strip().lower()
    _, d = _get("subdomain", nombre)
    if not d: return f"ERROR: '{nombre}' no está registrado."
    if d.get("dueno") != me: return f"ERROR: '{nombre}' es de '{d.get('dueno')}'."
    d["estado"] = "libre"
    return _jd(_put("subdomain", nombre, d))

@mcp.tool()
def deploy_info() -> str:
    """Cómo desplegar un sitio estático o una app dinámica en <nombre>.<dominio>."""
    return _jd({
        "estatico": {
            "1": "subdomain_claim('<nombre>')",
            "2": "empaquetar: tar -czf sitio.tar.gz -C ./build .",
            "3": f"PUT https://{PUBLIC_HOST}/<TU_TOKEN>/deploy/<nombre>  (cuerpo = tar.gz)",
            "4": f"listo en https://<nombre>.{DOMAIN}",
        },
        "app_dinamica": {
            "1": "subdomain_claim('<nombre>')",
            "2": "incluir eva-app.toml en la raíz con:  cmd = \"python3 servidor.py\"  (escucha en $PORT)",
            "3": f"PUT https://{PUBLIC_HOST}/<TU_TOKEN>/app/<nombre>  (cuerpo = tar.gz)",
            "4": f"el servidor la corre en sandbox systemd y la publica en https://<nombre>.{DOMAIN}",
            "limites": "512M RAM, 80% CPU, sin privilegios, reinicio automático",
            "gestion": "app_status / app_logs / app_restart / app_stop / app_start",
        },
        "maximo": "50 MB por despliegue",
        "nota": "el tar.gz no debe contener rutas absolutas ni '..'",
    })

SPOOL = os.environ.get("EVASTATE_SPOOL", "/var/lib/evastate/ctl-spool")
CTL_OUT = os.environ.get("EVASTATE_CTL_OUT", "/var/lib/evastate/ctl-out")
os.makedirs(SPOOL, exist_ok=True); os.makedirs(CTL_OUT, exist_ok=True)

def _ctl_pedir(accion, nombre, extra=None):
    """Deja la petición en el spool; el ayudante root (eva-appd) la ejecuta.
    Así el servidor conserva su sandbox (ProtectSystem=strict) intacto."""
    import uuid, time
    rid = uuid.uuid4().hex
    req = {"id": rid, "accion": accion, "nombre": nombre, **(extra or {})}
    tmp = os.path.join(SPOOL, f".{rid}.tmp")
    with open(tmp, "w") as f: json.dump(req, f)
    os.replace(tmp, os.path.join(SPOOL, f"{rid}.json"))
    res_p = os.path.join(CTL_OUT, f"{rid}.json")
    for _ in range(240):                      # hasta 120 s
        if os.path.exists(res_p):
            res = json.load(open(res_p)); os.remove(res_p)
            return res.get("rc", 1), res.get("out", "")
        time.sleep(0.5)
    return 1, "timeout: el ayudante eva-appd no respondió en 120 s"

def _sudo_ctl(*args):
    accion, nombre = args[0], args[1]
    extra = {}
    if accion == "install": extra["puerto"] = int(args[2])
    if accion == "logs" and len(args) > 2: extra["lineas"] = int(args[2])
    return _ctl_pedir(accion, nombre, extra)

def _app_owned(nombre):
    me = ident()
    nombre = nombre.strip().lower()
    if not NOMBRE_RE.match(nombre): return None, "ERROR: nombre inválido."
    _, d = _get("subdomain", nombre)
    if not d or d.get("dueno") != me:
        return None, f"ERROR: '{nombre}' no es un subdominio tuyo (subdomain_claim primero)."
    return nombre, None

@mcp.tool()
def app_list() -> str:
    """Apps dinámicas registradas y su estado."""
    rows = _rows("app")
    for r in rows:
        rc, out = _sudo_ctl("status", r["_key"])
        r["_estado_systemd"] = out.splitlines()[0] if out else "?"
    return _jd(rows)

@mcp.tool()
def app_status(nombre: str) -> str:
    """Estado systemd de una app propia."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("status", nombre)
    return out or "sin salida"

@mcp.tool()
def app_logs(nombre: str, lineas: int = 40) -> str:
    """Últimas líneas de log de una app propia."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("logs", nombre, str(min(int(lineas), 200)))
    return out or "sin salida"

@mcp.tool()
def app_restart(nombre: str) -> str:
    """Reinicia una app propia."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("restart", nombre)
    return out or ("ok" if rc == 0 else f"ERROR rc={rc}")

@mcp.tool()
def app_stop(nombre: str) -> str:
    """Detiene una app propia (el subdominio vuelve a servir lo estático si existe)."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("stop", nombre)
    return out or ("ok" if rc == 0 else f"ERROR rc={rc}")

@mcp.tool()
def app_start(nombre: str) -> str:
    """Arranca una app propia detenida."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("start", nombre)
    return out or ("ok" if rc == 0 else f"ERROR rc={rc}")

# ───────────── HTTP: salud, tls-check, despliegues ─────────────
async def health(_):
    with db() as con:
        n = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    return PlainTextResponse(f"ok · {n} registros")

async def tls_check(request):
    host = (request.query_params.get("domain") or "").strip().lower()
    if host in (DOMAIN, f"www.{DOMAIN}", PUBLIC_HOST):
        return PlainTextResponse("ok")
    if not host.endswith(f".{DOMAIN}"): return PlainTextResponse("no", status_code=404)
    nombre = host[: -len(f".{DOMAIN}")]
    if "." in nombre: return PlainTextResponse("no", status_code=404)
    _, d = _get("subdomain", nombre)
    if d and d.get("estado") == "ocupado": return PlainTextResponse("ok")
    return PlainTextResponse("no", status_code=404)

def _extraer_seguro(blob, destino):
    """Extrae tar.gz rechazando rutas absolutas, '..' y enlaces."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf.getmembers():
            p = m.name
            if p.startswith("/") or ".." in p.split("/") or m.issym() or m.islnk():
                raise ValueError(f"entrada insegura en el tar: {p}")
        os.makedirs(destino, exist_ok=True)
        subprocess.run(["find", destino, "-mindepth", "1", "-delete"], check=False)
        tf.extractall(destino)

async def _leer_cuerpo(request):
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD:
            return None
    return body

async def deploy_static(request, pid):
    nombre = request.path_params["nombre"].strip().lower()
    if not NOMBRE_RE.match(nombre):
        return JSONResponse({"error": "nombre inválido"}, status_code=400)
    _, d = _get("subdomain", nombre)
    if not d or d.get("dueno") != pid or d.get("estado") != "ocupado":
        return JSONResponse({"error": f"'{nombre}' no está reservado por '{pid}' (subdomain_claim primero)"}, status_code=403)
    body = await _leer_cuerpo(request)
    if body is None: return JSONResponse({"error": "más de 50 MB"}, status_code=413)
    try:
        _extraer_seguro(body, os.path.join(SITES_DIR, nombre))
    except Exception as e:
        return JSONResponse({"error": f"tar inválido: {e}"}, status_code=400)
    d["ultimo_deploy"] = {"por": pid, "fecha": now(), "bytes": len(body), "tipo": "estatico"}
    _put("subdomain", nombre, d)
    return JSONResponse({"ok": True, "url": f"https://{nombre}.{DOMAIN}", "bytes": len(body)})

async def deploy_app(request, pid):
    nombre = request.path_params["nombre"].strip().lower()
    if not NOMBRE_RE.match(nombre):
        return JSONResponse({"error": "nombre inválido"}, status_code=400)
    _, d = _get("subdomain", nombre)
    if not d or d.get("dueno") != pid or d.get("estado") != "ocupado":
        return JSONResponse({"error": f"'{nombre}' no está reservado por '{pid}' (subdomain_claim primero)"}, status_code=403)
    body = await _leer_cuerpo(request)
    if body is None: return JSONResponse({"error": "más de 50 MB"}, status_code=413)
    destino = os.path.join(APPS_DIR, nombre)
    try:
        _extraer_seguro(body, destino)
    except Exception as e:
        return JSONResponse({"error": f"tar inválido: {e}"}, status_code=400)
    if not os.path.exists(os.path.join(destino, "eva-app.toml")):
        return JSONResponse({"error": "falta eva-app.toml (con cmd = \"...\")"}, status_code=400)
    # puerto estable por app
    aid, ad = _get("app", nombre)
    if ad and ad.get("puerto"):
        puerto = ad["puerto"]
    else:
        usados = {a.get("puerto") for a in _rows("app")}
        puerto = next(p for p in range(9100, 9900) if p not in usados)
    rc, out = _sudo_ctl("install", nombre, str(puerto))
    if rc != 0:
        return JSONResponse({"error": f"instalación falló: {out}"}, status_code=500)
    _put("app", nombre, {"nombre": nombre, "dueno": pid, "puerto": puerto,
                         "ultimo_deploy": {"por": pid, "fecha": now(), "bytes": len(body)}})
    return JSONResponse({"ok": True, "url": f"https://{nombre}.{DOMAIN}",
                         "puerto_interno": puerto, "detalle": out})

BACKUP_DIR = "/var/backups/evastate"

async def backup_get(request, pid):
    """Devuelve el respaldo mas reciente (gz). Cada cowork baja su copia al
    abrir sesion: cuatro copias en cuatro sitios."""
    import glob as _g
    files = sorted(_g.glob(os.path.join(BACKUP_DIR, "state-*.db.gz")))
    if not files:
        return JSONResponse({"error": "aun no hay respaldo"}, status_code=404)
    f = files[-1]
    data = open(f, "rb").read()
    from starlette.responses import Response
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{os.path.basename(f)}"',
        "X-Backup-Sha256": __import__("hashlib").sha256(data).hexdigest(),
    })

# ───────────── ENSAMBLE ─────────────
@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield

base = Starlette(routes=[Route("/health", health), Route("/tls-check", tls_check)],
                 lifespan=lifespan)
mcp_asgi = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:443",
                       "127.0.0.1:*", "localhost:*"],
        allowed_origins=[f"https://{PUBLIC_HOST}"],
    ),
)

async def app(scope, receive, send):
    if scope["type"] != "http":
        return await base(scope, receive, send)
    path = scope.get("path", "")
    partes = path.split("/")
    # /<token>/mcp | /<token>/deploy/<nombre> | /<token>/app/<nombre>
    if len(partes) >= 2 and partes[1] == "registro" and scope.get("method") == "POST":
        from starlette.requests import Request
        resp = await registro_post(Request(scope, receive))
        return await resp(scope, receive, send)
    if len(partes) >= 3 and _sha(partes[1]) in TOKEN_INDEX:
        pid = TOKEN_INDEX[_sha(partes[1])]
        tokvar = CURRENT.set(pid)
        try:
            resto = "/" + "/".join(partes[2:])
            if partes[2] == "mcp":
                sub = dict(scope); sub["path"] = resto; sub["raw_path"] = resto.encode()
                return await mcp_asgi(sub, receive, send)
            if partes[2] == "backup" and scope["method"] == "GET":
                resp = await backup_get(None, pid)
                return await resp(scope, receive, send)
            if partes[2] in ("deploy", "app") and len(partes) >= 4 and scope["method"] == "PUT":
                from starlette.requests import Request
                scope2 = dict(scope)
                scope2["path_params"] = {"nombre": partes[3]}
                req = Request(scope2, receive)
                req.scope["path_params"] = {"nombre": partes[3]}
                fn = deploy_static if partes[2] == "deploy" else deploy_app
                resp = await fn(req, pid)
                return await resp(scope, receive, send)
        finally:
            CURRENT.reset(tokvar)
        return await PlainTextResponse("ruta no reconocida bajo tu token", status_code=404)(scope, receive, send)
    return await base(scope, receive, send)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("EVASTATE_PORT", "8787")))
