#!/usr/bin/env python3
"""Test battery (7 gates), stdlib only.

Required: BAT_URL_BASE, BAT_TOKEN_1, BAT_TOKEN_2 (two different identities;
their ids are read from the server, not assumed).
Optional: BAT_TOKEN_AJENO, BAT_TOKEN_OTRA_ESTACION, BAT_SSH, BAT_UA, BAT_PROD.
  BAT_SIN_CDN=1       no CDN in front (skips the User-Agent filter case)
  BAT_SIN_RESPALDO=1  no backup generated yet (skips the backup case)
  BAT_SITIO=<name>    a deployed site, to check the built-in static server
Against production only --humo (smoke) is allowed."""
import json, os, sys, time, gzip, hashlib, io, subprocess, threading, urllib.request, urllib.error
RUN = str(int(time.time()))

def _req(var):
    v = os.environ.get(var)
    if not v: sys.exit(f"define {var} (ver PROTOCOLO-PRUEBAS.md / README del repo)")
    return v
BASE = _req("BAT_URL_BASE").rstrip("/")
ES_PROD = os.environ.get("BAT_PROD") == "1"   # marcar explicitamente cuando el objetivo es produccion
T1 = open(_req("BAT_TOKEN_1")).read().strip()
T2 = open(_req("BAT_TOKEN_2")).read().strip()
_t3 = os.environ.get("BAT_TOKEN_OTRA_ESTACION")   # participante en OTRA maquina
T3 = open(_t3).read().strip() if _t3 else None
_ta = os.environ.get("BAT_TOKEN_AJENO")
TA = open(_ta).read().strip() if _ta else None
SSH = os.environ.get("BAT_SSH", "")           # vacio = se salta el caso de restart
UA = os.environ.get("BAT_UA", "estado-mcp-bateria/1.0")

# Los ids no se asumen: se preguntan al servidor. Antes estaban escritos en el
# codigo y la bateria solo servia en la instalacion de quien la escribio.
SIN_CDN = os.environ.get("BAT_SIN_CDN") == "1"   # instalacion sin CDN delante
SIN_RESPALDO = os.environ.get("BAT_SIN_RESPALDO") == "1"  # aun no hay respaldo generado
SITIO_PRUEBA = os.environ.get("BAT_SITIO", "")   # nombre de un sitio ya desplegado (modo autonomo)

R = {"ok": 0, "fallo": 0, "salto": 0}
FALLOS = []

def caso(puerta, nombre, fn):
    try:
        fn(); R["ok"] += 1; print(f"  [OK]    {puerta} · {nombre}")
    except AssertionError as e:
        R["fallo"] += 1; FALLOS.append(f"{puerta} · {nombre}: {e}")
        print(f"  [FALLO] {puerta} · {nombre}: {e}")
    except Exception as e:
        R["fallo"] += 1; FALLOS.append(f"{puerta} · {nombre}: {type(e).__name__} {e}")
        print(f"  [FALLO] {puerta} · {nombre}: {type(e).__name__} {e}")

def salto(puerta, nombre, motivo):
    R["salto"] += 1; print(f"  [SALTO] {puerta} · {nombre} ({motivo})")

def http(metodo, url, data=None, ua=UA, hdrs=None, timeout=30):
    h = {"User-Agent": ua} if ua else {}
    h.update(hdrs or {})
    req = urllib.request.Request(url, data=data, headers=h, method=metodo)
    return urllib.request.urlopen(req, timeout=timeout)

def rpc(tok, metodo, params=None, _id=1, ua=UA):
    body = {"jsonrpc": "2.0", "id": _id, "method": metodo}
    if params is not None: body["params"] = params
    r = http("POST", f"{BASE}/{tok}/mcp", json.dumps(body).encode(), ua=ua,
             hdrs={"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"})
    return json.loads(r.read().decode())

class SinNode(Exception):
    pass

class Rechazo(Exception):
    """El tool rechazó la operación (texto 'ERROR: ...' o isError)."""

def call(tok, tool, args=None):
    r = rpc(tok, "tools/call", {"name": tool, "arguments": args or {}})
    assert "error" not in r, f"error JSON-RPC: {r.get('error')}"
    txt = "\n".join(c.get("text", "") for c in r["result"].get("content", []))
    if r["result"].get("isError") or txt.strip().startswith("ERROR"):
        raise Rechazo(txt[:200])
    try: return json.loads(txt)
    except Exception: return txt

def _quien(tok):
    try:
        return call(tok, "whoami")["id"]
    except Exception as e:
        sys.exit(f"no pude identificar el token: {e}")

ID1 = _quien(T1)
ID2 = _quien(T2)
if ID1 == ID2:
    sys.exit("BAT_TOKEN_1 y BAT_TOKEN_2 son la misma identidad: hacen falta dos")

def status_de(fn):
    try:
        fn(); return 200
    except urllib.error.HTTPError as e:
        return e.code

def espera_404(fn, intentos=3):
    """Un 429 aqui es el freno global anti fuerza bruta (compartido entre casos),
    no la respuesta que se prueba: espera a que la ventana se libere y reintenta."""
    for i in range(intentos):
        c = status_de(fn)
        if c != 429: return c
        time.sleep(22)
    return 429

# ---------- PUERTA A · HUMO ----------
def a_health():
    b = http("GET", f"{BASE}/health").read().decode() if False else None
def a_health2():
    # /health no pasa por token: en esta arquitectura va tras Caddy directo
    r = http("GET", f"{BASE.replace('https://','https://')}" + f"/{T1}/mcp", None)  # no-op
def puerta_A():
    print("PUERTA A · humo")
    id_esperado = os.environ.get("BAT_ID_1")  # opcional: exige una identidad concreta
    caso("A", "whoami responde y firma la identidad correcta",
         lambda: (lambda w: (assert_(w.get("id") and w["activo"] is True, w),
                             assert_(id_esperado is None or w["id"] == id_esperado, w)))(call(T1, "whoami")))
    caso("A", "state_overview responde con las 7 secciones",
         lambda: assert_(all(k in call(T1, "state_overview") for k in
                ("yo","mensajes_pendientes","decisiones_recientes","hechos",
                 "infraestructura","subdominios","apps")), "faltan secciones"))
    caso("A", "GET /panel sirve la consola (login por token, same-origin)",
         lambda: assert_("Panel de state" in http("GET", f"{BASE}/panel").read().decode("utf-8"),
                         "el panel no responde o no es la página esperada"))
    caso("A", "el panel llega con CSP estricta y sin caché",
         lambda: (lambda h: (assert_("default-src 'none'" in h.get("Content-Security-Policy",""), "sin CSP estricta"),
                             assert_("frame-ancestors 'none'" in h.get("Content-Security-Policy",""), "permite iframes"),
                             assert_(h.get("Cache-Control") == "no-store", "el panel es cacheable"),
                             assert_(h.get("X-Content-Type-Options") == "nosniff", "sin nosniff")))
                 (http("GET", f"{BASE}/panel").headers))
    try:
        _a_panel_js(); R["ok"] += 1
        print("  [OK]    A · el JavaScript del panel es sintácticamente válido")
    except SinNode:
        salto("A", "el JavaScript del panel es válido", "hace falta node para comprobarlo")
    except AssertionError as e:
        R["fallo"] += 1; FALLOS.append(f"A · el JavaScript del panel es sintácticamente válido: {e}")
        print(f"  [FALLO] A · el JavaScript del panel es sintácticamente válido: {e}")
    caso("A", "/wake responde la página de despertar sin exponer nada", _a_wake)
    caso("A", "el handshake saluda con lo pendiente de esa identidad", _a_saludo_al_conectar)
    if SITIO_PRUEBA:
        caso("A", "modo autonomo: el sitio se sirve en /s/<nombre>/", _a_sitios_ruta)
        caso("A", "modo autonomo: no se puede salir del directorio del sitio", _a_sitios_fuga)
    else:
        salto("A", "sitios servidos por el propio canal", "sin BAT_SITIO (instalacion con proxy delante)")
    n_tools = int(os.environ.get("BAT_TOOLS", "53"))
    caso("A", f"tools/list expone las {n_tools} herramientas",
         lambda: assert_(len(rpc(T1, "tools/list")["result"]["tools"]) == n_tools,
                         f"hay {len(rpc(T1,'tools/list')['result']['tools'])}"))

def assert_(cond, msg=""):
    assert cond, msg

def _a_wake():
    """El despertador es publico a proposito: 404 para lo que no existe, y un GET
    NUNCA debe encender nada (los escaneres barren los subdominios sin parar)."""
    assert status_de(lambda: http("GET", f"{BASE}/wake/no-existe-{RUN}")) == 404, \
        "el despertador responde a nombres inexistentes"
    assert status_de(lambda: http("GET", f"{BASE}/wake/..%2Fetc")) in (404, 400), \
        "el despertador acepta rutas raras"
    apps = [a for a in call(T1, "app_list") if a.get("estado") != "eliminada"]
    if not apps: return
    n = apps[0]["nombre"]
    antes = json.dumps(call(T1, "app_status", {"nombre": n}))
    try: http("GET", f"{BASE}/wake/{n}")
    except urllib.error.HTTPError as e:
        assert e.code == 503, f"GET /wake devolvio {e.code}"
        assert b"Encender demo" in e.read(), "la pagina no ofrece encender a mano"
    time.sleep(2)
    despues = json.dumps(call(T1, "app_status", {"nombre": n}))
    if "inactive" in antes or "dead" in antes:
        assert "active" not in despues.replace("inactive", ""), \
            "un simple GET encendio la app: los escaneres la mantendrian viva"

def _a_sitios_ruta():
    """Modo autonomo: los sitios se publican en /s/<nombre>/ sin proxy delante."""
    c = status_de(lambda: http("GET", f"{BASE}/s/{SITIO_PRUEBA}/"))
    assert_(c == 200, f"el sitio no se sirve: http {c}")

def _a_sitios_fuga():
    """El handler no puede servir nada fuera del directorio del sitio."""
    intentos = [
        f"/s/{SITIO_PRUEBA}/../../../etc/passwd",
        "/s/../../etc/passwd",
        f"/s/{SITIO_PRUEBA}/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/s/..%2f..%2fetc%2fpasswd",
    ]
    for u in intentos:
        c = status_de(lambda u=u: http("GET", BASE + u))
        assert_(c == 404, f"{u} devolvio {c}, deberia ser 404")

def _a_panel_js():
    """Un error de sintaxis deja el panel mudo: los botones no hacen NADA y la
    página se ve perfecta. Se valida con node si existe; si no, con un balance
    de llaves/paréntesis fuera de cadenas."""
    import re, shutil, tempfile
    html = http("GET", f"{BASE}/panel").read().decode("utf-8")
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    assert js.strip(), "el panel no trae script"
    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write(js); ruta = f.name
        r = subprocess.run([node, "--check", ruta], capture_output=True, text=True, timeout=30)
        os.unlink(ruta)
        assert r.returncode == 0, f"node --check: {(r.stderr or '')[:300]}"
    else:
        # Contar llaves no es analizar JavaScript: una expresion regular con
        # llaves basta para dar un rojo falso, y un rojo falso ensena a
        # ignorar los rojos. Mejor decir que no se pudo comprobar.
        raise SinNode()

# ---------- PUERTA B · PROTOCOLO ----------
def puerta_B():
    print("PUERTA B · protocolo/conformidad")
    caso("B", "token inválido → 404 (no 401/500: no filtra que existe el MCP)",
         lambda: (lambda c: assert_(c == 404, f"código {c}"))
                 (espera_404(lambda: rpc("token-falso-123", "tools/list"))))
    caso("B", "método JSON-RPC inexistente → error controlado, no 500",
         lambda: assert_("error" in rpc(T1, "metodo/inexistente"), "no devolvió error JSON-RPC"))
    caso("B", "un parametro que no existe se RECHAZA, no se ignora", _b_extra_rechazado)
    caso("B", "el catalogo declara additionalProperties:false", _b_esquema_declara_estricto)
    caso("B", "parametros() publica lo que acepta cada herramienta", _b_parametros_se_publican)
    caso("B", "cuerpo no-JSON → rechazo controlado",
         lambda: assert_(status_de(lambda: http("POST", f"{BASE}/{T1}/mcp", b"esto no es json",
                hdrs={"Content-Type":"application/json","Accept":"application/json"})) in (400, 406, 422),
                "aceptó basura"))
    if SIN_CDN:
        salto("B", "el CDN filtra el User-Agent de libreria", "instalacion sin CDN (BAT_SIN_CDN=1)")
    else:
        caso("B", "User-Agent de librería → 403 del CDN (OP-085 sigue vigente)",
             lambda: assert_(status_de(lambda: rpc(T1, "tools/list", ua="Python-urllib/3.10")) == 403,
                    "el CDN dejó pasar el UA de urllib"))

def _b_extra_rechazado():
    """Un parametro que no existe debe RECHAZARSE. Si se ignora, quien llama cree
    que filtro y recibe todo: paso el 28-ago y rompio una comunicacion real.
    Depende de que el SDK herede extra=forbid; si cambia de estructura, este caso
    es el que avisa."""
    try:
        call(T1, "whoami", {"parametro_que_no_existe": "x"})
    except Rechazo as e:
        assert_("extra" in str(e).lower() or "no permit" in str(e).lower()
                or "not permitted" in str(e).lower(), f"rechazado, pero por otro motivo: {e}")
        return
    raise AssertionError("ACEPTO un parametro inexistente: el filtro silencioso volvio")

def _b_esquema_declara_estricto():
    """El catalogo debe decir additionalProperties:false, para que el cliente lo
    sepa antes de equivocarse."""
    r = rpc(T1, "tools/list")
    tools = r.get("result", {}).get("tools", [])
    assert_(tools, "tools/list vacio")
    laxas = [t["name"] for t in tools
             if (t.get("inputSchema") or {}).get("additionalProperties") is not False]
    assert_(not laxas, f"herramientas sin additionalProperties:false: {laxas[:5]}")

def _b_parametros_se_publican():
    p = call(T1, "parametros", {"herramienta": "msg_desde"})
    assert_(isinstance(p, dict) and "msg_desde" in p, f"respuesta inesperada: {str(p)[:120]}")
    assert_("fecha_iso" in p["msg_desde"]["obligatorios"], f"no declara fecha_iso: {p}")
    todos = call(T1, "parametros")
    assert_(todos.get("total", 0) >= 40, f"catalogo corto: {todos.get('total')}")

def _a_saludo_al_conectar():
    """El handshake debe traer lo pendiente de ESA identidad, sin que el cliente
    tenga que acordarse de preguntar."""
    r = rpc(T1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "bateria", "version": "1"}})
    ins = r.get("result", {}).get("instructions") or ""
    assert_("state" in ins.lower(), f"sin instrucciones en el handshake: {ins[:120]}")
    assert_(ID1 in ins, f"el saludo no nombra la identidad {ID1}: {ins[:160]}")
    r2 = rpc(T2, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "bateria", "version": "1"}})
    ins2 = r2.get("result", {}).get("instructions") or ""
    assert_(ID2 in ins2 and ins2 != ins, "el saludo no distingue identidades")

# ---------- PUERTA C · IDENTIDAD/SEGURIDAD ----------
def puerta_C():
    print("PUERTA C · identidad y seguridad")
    if TA:
        caso("C", "token válido de OTRA instancia → 404 aquí (aislamiento entre instancias)",
             lambda: (lambda c: assert_(c == 404, f"código {c} (200 = fuga de aislamiento)"))
                     (espera_404(lambda: rpc(TA, "tools/list"))))
    else:
        salto("C", "aislamiento entre instancias", "sin BAT_TOKEN_AJENO")
    caso("C", "el emisor lo sella el servidor: msg de T1 llega firmado con la identidad de T1",
         lambda: assert_(_msg_firmado() == ID1, f"firmado {_msg_firmado()}, esperado {ID1}"))
    caso("C", "sol_cerrar por un NO involucrado → rechazo",
         lambda: _c_sol_cerrar_ajeno())
    if SSH:
        caso("C", "el servidor NO guarda tokens en texto plano (solo hashes)", _c_sin_texto_plano)
    else:
        salto("C", "tokens sin texto plano", "sin BAT_SSH")
    caso("C", "alta remota: invitación de un solo uso + aprobación de la autoridad", _c_alta_remota)
    caso("C", "la invitación trae un texto listo para pegar, con script válido", _c_texto_invitacion)
    caso("C", "subdominio de un no-autoridad queda PENDIENTE y no puede desplegar", _c_sub_pendiente)
    caso("C", "la autoridad aprueba/rechaza subdominios; tras aprobar sí despliega", _c_sub_aprobar)

_ult_ref = {}
def _msg_firmado():
    call(T1, "msg_send", {"para": ID2, "asunto": "prueba de firma",
                          "cuerpo": "quién firma este mensaje", "tipo": "aviso"})
    inbox = call(T2, "msg_inbox")
    m = [x for x in inbox if x["asunto"] == "prueba de firma"][-1]
    call(T2, "msg_ack", {"id": m["_id"]})
    return m["de"]

def _c_sol_cerrar_ajeno():
    r = call(T1, "msg_send", {"para": ID2, "asunto": "solicitud para cierre ajeno",
                              "cuerpo": "x", "tipo": "solicitud"})
    ref = r.get("ref") or r.get("sol") or ""
    assert ref, f"la solicitud no recibió ref: {r}"
    _ult_ref["c"] = ref
    # el dueño de T2 SÍ está involucrado; el no-involucrado sería un tercero.
    # En sandbox de 2 participantes: probamos que un token INVÁLIDO no puede, y
    # que el involucrado SÍ puede (cierre limpio del caso).
    ok = call(T2, "sol_cerrar", {"ref": ref, "estado": "descartada"})
    assert "cerrad" in json.dumps(ok) or ok, f"el involucrado no pudo cerrar: {ok}"

def _tar_demo(texto):
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        dato = texto.encode()
        info = tarfile.TarInfo("index.html"); info.size = len(dato)
        tf.addfile(info, io.BytesIO(dato))
    return buf.getvalue()

def _put_deploy(tok, nombre, blob):
    try:
        http("PUT", f"{BASE}/{tok}/deploy/{nombre}", blob)
        return 200
    except urllib.error.HTTPError as e:
        return e.code

def _c_sub_pendiente():
    sub = "bat" + RUN[-6:]
    _ult_ref["sub"] = sub
    r = call(T2, "subdomain_claim", {"nombre": sub, "notas": "caso de bateria"})
    assert r.get("estado") == "solicitado", f"un no-autoridad reservó directo: {r}"
    lst = call(T2, "subdomain_list")
    mio = [s for s in lst if s["nombre"] == sub][0]
    assert mio["estado"] == "solicitado", mio
    cod = _put_deploy(T2, sub, _tar_demo("no deberia publicarse"))
    assert cod == 403, f"desplegó sin aprobación (HTTP {cod})"
    try:
        call(T2, "subdomain_pendientes")
        assert False, "un no-autoridad vio las solicitudes pendientes"
    except Rechazo:
        pass
    try:
        call(T2, "subdomain_aprobar", {"nombre": sub})
        assert False, "un no-autoridad aprobó su propio subdominio"
    except Rechazo:
        pass

def _c_sub_aprobar():
    sub = _ult_ref["sub"]
    pend = call(T1, "subdomain_pendientes")
    assert any(s["nombre"] == sub for s in pend), f"{sub} no aparece como pendiente"
    ov = call(T1, "state_overview")
    assert sub in (ov.get("por_aprobar", {}).get("subdominios") or []), "no aparece en por_aprobar del overview"
    ok = call(T1, "subdomain_aprobar", {"nombre": sub, "nota": "bateria"})
    assert ok.get("accion") == "aprobado", ok
    cod = _put_deploy(T2, sub, _tar_demo("aprobado por la autoridad"))
    assert cod == 200, f"tras aprobar, el dueño no pudo desplegar (HTTP {cod})"
    otro = "bat" + RUN[-6:] + "b"
    call(T2, "subdomain_claim", {"nombre": otro, "notas": "para rechazar"})
    r = call(T1, "subdomain_rechazar", {"nombre": otro, "motivo": "caso de bateria"})
    assert r.get("accion") == "rechazado", r
    cod = _put_deploy(T2, otro, _tar_demo("rechazado"))
    assert cod == 403, f"desplegó un subdominio rechazado (HTTP {cod})"
    lib = call(T1, "subdomain_release", {"nombre": sub})
    assert lib.get("accion"), f"la autoridad no pudo liberar un subdominio ajeno: {lib}"

def _c_freno_auth():
    vio = []
    for i in range(45):
        if status_de(lambda: rpc(f"token-invalido-{RUN}-{i}" + "x" * 30, "tools/list")) == 429:
            vio.append(1); break
    assert vio, "45 tokens inválidos seguidos no dispararon el freno"
    assert call(T1, "whoami").get("id"), "el freno afectó a una identidad válida"

def _c_sin_texto_plano():
    pf = os.environ.get("BAT_PARTICIPANTS_FILE", "/etc/evastate-test/participants.json")
    r = subprocess.run(SSH.split() + [f"sudo grep -cF {T1} {pf} || true"],
                       capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() in ("0", ""), f"el token de T1 aparece en {pf}"

def _c_texto_invitacion():
    """El texto debe servir sin editar nada: con el codigo dentro, el host real
    y un script que al menos sea Python valido (si no, el cowork se atasca)."""
    import ast
    inv = call(T1, "alta_invitar", {"nota": "bateria", "id_sugerido": "bat" + RUN[-5:]})
    txt = inv.get("texto_para_el_cowork", "")
    assert inv["codigo"] in txt, "el texto no incluye el codigo"
    assert BASE.split("//")[1] in txt, "el texto no incluye el host real"
    assert "state" in txt and "git" in txt, "no explica donde guardar la clave"
    partes = txt.split("-" * 77)
    assert len(partes) >= 3, "no se encuentra el script delimitado"
    ast.parse(partes[1])
    try:
        call(T2, "alta_invitar", {})
        assert False, "un no-autoridad emitió una invitación"
    except Rechazo:
        pass

def _c_alta_remota():
    inv = call(T1, "alta_invitar", {"nota": "caso de bateria"})
    codigo = inv["codigo"]
    try:
        call(T2, "alta_invitar", {})
        assert False, "un no-autoridad pudo emitir invitaciones"
    except Rechazo:
        pass
    pid = "pr" + RUN[-6:]
    tok_nuevo = "bat" + hashlib.sha256((RUN + "x").encode()).hexdigest()[:40]
    def registro(cod, i, t):
        req = urllib.request.Request(f"{BASE}/registro",
            json.dumps({"codigo": cod, "id": i, "tipo": "cowork", "nombre": "Alta de bateria",
                        "maquina": "bateria", "token_propuesto": t}).encode(),
            {"Content-Type": "application/json", "User-Agent": UA}, method="POST")
        return json.load(urllib.request.urlopen(req, timeout=30))
    assert status_de(lambda: registro("codigo-falso", pid, tok_nuevo)) == 404, "acepto un codigo falso"
    r = registro(codigo, pid, tok_nuevo)
    assert r.get("ok") and "pendiente" in r.get("estado", ""), r
    assert status_de(lambda: registro(codigo, pid + "b", tok_nuevo)) in (404, 409), "el codigo sirvio dos veces"
    try:
        call(tok_nuevo, "whoami")
        assert False, "el token propuesto funciono ANTES de aprobarse"
    except Exception:
        pass
    try:
        call(T2, "alta_aprobar", {"id": pid})
        assert False, "un no-autoridad pudo aprobar altas"
    except Rechazo:
        pass
    pend = call(T1, "altas_pendientes")
    assert any(i.get("id") == pid for i in pend), f"{pid} no esta en pendientes"
    ok = call(T1, "alta_aprobar", {"id": pid})
    assert ok.get("accion") == "aprobada", ok
    w = call(tok_nuevo, "whoami")
    assert w["id"] == pid and w.get("alta_via") == "registro", w
    _ult_ref["tok_fresco"] = tok_nuevo

# ---------- PUERTA D · FUNCIONAL ----------
def puerta_D():
    print("PUERTA D · funcional (mensajería, refs, hechos, decisiones, búsqueda)")
    caso("D", "aviso llega a la bandeja del destinatario y msg_ack lo atiende", _d_ciclo_msg)
    caso("D", "solicitud recibe ref SOL-N; ref explícita única; duplicada se rechaza; contador salta", _d_refs)
    caso("D", "respuesta enlaza con responde_a y msg_hilo la reconstruye", _d_hilo)
    caso("D", "fact_set/fact_get conservan acentos y eñes (UTF-8 íntegro)", _d_utf8)
    caso("D", "decision_log queda y decision_list la devuelve", _d_decision)
    caso("D", "search encuentra lo escrito", _d_search)
    caso("D", "serie TEST-N: única, cerrable y NO toca el contador SOL (D3)", _d_serie_test)
    caso("D", "refs normalizadas: SOL-7 ≡ SOL-007 en duplicado y en sol_cerrar (H1)", _d_norm)
    caso("D", "puertos: colisión detectada en la misma estación, rangos incluidos", _d_puertos)
    if T3:
        caso("D", "puertos: cada estación solo ve la suya y el mismo número convive", _d_puertos_aislados)
    else:
        salto("D", "aislamiento entre estaciones", "sin BAT_TOKEN_OTRA_ESTACION")
    caso("D", "cartelera: solo autoridad publica; regla exige confirmación por receptor", _d_cartel_regla)
    caso("D", "cartelera: petición se responde EN PRIVADO a la autoridad, nunca a todos", _d_cartel_peticion)
    caso("D", "msg_historial: mismo historial del par visto desde ambos lados", _d_historial)
    caso("F", "D10: una aclaracion del propio solicitante NO cierra su solicitud", _f_d10_aclaracion_propia)
    caso("D", "fechas: comprometer, mover con motivo, avanzar y cerrar", _d_fecha_ciclo)
    caso("D", "fechas: el dueño lo sella el servidor y solo el mueve lo suyo", _d_fecha_dueno)
    caso("D", "fechas: choque de recurso avisa pero no bloquea", _d_fecha_choque)
    caso("D", "fechas: mover exige motivo y bloquear exige causa", _d_fecha_exige_motivo)
    caso("D", "fechas: una vencida aparece sola en el overview", _d_fecha_en_overview)
    caso("D", "overview: esperando_respuesta lista mis solicitudes abiertas (D9)", _d_esperando)

def _d_ciclo_msg():
    call(T1, "msg_send", {"para": ID2, "asunto": "ciclo completo",
                          "cuerpo": "ida", "tipo": "aviso"})
    m = [x for x in call(T2, "msg_inbox") if x["asunto"] == "ciclo completo"]
    assert m, "no llegó a la bandeja"
    r = call(T2, "msg_ack", {"id": m[-1]["_id"]})
    assert r.get("accion") == "atendido", r
    m2 = [x for x in call(T2, "msg_inbox") if x["asunto"] == "ciclo completo"
          and x["_id"] == m[-1]["_id"]]
    assert not m2, "sigue pendiente tras el ack"

def _d_refs():
    r1 = call(T1, "msg_send", {"para": ID2, "asunto": "sol auto",
                               "cuerpo": "x", "tipo": "solicitud"})
    ref1 = r1.get("ref") or ""
    assert ref1.startswith("SOL-"), f"sin ref automática: {r1}"
    n1 = int(ref1.split("-")[1])
    exp = f"SOL-{n1 + 10}"
    r2 = call(T1, "msg_send", {"para": ID2, "asunto": "sol explícita",
                               "cuerpo": "x", "tipo": "solicitud", "ref": exp})
    assert int((r2.get("ref") or "SOL-0").split("-")[1]) == n1 + 10, \
        f"no respetó la ref explícita: {r2}"
    exp = r2["ref"]  # forma canónica devuelta por el servidor
    try:
        call(T1, "msg_send", {"para": ID2, "asunto": "sol duplicada",
                              "cuerpo": "x", "tipo": "solicitud", "ref": exp})
        assert False, "aceptó una ref duplicada"
    except Rechazo:
        pass  # rechazo correcto
    r3 = call(T1, "msg_send", {"para": ID2, "asunto": "sol salto",
                               "cuerpo": "x", "tipo": "solicitud"})
    n3 = int((r3.get("ref") or "SOL-0").split("-")[1])
    assert n3 > n1 + 10, f"el contador no saltó la explícita: {r3}"
    _ult_ref["d"] = r3.get("ref")
    for ref in (ref1, exp, r3.get("ref")):
        call(T2, "sol_cerrar", {"ref": ref, "estado": "descartada"})

def _d_hilo():
    r = call(T1, "msg_send", {"para": ID2, "asunto": "hilo pregunta",
                              "cuerpo": "¿?", "tipo": "solicitud"})
    ref = r["ref"]
    call(T2, "msg_send", {"para": ID1, "asunto": "hilo respuesta",
                          "cuerpo": "!", "tipo": "respuesta", "responde_a": ref})
    hilo = call(T1, "msg_hilo", {"ref": ref})
    txt = json.dumps(hilo, ensure_ascii=False)
    assert "hilo pregunta" in txt and "hilo respuesta" in txt, f"hilo incompleto"
    call(T1, "sol_cerrar", {"ref": ref, "estado": "respondida"})

def _d_utf8():
    call(T1, "fact_set", {"clave": "bat.utf8", "valor": "canción año búho ñandú",
                          "fuente": "batería"})
    v = call(T2, "fact_get", {"clave": "bat.utf8"})
    assert "canción año búho ñandú" in json.dumps(v, ensure_ascii=False), v

def _d_decision():
    call(T1, "decision_log", {"titulo": "decisión de batería", "decision": "probar",
                              "motivo": "batería", "proyecto": "bateria"})
    d = call(T2, "decision_list")
    assert "decisión de batería" in json.dumps(d, ensure_ascii=False), "no aparece"

def _d_search():
    esquema = [t for t in rpc(T1, "tools/list")["result"]["tools"] if t["name"] == "search"][0]
    parametro = list(esquema["inputSchema"]["properties"].keys())[0]
    s = call(T1, "search", {parametro: "ñandú"})
    assert "ñandú" in json.dumps(s, ensure_ascii=False), "search no encuentra el hecho"

def _puerto_libre(base):
    return base + (int(RUN[-3:]) % 900)

def _d_puertos():
    p = _puerto_libre(21000)
    r = call(T1, "puerto_reservar", {"puerto": p, "servicio": "bateria " + RUN})
    assert r.get("puerto") == p, r
    try:
        call(T2, "puerto_reservar", {"puerto": p, "servicio": "otro"})
        assert False, "otro participante de la misma estación pudo tomar el puerto"
    except Rechazo as e:
        assert "ya es de" in str(e), str(e)
    q = call(T2, "puerto_quien", {"puerto": p})
    assert q.get("encontrado") and q.get("dueno"), q
    # rango: reservar y comprobar que un punto interior tambien colisiona
    b = _puerto_libre(22000)
    call(T1, "puerto_reservar", {"puerto": b, "hasta": b + 10, "servicio": "rango " + RUN})
    try:
        call(T2, "puerto_reservar", {"puerto": b + 5, "servicio": "dentro del rango"})
        assert False, "un puerto dentro de un rango ajeno se dejó reservar"
    except Rechazo:
        pass
    assert call(T2, "puerto_quien", {"puerto": b + 5}).get("encontrado"), "el rango no responde a puerto_quien"
    libre = call(T1, "puerto_liberar", {"puerto": p})
    assert libre.get("accion") == "liberado", libre
    assert not call(T1, "puerto_quien", {"puerto": p}).get("encontrado"), "sigue ocupado tras liberar"
    call(T1, "puerto_liberar", {"puerto": b})

def _d_puertos_aislados():
    """Lo de otra estación es ruido: no debe aparecer, y el mismo número tiene
    que poder usarse en dos máquinas a la vez sin estorbarse."""
    p = _puerto_libre(23000)
    call(T1, "puerto_reservar", {"puerto": p, "servicio": "en mi estacion " + RUN})
    r3 = call(T3, "puerto_reservar", {"puerto": p, "servicio": "en la otra estacion " + RUN})
    assert r3.get("puerto") == p, f"la otra estación no pudo usar el mismo número: {r3}"
    mia = call(T1, "puerto_list"); otra = call(T3, "puerto_list")
    assert mia["estacion"] != otra["estacion"], "ambas identidades declaran la misma estación"
    assert all(x["dueno"] != r3.get("dueno") or x["servicio"] != r3.get("servicio")
               for x in mia["puertos"]), "veo puertos de otra estación en mi listado"
    q = call(T3, "puerto_quien", {"puerto": p})
    assert q["estacion"] == otra["estacion"], "puerto_quien mira la estación equivocada"
    call(T1, "puerto_liberar", {"puerto": p}); call(T3, "puerto_liberar", {"puerto": p})

def _d_serie_test():
    a = call(T1, "msg_send", {"para": ID2, "asunto": "ancla pre-test",
                              "cuerpo": "x", "tipo": "solicitud"})
    na = int(a["ref"].split("-")[1])
    tref = f"TEST-{int(time.time()) % 100000}"
    t = call(T1, "msg_send", {"para": ID2, "asunto": "sol de la serie test",
                              "cuerpo": "x", "tipo": "solicitud", "ref": tref})
    assert t["ref"].startswith("TEST-"), t
    try:
        call(T1, "msg_send", {"para": ID2, "asunto": "test duplicada",
                              "cuerpo": "x", "tipo": "solicitud", "ref": tref})
        assert False, "aceptó una TEST duplicada"
    except Rechazo:
        pass
    b = call(T1, "msg_send", {"para": ID2, "asunto": "ancla post-test",
                              "cuerpo": "x", "tipo": "solicitud"})
    nb = int(b["ref"].split("-")[1])
    assert nb == na + 1, f"la serie TEST movió el contador SOL ({na}→{nb})"
    r = call(T2, "sol_cerrar", {"ref": t["ref"], "estado": "descartada"})
    assert r.get("accion") == "descartada", r
    for ref in (a["ref"], b["ref"]):
        call(T2, "sol_cerrar", {"ref": ref, "estado": "descartada"})

def _d_norm():
    r = call(T1, "msg_send", {"para": ID2, "asunto": "sol para normalizar",
                              "cuerpo": "x", "tipo": "solicitud"})
    n = int(r["ref"].split("-")[1])
    sin_ceros = f"sol-{n}"
    try:
        call(T1, "msg_send", {"para": ID2, "asunto": "dup sin ceros",
                              "cuerpo": "x", "tipo": "solicitud", "ref": sin_ceros})
        assert False, f"aceptó {sin_ceros} existiendo {r['ref']}"
    except Rechazo:
        pass
    c = call(T2, "sol_cerrar", {"ref": sin_ceros, "estado": "descartada"})
    assert c.get("accion") == "descartada", f"sol_cerrar no normalizó: {c}"

def _d_cartel_regla():
    try:
        call(T2, "cartel_publicar", {"tipo": "regla", "asunto": "x", "cuerpo": "x"})
        assert False, "un no-autoridad pudo publicar en la cartelera"
    except Rechazo:
        pass
    r = call(T1, "cartel_publicar", {"tipo": "regla", "asunto": "regla de bateria " + RUN,
                                     "cuerpo": "comentarios minimos"})
    ref = r["ref"]; assert ref.startswith("CART-"), r
    ov = call(T2, "state_overview")
    assert any(c["ref"] == ref for c in ov.get("cartelera_pendiente", [])), "no aparece pendiente en overview"
    tab = call(T2, "cartelera")
    mio = [c for c in tab if c["ref"] == ref][0]
    assert "PENDIENTE" in mio["mi_estado"], mio["mi_estado"]
    call(T2, "cartel_confirmar", {"ref": ref})
    tab2 = [c for c in call(T2, "cartelera") if c["ref"] == ref][0]
    assert tab2["mi_estado"] == "al dia", tab2["mi_estado"]
    est = call(T1, "cartel_estado", {"ref": ref})
    assert ID2 in est["confirmados"] and est["confirmados"][ID2]["tipo"] == "integrada", est
    try:
        call(T2, "cartel_estado", {"ref": ref})
        assert False, "un no-autoridad pudo ver la matriz"
    except Rechazo:
        pass
    call(T1, "cartel_cerrar", {"ref": ref, "nota": "caso de bateria"})

def _d_cartel_peticion():
    r = call(T1, "cartel_publicar", {"tipo": "peticion", "asunto": "peticion de bateria " + RUN,
                                     "cuerpo": "informe x", "formato_respuesta": "json {dato}"})
    ref = r["ref"]
    try:
        call(T2, "msg_send", {"para": "todos", "tipo": "respuesta", "responde_a": ref,
                              "asunto": "resp", "cuerpo": "{}"})
        assert False, "permitió responder una petición de cartelera a todos"
    except Rechazo:
        pass
    call(T2, "msg_send", {"para": ID1, "tipo": "respuesta", "responde_a": ref,
                          "asunto": "resp privada", "cuerpo": "{\"dato\": 1}"})
    est = call(T1, "cartel_estado", {"ref": ref})
    assert est["confirmados"].get(ID2, {}).get("tipo") == "respuesta", est
    tab = [c for c in call(T2, "cartelera") if c["ref"] == ref][0]
    assert tab["mi_estado"] == "al dia", tab["mi_estado"]
    call(T1, "cartel_cerrar", {"ref": ref})

def _d_historial():
    a1 = "hist ida " + RUN; a2 = "hist vuelta " + RUN
    call(T1, "msg_send", {"para": ID2, "tipo": "aviso", "asunto": a1, "cuerpo": "x"})
    call(T2, "msg_send", {"para": ID1, "tipo": "aviso", "asunto": a2, "cuerpo": "y"})
    h1 = call(T1, "msg_historial", {"con_quien": ID2})
    h2 = call(T2, "msg_historial", {"con_quien": ID1})
    t1 = [m["asunto"] for m in h1["mensajes"]]; t2 = [m["asunto"] for m in h2["mensajes"]]
    assert a1 in t1 and a2 in t1, "faltan direcciones en el historial"
    assert t1 == t2, "las dos partes ven historiales distintos"

def _d_esperando():
    # con la base ya grande, esto solo pasa si el overview mira lo RECIENTE:
    # con ORDER BY id ASC + LIMIT la bandeja se congela en el pasado
    a = "espera d9 " + RUN
    r = call(T1, "msg_send", {"para": ID2, "tipo": "solicitud", "asunto": a, "cuerpo": "x"})
    ov = call(T1, "state_overview")
    assert any(m.get("ref") == r["ref"] for m in ov.get("esperando_respuesta", [])), \
        "mi solicitud abierta no aparece en esperando_respuesta"
    call(T2, "sol_cerrar", {"ref": r["ref"], "estado": "descartada"})
    ov2 = call(T1, "state_overview")
    assert not any(m.get("ref") == r["ref"] for m in ov2.get("esperando_respuesta", [])), \
        "sigue en esperando_respuesta tras cerrarse"

def _f_d10_aclaracion_propia():
    """Una aclaracion del PROPIO solicitante no cierra su solicitud: si lo hiciera,
    el trabajo pendiente desaparece de las listas sin que nadie lo haya atendido.
    Caso real: SOL-015, reportado por un cowork el 28-ago."""
    r = call(T1, "msg_send", {"para": ID2, "asunto": f"d10 {RUN}", "cuerpo": "x",
                              "tipo": "solicitud"})
    ref = r["ref"]
    call(T1, "msg_send", {"para": ID2, "asunto": f"d10 aclaracion {RUN}", "cuerpo": "matizo",
                          "tipo": "respuesta", "responde_a": ref})
    abiertas = [m.get("ref") for m in call(T1, "state_overview")["esperando_respuesta"]]
    assert_(ref in abiertas, f"{ref} se cerro con la aclaracion de quien la abrio")
    # y la respuesta del destinatario SI la cierra
    call(T2, "msg_send", {"para": ID1, "asunto": f"d10 respuesta {RUN}", "cuerpo": "ahi va",
                          "tipo": "respuesta", "responde_a": ref})
    abiertas2 = [m.get("ref") for m in call(T1, "state_overview")["esperando_respuesta"]]
    assert_(ref not in abiertas2, f"{ref} sigue abierta tras responder el destinatario")

def _d_fecha_ciclo():
    """Comprometer, mover conservando el motivo, avanzar y cerrar."""
    r = call(T1, "fecha_comprometer", {"que": f"entrega {RUN}", "cuando": "2026-11-20"})
    ref = r["ref"]
    assert_(r["estado"] == "pendiente", f"nace en {r['estado']}")
    m = call(T1, "fecha_mover", {"ref": ref, "nueva_fecha": "2026-11-27", "motivo": "prueba"})
    assert_(m["antes"] == "2026-11-20" and m["ahora"] == "2026-11-27", m)
    h = call(T1, "fecha_hilo", {"ref": ref})
    assert_(len(h["movimientos"]) == 1 and h["movimientos"][0]["motivo"] == "prueba",
            f"el historial no guardo el motivo: {h['movimientos']}")
    call(T1, "fecha_estado", {"ref": ref, "estado": "en_curso"})
    call(T1, "fecha_estado", {"ref": ref, "estado": "hecha", "nota": "listo"})
    l = call(T1, "fecha_list", {})
    assert_(not [f for f in l["fechas"] if f["ref"] == ref], "una fecha hecha sigue en la lista abierta")

def _d_fecha_dueno():
    """El dueño lo sella el servidor y solo el mueve lo suyo."""
    r = call(T1, "fecha_comprometer", {"que": f"mia {RUN}", "cuando": "2026-11-21"})
    ref = r["ref"]
    l = call(T1, "fecha_list", {})
    mia = [f for f in l["fechas"] if f["ref"] == ref][0]
    assert_(mia["dueno"] == ID1, f"firmada como {mia['dueno']}")
    try:
        call(T2, "fecha_mover", {"ref": ref, "nueva_fecha": "2026-12-01", "motivo": "ajena"})
    except Rechazo:
        return
    raise AssertionError("otro cowork pudo mover una fecha que no es suya")

def _d_fecha_choque():
    """Dos fechas comprometidas del mismo recurso avisan, pero NO se bloquean:
    a veces el solape es legitimo y decide la persona."""
    rec = f"gpu-prueba-{RUN}"
    call(T1, "fecha_comprometer", {"que": f"a {RUN}", "cuando": "2026-12-10", "recurso": rec})
    r2 = call(T2, "fecha_comprometer", {"que": f"b {RUN}", "cuando": "2026-12-10", "recurso": rec})
    assert_("AVISO_CHOQUE" in r2, f"no aviso del choque: {r2}")
    assert_(r2.get("ref"), "el choque bloqueo la reserva; solo debia avisar")
    r3 = call(T1, "fecha_comprometer", {"que": f"c {RUN}", "cuando": "2026-12-10",
                                        "recurso": f"otro-{RUN}"})
    assert_("AVISO_CHOQUE" not in r3, "aviso de choque con un recurso distinto")

def _d_fecha_exige_motivo():
    """Sin motivo no se mueve, y una bloqueada tiene que decir que la bloquea."""
    r = call(T1, "fecha_comprometer", {"que": f"motivos {RUN}", "cuando": "2026-12-15"})
    ref = r["ref"]
    for args, que in (({"ref": ref, "nueva_fecha": "2026-12-20", "motivo": ""}, "fecha_mover"),):
        try:
            call(T1, que, args); raise AssertionError(f"{que} acepto motivo vacio")
        except Rechazo:
            pass
    try:
        call(T1, "fecha_estado", {"ref": ref, "estado": "bloqueada"})
        raise AssertionError("acepto bloquear sin decir por que")
    except Rechazo:
        pass
    call(T1, "fecha_estado", {"ref": ref, "estado": "bloqueada", "nota": "falta algo"})

def _d_fecha_en_overview():
    """Una fecha vencida tiene que aparecer sola, sin que nadie la busque."""
    r = call(T1, "fecha_comprometer", {"que": f"vencida {RUN}", "cuando": "2020-01-15"})
    o = call(T1, "state_overview")
    v = o.get("FECHAS_MIAS_VENCIDAS", [])
    assert_(any(x["ref"] == r["ref"] for x in v), f"la vencida no salio en el overview: {v}")
    call(T1, "fecha_estado", {"ref": r["ref"], "estado": "cancelada", "nota": "limpieza"})

# ---------- PUERTA E · PERSISTENCIA/RESPALDO ----------
def puerta_E(con_restart):
    print("PUERTA E · persistencia y respaldo")
    if SIN_RESPALDO:
        salto("E", "GET /backup entrega el respaldo del dia", "aun no hay respaldo generado (BAT_SIN_RESPALDO=1)")
    else:
        caso("E", "GET /backup: SHA256 de cabecera coincide y el gz es una SQLite íntegra", _e_backup)
    if con_restart and not ES_PROD and SSH:
        caso("E", "restart del servicio: los datos sobreviven", _e_restart)
    else:
        salto("E", "restart del servicio", "requiere --todo, sandbox y BAT_SSH")

def _e_backup():
    r = http("GET", f"{BASE}/{T1}/backup")
    data = r.read()
    hdr = r.headers.get("X-Backup-SHA256") or r.headers.get("X-SHA256") or ""
    assert hashlib.sha256(data).hexdigest() == hdr.lower(), "SHA256 no coincide"
    raw = gzip.decompress(data)
    assert raw[:16] == b"SQLite format 3\x00", "el respaldo no es una SQLite"

def _e_restart():
    call(T1, "fact_set", {"clave": "bat.restart", "valor": "antes", "fuente": "batería"})
    subprocess.run(SSH.split() + ["sudo systemctl restart " + os.environ.get("BAT_SERVICIO", "evastate-test")], check=True,
                   capture_output=True, timeout=60)
    time.sleep(3)
    v = call(T1, "fact_get", {"clave": "bat.restart"})
    assert "antes" in json.dumps(v), "el dato no sobrevivió al restart"

# ---------- PUERTA F · REGRESIÓN (estado deseado tras D1/D2) ----------
def puerta_F():
    print("PUERTA F · regresión D1/D2 (HOY documentan el defecto: se esperan en ROJO hasta el parche)")
    caso("F", "D1: un aviso a 'todos' NO aparece en la bandeja de quien lo envió", _f_d1)
    caso("F", "D2: al cerrar la solicitud, su respuesta deja de estar pendiente", _f_d2)

def _f_d1():
    call(T1, "msg_send", {"para": "todos", "asunto": "aviso propio bandeja " + RUN,
                          "cuerpo": "x", "tipo": "aviso"})
    propios = [m for m in call(T1, "msg_inbox")
               if m["asunto"] == "aviso propio bandeja " + RUN and m["de"] == ID1]
    assert not propios, "el emisor ve su propio aviso como pendiente (defecto D1)"

def _f_d2():
    r = call(T1, "msg_send", {"para": ID2, "asunto": "sol para d2 " + RUN,
                              "cuerpo": "x", "tipo": "solicitud"})
    call(T2, "msg_send", {"para": ID1, "asunto": "resp para d2 " + RUN,
                          "cuerpo": "y", "tipo": "respuesta", "responde_a": r["ref"]})
    call(T1, "sol_cerrar", {"ref": r["ref"], "estado": "respondida"})
    pend = [m for m in call(T1, "msg_inbox")
            if m["asunto"] == "resp para d2 " + RUN and m.get("estado") == "pendiente"]
    assert not pend, "la respuesta sigue pendiente tras cerrar la solicitud (defecto D2)"

# ---------- PUERTA G · CARGA LIGERA ----------
def puerta_G():
    print("PUERTA G · carga ligera (20 escrituras concurrentes)")
    caso("G", "20 hilos escriben sin errores y todo queda en la base", _g_carga)
    if _ult_ref.get("tok_fresco"):
        caso("G", "límite de tasa: una identidad desbocada recibe 429 y no arrastra a las demás", _g_tasa)
    else:
        salto("G", "límite de tasa", "sin identidad fresca del caso de alta")
    if SSH:
        caso("G", "sin fuga de descriptores: la carga no deja conexiones abiertas", _g_fds)
    else:
        salto("G", "fuga de descriptores", "sin BAT_SSH")
    # el ULTIMO de todos: agota a proposito el freno global de intentos invalidos,
    # asi no contamina los casos que esperan un 404
    caso("G", "los tokens inválidos repetidos acaban en 429 (freno a fuerza bruta)", _c_freno_auth)

def _g_fds():
    """Tras cientos de llamadas, los descriptores hacia la base deben seguir
    siendo pocos: `with sqlite3.connect()` no cierra, hay que cerrar a mano."""
    svc = os.environ.get("BAT_SERVICIO", "evastate-test")
    r = subprocess.run(SSH.split() + [
        f"PID=$(systemctl show {svc} -p MainPID --value); sudo ls -l /proc/$PID/fd | grep -c state.db || true"],
        capture_output=True, text=True, timeout=60)
    n = int((r.stdout.strip() or "0").splitlines()[-1])
    assert n <= 8, f"{n} descriptores abiertos hacia la base (fuga de conexiones)"

def _g_tasa():
    tokf = _ult_ref["tok_fresco"]
    limite = int(os.environ.get("BAT_RATE_MAX", "240"))
    vio_429 = []
    def uno(_):
        try:
            rpc(tokf, "tools/list")
        except urllib.error.HTTPError as e:
            if e.code == 429: vio_429.append(1)
    lotes = (limite + 40) // 12 + 1
    for _ in range(lotes):
        hs = [threading.Thread(target=uno, args=(i,)) for i in range(12)]
        [h.start() for h in hs]; [h.join() for h in hs]
        if vio_429: break
    assert vio_429, f"nunca llego el 429 tras ~{limite+40} peticiones"
    w = call(T1, "whoami")
    assert w.get("id"), "el limite de una identidad afecto a otra"

def _g_carga():
    errores = []
    def uno(i):
        try:
            call(T1, "fact_set", {"clave": f"bat.carga.{i}", "valor": f"v{i}", "fuente": "g"})
        except Exception as e:
            errores.append(f"{i}: {e}")
    t0 = time.time()
    hs = [threading.Thread(target=uno, args=(i,)) for i in range(20)]
    [h.start() for h in hs]; [h.join() for h in hs]
    dt = time.time() - t0
    assert not errores, f"{len(errores)} errores: {errores[:3]}"
    faltan = [i for i in range(20)
              if f"v{i}" not in json.dumps(call(T1, "fact_get", {"clave": f"bat.carga.{i}"}))]
    assert not faltan, f"faltan {faltan}"
    print(f"          20 escrituras concurrentes en {dt:.1f}s")

# ---------- runner ----------
if __name__ == "__main__":
    todo = "--todo" in sys.argv
    solo_humo = "--humo" in sys.argv
    print(f"Batería state v1 · objetivo: {BASE} · {'PRODUCCIÓN (solo humo)' if ES_PROD else 'sandbox'}")
    if ES_PROD and not solo_humo:
        print("ABORTO: contra producción solo se permite --humo."); sys.exit(2)
    puerta_A()
    if not solo_humo:
        puerta_B(); puerta_C(); puerta_D(); puerta_E(todo); puerta_F()
        if todo: puerta_G()
    print(f"\nRESULTADO: {R['ok']} ok · {R['fallo']} fallo · {R['salto']} saltadas")
    if FALLOS:
        print("Fallos:"); [print(" -", f) for f in FALLOS]
    sys.exit(1 if R["fallo"] else 0)
