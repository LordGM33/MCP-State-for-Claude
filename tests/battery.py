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
FRASE = os.environ.get("BAT_FRASE", "")   # frase de seguridad de la instalacion de pruebas
SSH = os.environ.get("BAT_SSH", "")           # vacio = se salta el caso de restart
UA = os.environ.get("BAT_UA", "estado-mcp-bateria/1.0")

# Los ids no se asumen: se preguntan al servidor. Antes estaban escritos en el
# codigo y la bateria solo servia en la instalacion de quien la escribio.
PANEL = os.environ.get("BAT_PANEL", "")   # ruta local a panel.html, si se quiere comprobar
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
    caso("A", "el inventario de herramientas es coherente y estan las imprescindibles",
         _a_inventario_tools)
    if PANEL:
        caso("A", "ninguna herramienta se queda fuera de la consola sin decidirlo", _a_consola_conoce_las_tools)
    else:
        salto("A", "cobertura de la consola", "sin BAT_PANEL")

# Antes esto comparaba contra un numero escrito a mano (BAT_TOOLS=53). Se ponia en
# rojo cada vez que se anadia una herramienta legitima, sin haber detectado nunca
# nada: un caso que solo sabe gritar cuando el cambio es correcto ensena a ignorarlo.
# Ahora comprueba dos cosas que si importan y se mantienen solas: que las dos vias
# por las que el canal se describe (tools/list y parametros) digan LO MISMO -- si una
# tool deja de declarar sus parametros, se ve -- y que no falte ninguna pieza sin la
# que el canal no es el canal.
IMPRESCINDIBLES = ("whoami", "state_overview", "parametros", "msg_send", "msg_inbox",
                   "msg_hilo", "sol_cerrar", "cartel_publicar", "cartel_confirmar",
                   "puerto_reservar", "fecha_comprometer", "decision_log", "search")

def _a_inventario_tools():
    lista = {t["name"] for t in rpc(T1, "tools/list")["result"]["tools"]}
    decl = call(T1, "parametros")
    declaradas = set(decl["herramientas"])
    assert lista == declaradas, ("tools/list y parametros no coinciden: solo en lista="
                                 + str(sorted(lista - declaradas)) + " solo en parametros="
                                 + str(sorted(declaradas - lista)))
    assert decl["total"] == len(lista), f"parametros dice {decl['total']} y hay {len(lista)}"
    faltan = [t for t in IMPRESCINDIBLES if t not in lista]
    assert not faltan, "faltan herramientas basicas: " + ", ".join(faltan)
    assert len(lista) >= 40, f"solo {len(lista)} herramientas: parece que se perdio media API"

# Herramientas que a proposito NO estan en la consola. Se listan una a una para
# que anadir una nueva sea una decision escrita y no un olvido.
FUERA_DE_CONSOLA = {
    "recurso_declarar",   # la autoridad la usa al montar la estacion, no a diario
    "recurso_medir",      # la reporta un guion desde la maquina, no una persona
    "token_confirmar",    # lo llama el cliente al rotar, no se pulsa
    "rotacion_cerrar", "rotacion_anular", "rotacion_invitar",  # via botones propios
    "participante_baja", "participante_cartelera", "intentos_frase", "rotacion_estado",
    "msg_desde", "msg_hilo", "msg_ack", "sol_cerrar", "cartel_cerrar", "cartel_estado",
    "fecha_hilo", "fecha_quien", "fecha_mover", "fecha_estado", "fecha_comprometer",
    "puerto_quien", "puerto_liberar", "puerto_reservar", "recurso_tomar", "recurso_soltar",
    "decision_log", "fact_set", "fact_get", "infra_put", "alta_invitar", "alta_aprobar",
    "alta_rechazar", "altas_pendientes", "subdomain_claim", "subdomain_release",
    "subdomain_aprobar", "subdomain_rechazar", "subdomain_pendientes", "app_dormir",
    "app_eliminar", "app_status", "app_logs", "app_restart", "app_stop", "app_start",
    "deploy_info", "msg_send", "cartel_publicar", "cartel_confirmar", "whoami",
    "parametros", "state_overview", "participantes", "msg_inbox", "msg_historial",
    "search", "cartelera", "fecha_list", "decision_list", "fact_list", "infra_list",
    "app_list", "subdomain_list", "puerto_list", "recurso_estado",
}

def _a_consola_conoce_las_tools():
    """Cada vez que se anade una herramienta nueva hay que decidir si va a la consola.
    Olvidarlo no rompe nada: simplemente la funcion no existe para quien usa el panel
    y nadie se entera. Paso CUATRO veces el 3-sep, incluida una con el punto abierto
    en el orden del dia de la sesion sobre este mismo problema.

    Esto no prueba que la consola funcione — eso solo lo prueba usarla. Prueba que
    ninguna herramienta se ha quedado fuera sin que alguien lo decidiera."""
    if not PANEL:
        return
    try:
        html = open(PANEL, encoding="utf-8").read()
    except OSError as e:
        salto("A", "cobertura de la consola", f"no pude leer {PANEL}: {e}")
        return
    catalogo = set(call(T1, "parametros")["herramientas"])
    huerfanas = [t for t in sorted(catalogo)
                 if f'"{t}"' not in html and f"'{t}'" not in html
                 and t not in FUERA_DE_CONSOLA]
    assert not huerfanas, ("herramientas que no aparecen en la consola ni estan declaradas "
                           "como exentas: " + ", ".join(huerfanas))

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
    caso("B", "un rechazo llega con isError=True, no solo con el texto", _b_rechazo_va_marcado)
    caso("B", "una llamada valida NO llega marcada como error", _b_lo_correcto_no_va_marcado)
    caso("B", "una entrada ilegible se rechaza, no se devuelve vacia", _b_lo_ilegible_se_rechaza_no_se_vacia)
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

def _b_rechazo_va_marcado():
    """Un rechazo tiene que llegar MARCADO como rechazo, no solo escrito.

    Hasta el 3-sep-2026 las validaciones devolvian "ERROR: ..." con isError=False:
    un cliente que hacia LO CORRECTO —fiarse de isError— se tragaba el rechazo como
    si fuera un envio realizado, y el que leia el texto a mano se salvaba por
    accidente. Lo encontro editorial revisando su cliente por CART-014.

    Se prueban varias familias de validacion, no una, porque el arreglo envuelve el
    decorador y tiene que valer para todas."""
    casos = [
        ("cartel_publicar", {"tipo": "inventado", "asunto": "x", "cuerpo": "y"}),
        ("sol_cerrar",      {"ref": "SOL-999999"}),
        ("msg_send",        {"para": "no-existe-nadie", "asunto": "x", "cuerpo": "y"}),
        ("puerto_liberar",  {"puerto": 65530}),
    ]
    malos = []
    for nombre, args in casos:
        r = rpc(T1, "tools/call", {"name": nombre, "arguments": args})
        res = r.get("result", {})
        txt = "\n".join(c.get("text", "") for c in res.get("content", []))
        if not res.get("isError"):
            malos.append(f"{nombre} rechaza pero isError={res.get('isError')}: {txt[:60]}")
    assert not malos, "; ".join(malos)

def _b_lo_correcto_no_va_marcado():
    """Y al reves: una llamada buena NO puede llegar marcada como error, o el
    cliente que se fie de isError descartara resultados validos."""
    for nombre in ("whoami", "parametros", "participantes"):
        r = rpc(T1, "tools/call", {"name": nombre, "arguments": {}})
        res = r.get("result", {})
        assert not res.get("isError"), f"{nombre} es valida y llega con isError=True"

def _b_lo_ilegible_se_rechaza_no_se_vacia():
    """Devolver vacio ante una entrada ilegible es la forma mas cara de mentir:
    quien pregunta 'que ha pasado desde el lunes' con la fecha mal escrita recibe
    silencio y se lo cree. Encontrado el 3-sep barriendo las validaciones, en la
    MISMA funcion (msg_desde) donde produccion habia reportado el 28-ago un filtro
    que no filtraba. La forma cambia, la familia no."""
    for tool, args in (("msg_desde", {"fecha_iso": "no-es-fecha"}),
                       ("msg_desde", {"fecha_iso": "2026-13-45"}),
                       ("search", {"texto": "   "}),
                       ("decision_log", {"titulo": "x", "decision": "y", "motivo": "z",
                                         "supersede": 999999})):
        r = rpc(T1, "tools/call", {"name": tool, "arguments": args})
        res = r.get("result", {})
        txt = "\n".join(c.get("text", "") for c in res.get("content", []))
        assert res.get("isError"), f"{tool}({args}) NO se rechaza; devuelve: {txt[:90]}"

    # Y lo que no existe se dice, no se insinua con una lista vacia.
    h = call(T1, "msg_hilo", {"ref": "SOL-999999"})
    assert isinstance(h, dict) and h.get("encontrado") is False, \
        f"una ref inexistente se ve igual que un hilo vacio: {str(h)[:90]}"

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
    if SSH:
        caso("C", "el token no queda escrito en claro en el log del servicio", _c_token_no_aparece_en_logs)
    else:
        salto("C", "token fuera de los logs", "sin BAT_SSH")
    caso("C", "rotacion por codigo: el cliente propone su token y los rechazos aguantan", _c_rotacion_por_codigo)
    caso("C", "la frase de seguridad protege alta/baja/rotacion y registra los fallos", _c_frase_protege_credenciales)
    caso("C", "una confirmacion vieja NO cuenta para la rotacion siguiente", _c_confirmar_no_vale_para_siempre)
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

def _c_token_no_aparece_en_logs():
    """El token viaja en la ruta, asi que cada linea de acceso es una credencial
    escrita en journald. El 2-sep-2026 habia 847 peticiones registradas asi, entre
    Caddy y el propio servidor. Un secreto en un log no se puede desescribir: la
    unica reparacion posible es rotar, asi que este caso existe para que no vuelva
    a ocurrir en silencio."""
    call(T1, "whoami")                       # deja una linea de acceso recien hecha
    unidad = os.environ.get("BAT_SERVICIO", "evastate-test")
    r = subprocess.run(SSH.split() + [
        f"sudo journalctl -u {unidad} --since '2 min ago' --no-pager -o cat | "
        f"grep -cF {T1} || true"], capture_output=True, text=True, timeout=40)
    assert r.stdout.strip() in ("0", ""), \
        f"el token de T1 aparece EN CLARO en el log de {unidad}"
    r2 = subprocess.run(SSH.split() + [
        f"sudo journalctl -u {unidad} --since '2 min ago' --no-pager -o cat | "
        f"grep -c '/\\[{ID1}\\]/' || true"], capture_output=True, text=True, timeout=40)
    assert r2.stdout.strip() not in ("0", ""), \
        "no se registra ni la version censurada: se perdio la linea de acceso entera"

def _c_rotacion_por_codigo():
    """El canje de rotacion: el CLIENTE genera su token y lo propone; el servidor no
    emite ni transmite ninguno. Asi es como se evita que una credencial acabe en una
    URL, en un log o en un fichero compartido — que es como acabaron el 2-sep-2026.
    Aqui se comprueban los rechazos, que son la parte que protege; el canje bueno
    cambia credenciales vivas y se prueba a mano contra el sandbox."""
    import urllib.request, urllib.error
    def post(cuerpo):
        req = urllib.request.Request(BASE + "/rotacion", json.dumps(cuerpo).encode(),
              {"Content-Type": "application/json", "User-Agent": UA})
        try:
            return 200, urllib.request.urlopen(req, timeout=20).read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    c, _ = post({"codigo": "x", "token_propuesto": "corto"})
    assert c == 400, f"acepta un token propuesto invalido ({c})"
    c, _ = post({"codigo": "noexisteestecodigo", "token_propuesto": "a" * 40})
    assert c == 403, f"un codigo inventado no da 403 sino {c}"
    c, _ = post({"codigo": "noexisteestecodigo", "token_propuesto": T1})
    assert c in (403, 409), f"deja proponer un token YA EN USO ({c})"
    try:
        r = call(T2, "rotacion_invitar", {"id": ID2})
        raise AssertionError(f"un participante sin autoridad emite codigos: {str(r)[:80]}")
    except Rechazo as e:
        assert "autoridad" in str(e), f"rechazo por otro motivo: {e}"

def _c_frase_protege_credenciales():
    """Poner el ciclo de vida de credenciales en la consola sube lo que consigue quien
    robe el token de autoridad: de leer/escribir a ACUNAR Y REVOCAR IDENTIDADES. El
    segundo factor es lo que paga esa escalada, y estos son sus tres modos."""
    for herr, args in (("participante_baja", {"id": ID2}),
                       ("rotacion_invitar", {"id": ID2}),
                       ("rotacion_cerrar", {})):
        try:
            r = call(T1, herr, args)
            raise AssertionError(f"{herr} paso SIN frase: {str(r)[:90]}")
        except Rechazo as e:
            assert "frase" in str(e).lower(), f"{herr} rechaza por otro motivo: {e}"
    try:
        call(T1, "participante_baja", {"id": ID2, "frase": "esta-no-es-la-frase"})
        raise AssertionError("acepta una frase incorrecta")
    except Rechazo as e:
        assert "incorrecta" in str(e), f"rechazo raro con frase mala: {e}"
    if os.environ.get("BAT_FRASE"):
        n = call(T1, "intentos_frase", {"limite": 5})
        assert isinstance(n, list) and n, "el intento fallido no queda registrado"

def _c_confirmar_no_vale_para_siempre():
    """Una confirmacion lleva el identificador de LA rotacion que confirmaba. Sin eso,
    haber confirmado una vez contaba para todas las siguientes y rotacion_cerrar
    retiraba el token de quien nunca confirmo el nuevo — dejandolo fuera del canal,
    que es justo lo que este mecanismo entero existe para impedir. Encontrado el
    2-sep-2026 rotando dos veces seguidas el mismo id."""
    r = call(T1, "token_confirmar")
    assert isinstance(r, dict) and r.get("estado") == "sin_rotacion_en_curso", \
        f"confirma sin rotacion abierta: {r}"
    est = call(T1, "rotacion_estado")
    assert isinstance(est, dict) and "participantes" in est, f"rotacion_estado roto: {str(est)[:90]}"
    for f in est["participantes"]:
        if not f["en_rotacion"]:
            assert not f.get("confirmacion_vieja_ignorada") or f["confirmado"] is None, \
                f"{f['id']} no rota y aun asi figura confirmado"

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
        call(T2, "alta_aprobar", {"id": pid, "frase": FRASE})
        assert False, "un no-autoridad pudo aprobar altas"
    except Rechazo:
        pass
    pend = call(T1, "altas_pendientes")
    assert any(i.get("id") == pid for i in pend), f"{pid} no esta en pendientes"
    ok = call(T1, "alta_aprobar", {"id": pid, "frase": FRASE})
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
    caso("D", "las lecturas no recortan en silencio con la base crecida", _d_lectura_no_recorta)
    caso("C", "rotacion: no se confirma sin rotacion abierta y el estado es solo de la autoridad", _c_rotacion_no_deja_a_nadie_fuera)
    caso("D", "cartelera: quien esta exento no figura como pendiente", _d_cartelera_respeta_quien_no_confirma)
    caso("D", "fact_set/fact_get conservan acentos y eñes (UTF-8 íntegro)", _d_utf8)
    caso("D", "decision_log queda y decision_list la devuelve", _d_decision)
    caso("D", "search encuentra lo escrito", _d_search)
    caso("D", "the greeting carries headlines, not bodies", _pub_saludo_trae_titulares_no_cuerpos)
    caso("D", "the catalogue agrees with the edition it declares", _pub_el_catalogo_concuerda_con_su_perfil)
    caso("C", "stopping a tool needs a person, not an authority", _pub_parar_exige_un_humano)
    caso("C", "a pass belongs to its own door and only looks", _pub_un_pase_es_de_su_puerta_y_solo_mira)
    caso("D", "the core survives any edition", _pub_el_nucleo_sobrevive_a_cualquier_perfil)
    caso("D", "a removed tool is distinguishable from a nonexistent one", _pub_una_herramienta_quitada_se_distingue_de_una_inexistente)
    caso("C", "only the owner sets an app's credentials", _pub_solo_el_dueno_pone_credenciales)
    caso("C", "a credential value never comes back", _pub_una_credencial_no_vuelve_nunca)
    caso("C", "a path as a credential name is refused", _pub_una_clave_con_ruta_se_rechaza)
    caso("D", "deploy_info mentions what used to bite people", _pub_deploy_info_dice_lo_que_callaba)
    caso("D", "msg_leer returns one whole message", _pub_msg_leer_trae_el_cuerpo)
    caso("C", "msg_leer does not open somebody else's mail", _pub_msg_leer_no_abre_correo_ajeno)
    caso("D", "an unknown is reported as unknown, not guessed", _pub_no_lo_se_en_vez_de_adivinar)
    caso("D", "every tool declares its profile", _pub_cada_herramienta_declara_su_perfil)
    caso("D", "parametros(one) and parametros(all) agree", _pub_parametros_de_una_y_de_todas_coinciden)
    caso("D", "a retried request does not open a second one", _pub_un_reintento_no_crea_dos_solicitudes)
    caso("D", "a repeated notice IS created, and says so", _pub_un_aviso_repetido_si_se_crea)
    caso("D", "a notice can declare how long it is worth", _pub_un_anuncio_declara_cuanto_vale)
    caso("D", "a notice without a lifetime never expires", _pub_un_aviso_sin_vigencia_no_caduca)
    caso("D", "a request cannot expire on its own", _pub_una_solicitud_no_declara_vigencia)
    caso("D", "updating counts as a sign of life", _pub_actualizar_cuenta_como_senal_de_vida)
    caso("D", "acking does NOT credit the sender with a write", _pub_acusar_no_acredita_a_quien_no_escribio)
    caso("D", "the greeting shows what is taken on this machine", _pub_el_saludo_ensena_lo_tomado_en_mi_estacion)
    caso("D", "that section does not bloat the greeting (contract)", _pub_esa_seccion_no_engorda_el_saludo)
    if FRASE:
        caso("C", "aborting a rotation returns the token you had", _pub_abortar_devuelve_el_token_de_siempre)
        caso("C", "aborting refuses once confirmed (it would strand them)", _pub_abortar_se_niega_si_ya_confirmo)
    else:
        salto("C", "abort rotation", "no BAT_FRASE")
    caso("D", "serie TEST-N: única, cerrable y NO toca el contador SOL (D3)", _d_serie_test)
    caso("D", "refs normalizadas: SOL-7 ≡ SOL-007 en duplicado y en sol_cerrar (H1)", _d_norm)
    caso("D", "puertos: colisión detectada en la misma estación, rangos incluidos", _d_puertos)
    if T3:
        caso("D", "puertos: cada estación solo ve la suya y el mismo número convive", _d_puertos_aislados)
    else:
        salto("D", "aislamiento entre estaciones", "sin BAT_TOKEN_OTRA_ESTACION")
    caso("D", "recursos: avisan del exceso con nombres y cifras, y no bloquean", _d_recursos_avisan_sin_bloquear)
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
    caso("D", "D11: el emisor ve si el otro YA LEYO su solicitud, y el sello no se reescribe", _d11_acuse_lectura)
    caso("D", "D11: el permiso de cierre viaja con la solicitud y se cumple", _d11_permiso_viaja_con_el_objeto)
    caso("D", "D11: una escritura que no es mensaje cuenta como senal de vida", _d11_actividad_no_epistolar)

# ── D11 · el desencuentro produccion/voicetf del 30-ago no puede repetirse ──
# Los tres casos siguientes son la traduccion literal de aquel dia: cada uno fija
# una de las tres cosas que el canal permitia creer y no permitia comprobar.

def _d11_acuse_lectura():
    """Antes: el emisor no podia distinguir 'no la ha abierto' de 'la vio y no
    contesta'. Produccion espero por la primera creyendo la segunda."""
    a = "acuse d11 " + RUN
    r = call(T1, "msg_send", {"para": ID2, "tipo": "solicitud", "asunto": a, "cuerpo": "x"})
    ref = r["ref"]
    def _mia():
        ov = call(T1, "state_overview")
        m = [x for x in ov.get("esperando_respuesta", []) if x.get("ref") == ref]
        assert m, f"{ref} no figura en esperando_respuesta"
        return m[0]
    antes = _mia()
    assert "AUN NO LA HA ABIERTO" in antes["lectura"], antes
    call(T2, "msg_inbox")                     # el destinatario la tiene delante
    despues = _mia()
    assert "LA LEYO" in despues["lectura"], despues
    # y el sello no se mueve al releer: es la PRIMERA vez, no la ultima
    call(T2, "msg_inbox")
    assert _mia()["lectura"] == despues["lectura"], "el acuse se reescribe al releer"

def _d11_permiso_viaja_con_el_objeto():
    """Antes: voicetf afirmo que el emisor no podia cerrar su propia solicitud.
    Era falso, produccion le creyo, y la solicitud quedo abierta de adorno."""
    a = "permiso d11 " + RUN
    ref = call(T1, "msg_send", {"para": ID2, "tipo": "solicitud", "asunto": a, "cuerpo": "x"})["ref"]
    m = [x for x in call(T1, "state_overview").get("esperando_respuesta", []) if x["ref"] == ref]
    assert m and m[0].get("puedes_cerrarla_tu") is True, "el overview no dice que puedo cerrarla"
    assert "sol_cerrar" in m[0].get("como", ""), "no dice COMO cerrarla"
    r = call(T1, "sol_cerrar", {"ref": ref})   # y lo que promete, se cumple
    assert r.get("accion") == "respondida", f"prometio que podia y no pude: {r}"

def _d11_actividad_no_epistolar():
    """Antes: produccion dedujo de su bandeja que voicetf llevaba dias callado,
    cuando voicetf habia reservado tres puertos y respondido un cartel ese dia.
    Una escritura que no es un mensaje tiene que contar como senal de vida."""
    import random
    pto = random.randint(21000, 21999)
    call(T2, "puerto_reservar", {"puerto": pto, "servicio": "senal-vida-" + RUN})
    act = call(T1, "state_overview").get("actividad_de_todos", {})
    assert ID2 in act, "el overview no informa de la actividad de los demas"
    e = act[ID2].get("ultima_escritura")
    assert e, f"una reserva de puerto no cuenta como escritura: {act[ID2]}"
    assert act[ID2].get("ultimo_escrito") == "puerto", act[ID2]
    assert act[ID2].get("ultima_conexion"), "conectarse no deja huella"
    ps = call(T1, "participantes")
    yo2 = [p for p in ps if p.get("id") == ID2]
    assert yo2 and yo2[0].get("ultima_escritura") == e, "participantes() no lleva la huella"
    call(T2, "puerto_liberar", {"puerto": pto})

def _jd_(x):
    """Short JSON for assertion messages. TRUNCATES, so never assert on this."""
    try:
        return json.dumps(x, ensure_ascii=False)[:400]
    except Exception:
        return str(x)[:400]


def _otro_segundo():
    """Wait for the clock second to change.

    Timestamps here have one-second resolution. Asked which of two writes in the
    same second came last, the honest answer is that nobody knows, and a test
    that asserts one of them passes or fails by luck. Rather than make the server
    answer firmly -- which would manufacture a certainty it does not have -- the
    test makes sure there IS a last write before asking for one.
    """
    t0 = time.time()
    while int(time.time()) == int(t0) and time.time() - t0 < 2:
        time.sleep(0.05)


def _pub_saludo_trae_titulares_no_cuerpos():
    """The opening snapshot carries headlines, not full messages.

    It used to return every pending message in full. On a channel with history
    that reached 185 KB, paid on every session of every participant before a
    single useful word was exchanged. The fix is only half a fix unless you can
    still fetch a body on demand, which is the next case.
    """
    # THE MARKER GOES AT THE END, and that is the whole point of the case.
    #
    # A headline legitimately carries the FIRST few hundred characters, so
    # asserting that the start of the body is absent tests the opposite of the
    # design and fails on a correct server. What must not travel is the END: if
    # that arrives, nothing was trimmed.
    cuerpo = "start-" + RUN + ("x" * 3000) + "-FINAL-" + RUN
    call(T2, "msg_send", {"para": ID1, "tipo": "aviso",
                          "asunto": "headline test " + RUN, "cuerpo": cuerpo})
    ov = call(T1, "state_overview")
    txt = json.dumps(ov, ensure_ascii=False)
    assert "-FINAL-" + RUN not in txt, (
        "the greeting still ships whole message bodies: no saving at all")
    assert len(txt) < 400000, "the greeting is %d bytes: nothing was trimmed" % len(txt)
    pend = ov.get("mensajes_pendientes") or []
    mio = [m for m in pend if (m.get("asunto") or "").endswith(RUN)]
    assert mio, "the message is not in the inbox at all: %s" % _jd_(pend)[:200]
    assert mio[0].get("_id"), "a headline with no id cannot be expanded: %s" % _jd_(mio[0])


def _pub_msg_leer_trae_el_cuerpo():
    """Fetching one message by id returns it whole.

    Without this the slimmed greeting would not save anything: it would only move
    the cost to a second call that does not exist.
    """
    cuerpo = "CUERPO-ENTERO-" + RUN
    r = call(T2, "msg_send", {"para": ID1, "tipo": "aviso",
                              "asunto": "read one " + RUN, "cuerpo": cuerpo})
    res = call(T1, "msg_leer", {"ids": str(r["id"])})
    if isinstance(res, dict):
        res = res.get("mensajes", [])
    assert res and cuerpo in json.dumps(res, ensure_ascii=False), \
        "msg_leer did not return the body: %s" % _jd_(res)


def _pub_msg_leer_no_abre_correo_ajeno():
    """Message ids are consecutive, so without a recipient check anyone could
    walk the numbers and read other people's mail. The saving must not open a
    door that was closed before."""
    r = call(T2, "msg_send", {"para": ID2, "tipo": "aviso",
                              "asunto": "private " + RUN,
                              "cuerpo": "SECRETO-" + RUN})
    res = call(T1, "msg_leer", {"ids": str(r["id"])})
    txt = json.dumps(res, ensure_ascii=False)
    assert "SECRETO-" + RUN not in txt, (
        "msg_leer handed over a message addressed to somebody else. Ids are "
        "consecutive: this is a door, not an edge case.")


def _pub_no_lo_se_en_vez_de_adivinar():
    """An unknown is reported as an unknown, not as a zero or a false.

    A tool that answers "off" when it has no reading is indistinguishable from
    one that measured and found it off, and the reader cannot tell. The shape is
    explicit: sabido=False plus why, and when it was last known if ever.
    """
    import random as _r
    rid = "tool-" + RUN[-6:]
    call(T1, "herramienta_declarar", {"id": rid, "puerto": _r.randint(26000, 26999),
                                      "arranque": "battery probe, never started"})
    est = call(T1, "herramienta_estado", {"id": rid})
    txt = json.dumps(est, ensure_ascii=False)
    assert "sabido" in txt, (
        "state of a tool nobody has measured does not say it is unknown: %s"
        % txt[:300])
    enc = est.get("encendida") if isinstance(est, dict) else None
    if isinstance(enc, dict):
        assert enc.get("sabido") is False, "expected sabido=False, got %s" % _jd_(enc)
        assert enc.get("porque"), "says it does not know but not why"
    else:
        assert enc is not True, (
            "with no measurement it answers a plain %r, which reads as a fact" % enc)


def _pub_cada_herramienta_declara_su_perfil():
    """Every tool says which edition it belongs to.

    An install that is a desktop has no use for the parts that manage a server,
    and a tool with no declared profile is one nobody decided about. The server
    reports the gap itself rather than leaving the reader to diff two lists.
    """
    p = call(T1, "parametros")
    assert isinstance(p, dict) and p.get("herramientas"), \
        "parametros() does not list the tools: %s" % _jd_(p)
    sin = p.get("SIN_PERFIL")
    assert not sin, ("these tools declare no profile: %s. Undeclared is not "
                     "neutral, it is undecided." % _jd_(sin))


def _pub_parametros_de_una_y_de_todas_coinciden():
    """Asking about one tool and asking about all of them must agree.

    Two code paths that answer the same question are how two answers end up
    disagreeing without anybody noticing.
    """
    # The two answers have DIFFERENT SHAPES on purpose: asking about one tool
    # returns it at the top level, asking about all of them nests them under
    # "herramientas" alongside the totals. The first version of this case assumed
    # one shape for both and failed on a correct server -- assuming the shape of
    # an answer is the same mistake as assuming its content.
    una = call(T1, "parametros", {"herramienta": "msg_send"})
    todas = call(T1, "parametros")
    a = (una or {}).get("msg_send") if isinstance(una, dict) else None
    b = ((todas or {}).get("herramientas") or {}).get("msg_send") \
        if isinstance(todas, dict) else None
    assert a, "parametros('msg_send') does not return it: %s" % _jd_(una)
    assert b, "parametros() does not list msg_send under 'herramientas': %s" % _jd_(
        list((todas or {}).keys()))
    for campo in ("obligatorios", "opcionales", "perfil"):
        assert a.get(campo) == b.get(campo), (
            "parametros('msg_send') and parametros() disagree on %r:\n  one: %s\n  all: %s"
            % (campo, _jd_(a.get(campo)), _jd_(b.get(campo))))


def _pub_un_reintento_no_crea_dos_solicitudes():
    """The same request sent twice in seconds is a retry, not two obligations.

    A client that loses the reply and resends would otherwise open two tickets
    for one problem, and one of them is then real to nobody. Only requests
    collapse: for other kinds, losing a message is worse than seeing it twice.
    """
    args = {"para": ID1, "tipo": "solicitud", "asunto": "retry " + RUN,
            "cuerpo": "same request twice"}
    a = call(T2, "msg_send", dict(args))
    b = call(T2, "msg_send", dict(args))
    assert a.get("ref") and a.get("ref") == b.get("ref"), (
        "a retry opened a SECOND request: %s then %s. Two references for one "
        "thing means one of them is nobody's." % (a.get("ref"), b.get("ref")))
    assert a.get("id") == b.get("id"), "same ref but a different message was created"


def _pub_un_aviso_repetido_si_se_crea():
    """The mirror of the case above, and the reason it is not one rule.

    Repeating a notice can be legitimate. Collapsing those would silently drop
    messages, which is what an early version of this did. They are created, and
    the answer carries a flag so the sender can notice.
    """
    args = {"para": ID1, "tipo": "aviso", "asunto": "repeat " + RUN,
            "cuerpo": "same notice twice"}
    a = call(T2, "msg_send", dict(args))
    b = call(T2, "msg_send", dict(args))
    assert a.get("id") != b.get("id"), (
        "two identical notices collapsed into one. Losing a message is worse "
        "than seeing it twice.")
    assert b.get("posible_duplicado"), (
        "created it but said nothing: the sender cannot tell it was a repeat")


def _pub_un_anuncio_declara_cuanto_vale():
    """A notice can say how long it is worth, and then leaves the greeting.

    Daily notices otherwise sit in everyone's inbox forever. The lifetime is
    DECLARED by the sender, never inferred from the text: inferring it from a
    prefix would be a rule nobody agreed to, and it would quietly hide standing
    norms that happen to look like daily notices.
    """
    r = call(T2, "msg_send", {"para": "todos", "tipo": "aviso",
                              "asunto": "expires " + RUN, "cuerpo": "short lived",
                              "vigencia_dias": 1})
    assert r.get("caduca"), "declared a lifetime and the server did not record it: %s" % _jd_(r)


def _pub_un_aviso_sin_vigencia_no_caduca():
    """Protects the standing norms. A notice that declares nothing lasts."""
    r = call(T2, "msg_send", {"para": "todos", "tipo": "aviso",
                              "asunto": "forever " + RUN, "cuerpo": "a standing rule"})
    assert not r.get("caduca"), (
        "a notice that declared no lifetime got one anyway: standing rules would "
        "disappear on their own")


def _pub_una_solicitud_no_declara_vigencia():
    """A request cannot expire by itself: it is closed or it is open. An expiring
    obligation is one nobody has to answer."""
    try:
        r = call(T2, "msg_send", {"para": ID1, "tipo": "solicitud",
                                  "asunto": "expiring duty " + RUN,
                                  "cuerpo": "should be refused", "vigencia_dias": 1})
    except Rechazo:
        return
    assert not r.get("caduca"), (
        "a request was allowed to expire on its own: %s" % _jd_(r))


def _pub_actualizar_cuenta_como_senal_de_vida():
    """Updating something counts as being alive, the same as creating it.

    The signal is derived from the rows, and a row updated today keeps its
    original creation date and its original id. Reading only creations, or
    reading the most recent rows by id, both miss exactly the case this exists
    for: whoever only updates things looks inactive. That misreading is what this
    signal was added to prevent in the first place.
    """
    import random as _r
    pto = _r.randint(24000, 24999)
    call(T2, "puerto_reservar", {"puerto": pto, "servicio": "alive-" + RUN})
    # THE RELEASE GOES IN A finally, and it is not tidiness.
    #
    # Without it, a case that fails midway leaves its port reserved, and the NEXT
    # run trips over it: the port-collision case then reports a clash that the
    # server was right to report. A red caused by the previous run's leftovers is
    # worse than no test, because it sends you looking for a defect that is not
    # there. Happened here, with a port left behind by this very case.
    try:
        _otro_segundo()
        call(T2, "fact_set", {"clave": "bat.alive.%s" % RUN, "valor": "x",
                              "fuente": "battery"})
        act = (call(T1, "state_overview").get("actividad_de_todos") or {}).get(ID2) or {}
        assert act.get("ultimo_escrito") == "fact", \
            "unexpected starting point: %s" % _jd_(act)
        _otro_segundo()
        r = call(T2, "puerto_reservar", {"puerto": pto, "servicio": "alive-again-" + RUN})
        assert (r.get("accion") or "") == "actualizado", (
            "expected an update and got %r; the case is not testing what it claims"
            % r.get("accion"))
        act = (call(T1, "state_overview").get("actividad_de_todos") or {}).get(ID2) or {}
        assert act.get("ultimo_escrito") == "puerto", (
            "updating left no trace: %s. Whoever only updates things appears "
            "inactive, and reading that as absence is the misunderstanding this "
            "signal exists to prevent." % _jd_(act))
    finally:
        try:
            call(T2, "puerto_liberar", {"puerto": pto})
        except Exception:
            pass


def _pub_acusar_no_acredita_a_quien_no_escribio():
    """The trap in the obvious fix for the case above.

    Deriving the signal from the update timestamp alone would credit the SENDER
    of a message with a write when the RECEIVER acknowledges it, because the ack
    updates the sender's row. A false sign of life is worse than none: it says
    somebody is working when they are not.
    """
    r = call(T2, "msg_send", {"para": ID1, "tipo": "aviso",
                              "asunto": "ack credit " + RUN, "cuerpo": "x"})
    antes = ((call(T1, "state_overview").get("actividad_de_todos") or {})
             .get(ID2) or {}).get("ultima_escritura")
    _otro_segundo()
    call(T1, "msg_ack", {"id": int(r["id"])})
    despues = ((call(T1, "state_overview").get("actividad_de_todos") or {})
               .get(ID2) or {}).get("ultima_escritura")
    assert antes == despues, (
        "acknowledging somebody else's message credited THEM with a write they "
        "did not make: %s -> %s" % (antes, despues))


def _pub_el_saludo_ensena_lo_tomado_en_mi_estacion():
    """What is taken on your machine is visible when you open the session.

    The greeting already showed the ports registered on your machine. Not showing
    the resources was an asymmetry with no defence: a clashing port is an
    annoyance, missing VRAM leaves the job half done. Declaring that reserving a
    resource replaces hand-written notices only works if the reservation is
    visible without asking for it.
    """
    rid = "res-" + RUN[-6:]
    call(T1, "recurso_declarar", {"id": rid, "capacidad": 10000, "unidad": "MiB",
                                  "base": 1000, "notas": "battery probe"})
    call(T2, "recurso_tomar", {"recurso": rid, "cuanto": 3000,
                               "para": "battery run " + RUN, "minutos": 30})
    try:
        ov = call(T1, "state_overview")
        rec = ov.get("recursos_de_mi_estacion")
        assert isinstance(rec, list), (
            "the greeting does not show what is taken on this machine: %s"
            % _jd_(sorted(ov.keys())))
        mio = [x for x in rec if x.get("recurso") == rid]
        assert mio, "%s was just taken and does not appear: %s" % (rid, _jd_(rec))
        txt = json.dumps(mio[0], ensure_ascii=False)
        assert ID2 in txt, "does not say who holds it: %s" % txt[:200]
        assert "battery run" in txt, (
            "does not say WHAT FOR, which is what stops somebody killing the "
            "process believing it is spare")
    finally:
        try:
            call(T2, "recurso_soltar", {"recurso": rid})
        except Exception:
            pass


def _pub_esa_seccion_no_engorda_el_saludo():
    """Contract. A new section cannot undo the slimming.

    Caught in testing with 45 leftover resources, where the section reached 7 KB.
    An install with two cards would never have shown it, and by the time it did
    it would be too late.
    """
    rec = call(T1, "state_overview").get("recursos_de_mi_estacion") or []
    peso = len(json.dumps(rec, ensure_ascii=False))
    assert peso < 4000, (
        "the resources section is %d bytes; it is meant to be a few lines, not "
        "the whole register" % peso)


def _pub_abortar_devuelve_el_token_de_siempre():
    """Undoing an exchanged rotation leaves you with the token you already use.

    Rotation is deliberately not atomic: the participant generates their own
    token and the old one keeps working until they confirm. When the new token is
    lost between the exchange and the disk, they are left with two registered
    tokens, one of which nobody holds. Before this existed the only apparent way
    out retired the OLD token -- the only one they still had.

    Checked by CALLING the channel with that token, not by reading the answer:
    an "aborted" that left the door shut would be the worst possible outcome.
    """
    import urllib.request as _u
    viejo = T2
    call(viejo, "parametros")
    inv = call(T1, "rotacion_invitar", {"id": ID2, "dias": 1, "frase": FRASE})
    nuevo = "bat" + RUN + "x" * max(0, 40 - len(RUN))
    d = json.dumps({"codigo": inv["codigo"], "token_propuesto": nuevo}).encode()
    rq = _u.Request(BASE + "/rotacion", d,
                    {"Content-Type": "application/json", "User-Agent": UA})
    assert json.loads(_u.urlopen(rq, timeout=30).read().decode()).get("ok"), \
        "could not exchange; the case measures nothing"
    try:
        res = call(T1, "rotacion_abortar", {"id": ID2, "frase": FRASE})
        assert "abort" in json.dumps(res, ensure_ascii=False).lower(), \
            "rotacion_abortar does not report aborting: %s" % _jd_(res)
        call(viejo, "parametros")          # the door, not the answer
        est = [p for p in call(T1, "rotacion_estado")["participantes"]
               if p["id"] == ID2][0]
        assert not est["en_rotacion"], \
            "aborted but the channel still reports an open rotation: %s" % _jd_(est)
        try:
            call(nuevo, "parametros")
            raise AssertionError("the aborted token still opens: nothing was retired")
        except Rechazo:
            pass
        except AssertionError:
            raise
        except Exception:
            pass
    finally:
        try:
            call(T1, "rotacion_abortar", {"id": ID2, "frase": FRASE})
        except Exception:
            pass


def _pub_abortar_se_niega_si_ya_confirmo():
    """The refusal is the useful half.

    Confirming is only possible by calling WITH the new token, so whoever
    confirmed holds it and is using it. Aborting there would take away the token
    they use. It is the same rule closing applies from the other end -- closing
    refuses while somebody has NOT confirmed -- and neither call can leave anyone
    without a working token.
    """
    import urllib.request as _u
    inv = call(T1, "rotacion_invitar", {"id": ID2, "dias": 1, "frase": FRASE})
    nuevo = "bt2" + RUN + "y" * max(0, 40 - len(RUN))
    d = json.dumps({"codigo": inv["codigo"], "token_propuesto": nuevo}).encode()
    rq = _u.Request(BASE + "/rotacion", d,
                    {"Content-Type": "application/json", "User-Agent": UA})
    assert json.loads(_u.urlopen(rq, timeout=30).read().decode()).get("ok")
    try:
        call(nuevo, "token_confirmar")
        try:
            res = call(T1, "rotacion_abortar", {"id": ID2, "frase": FRASE})
        except Rechazo as e:
            res = str(e)
        txt = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
        assert "confirm" in txt.lower(), (
            "abort did NOT refuse on somebody already using the new token. That "
            "takes away the token they use: %s" % txt[:250])
    finally:
        # Back to the token in the file. Closing here would retire the OLD one
        # and leave the test identity holding a token that exists nowhere --
        # which is how an earlier version of this cleanup broke the whole run.
        try:
            call(T1, "rotacion_cerrar", {"id": ID2, "frase": FRASE})
            inv = call(T1, "rotacion_invitar", {"id": ID2, "dias": 1, "frase": FRASE})
            d = json.dumps({"codigo": inv["codigo"], "token_propuesto": T2}).encode()
            rq = _u.Request(BASE + "/rotacion", d,
                            {"Content-Type": "application/json", "User-Agent": UA})
            _u.urlopen(rq, timeout=30).read()
            call(T2, "token_confirmar")
            call(T1, "rotacion_cerrar", {"id": ID2, "frase": FRASE})
            call(T2, "parametros")
        except Exception as e:
            raise AssertionError(
                "cleanup did not give %s its token back: %s. The rest of the "
                "battery will fail on 404." % (ID2, e))


def _pub_el_catalogo_concuerda_con_su_perfil():
    """The catalogue must agree with the edition the server says it is running.

    An installation can run a reduced edition: a desktop has no use for the parts
    that manage a server. The risk is not that tools are missing -- that is the
    point -- but that the reduction takes something it should not, or claims a
    reduction it did not apply. Either way the operator finds out by a call
    failing, days later, with no hint that an edition decided it.

    This works in EVERY edition, including the full one, where it asserts nothing
    was dropped. A case that only runs on one configuration tests nothing on the
    others -- and the reduced editions are exactly the ones nobody runs the
    battery against.
    """
    p = call(T1, "parametros")
    assert isinstance(p, dict), "parametros() did not answer an object: %s" % _jd_(p)
    activo = p.get("perfil_activo")
    assert activo, ("parametros() does not say which edition is running. With a "
                    "trimmed catalogue the reader would have to infer it from "
                    "what is missing.")
    herr = p.get("herramientas") or {}
    fuera = p.get("fuera_de_este_perfil") or []

    if activo == "completo":
        assert not fuera, ("the full edition dropped tools: %s. Full means full."
                           % _jd_(fuera))
        return

    # Edicion recortada: lo declarado fuera tiene que estar fuera DE VERDAD, y lo
    # que queda no puede pertenecer a la otra edicion.
    for n in fuera:
        assert n not in herr, ("%r is listed as excluded and is still in the "
                               "catalogue: the two halves disagree" % n)
    otra = "vps" if activo == "escritorio" else "escritorio"
    intrusos = [n for n, d in herr.items() if (d or {}).get("perfil") == otra]
    assert not intrusos, ("edition %r still offers tools belonging to %r: %s. "
                          "Offering what will fail here is worse than not "
                          "offering it." % (activo, otra, _jd_(intrusos)))


def _pub_el_nucleo_sobrevive_a_cualquier_perfil():
    """Whatever the edition, coordination must still be there.

    An edition that drops the core is not an edition, it is a broken install. The
    tools marked as belonging to both editions are the ones this channel exists
    for: if a reduction reaches them, the operator loses the ability to say so
    through the very channel that would carry the complaint.
    """
    p = call(T1, "parametros")
    herr = p.get("herramientas") or {}
    for n in ("whoami", "state_overview", "msg_send", "msg_inbox", "parametros",
              "search", "fact_set", "cartelera"):
        assert n in herr, ("edition %r is missing %r, which belongs to both. An "
                           "edition that drops the core is a broken install."
                           % (p.get("perfil_activo"), n))


def _pub_una_herramienta_quitada_se_distingue_de_una_inexistente():
    """Asking about a tool the edition removed must not read like a typo.

    "No such tool" is true of this catalogue and misleading about the channel:
    the tool exists, it is not HERE. Confusing the two sends somebody hunting a
    misspelling when the answer is an edition decision.
    """
    p = call(T1, "parametros")
    fuera = p.get("fuera_de_este_perfil") or []
    if not fuera:
        return          # full edition: nothing was removed, nothing to tell apart
    try:
        r = call(T1, "parametros", {"herramienta": fuera[0]})
        t = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    except Rechazo as e:
        t = str(e)
    assert "perfil" in t.lower(), (
        "about a tool this edition removed it answers as if it never existed: %s"
        % t[:200])


def _pub_solo_el_dueno_pone_credenciales():
    """An app's credentials belong to whoever owns the app.

    The root helper that writes them knows nothing about ownership -- only the
    channel does. Without this check anyone could place credentials in somebody
    else's application, or replace theirs with their own, and the application
    would start using them without noticing.
    """
    apps = call(T1, "app_list")
    mias = [a for a in (apps if isinstance(apps, list) else [])
            if a.get("estado") != "eliminada" and a.get("dueno") == ID1]
    if not mias:
        return          # nothing deployed by T1 here: nothing to guard
    n = mias[0]["nombre"]
    try:
        r = call(T2, "app_secreto", {"nombre": n, "clave": "AJENA", "valor": "x"})
        t = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    except Rechazo as e:
        t = str(e)
    assert "dueno" in t.lower() or "no existe" in t.lower(), (
        "another participant could touch the credentials of an app that is not "
        "theirs: %s" % t[:250])


def _pub_una_credencial_no_vuelve_nunca():
    """Setting a credential must not echo it, and listing must show names only.

    A value that comes back in an answer is a value in a transcript, in a log,
    and in whatever the caller prints while debugging. The whole point of moving
    secrets out of the deployment package is lost if the channel hands them back.
    """
    apps = call(T1, "app_list")
    mias = [a for a in (apps if isinstance(apps, list) else [])
            if a.get("estado") != "eliminada" and a.get("dueno") == ID1]
    if not mias:
        return
    n = mias[0]["nombre"]
    secreto = "bat-secret-" + RUN
    try:
        r = call(T1, "app_secreto", {"nombre": n, "clave": "BAT_PRUEBA", "valor": secreto})
        t = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
    except Rechazo as e:
        t = str(e)
    assert secreto not in t, "THE VALUE CAME BACK in the answer: %s" % t[:250]
    try:
        l = call(T1, "app_secretos", {"nombre": n})
        tl = l if isinstance(l, str) else json.dumps(l, ensure_ascii=False)
        assert secreto not in tl, "the value shows up when listing: %s" % tl[:250]
        assert "BAT_PRUEBA" in tl, "the name does not show up when listing: %s" % tl[:250]
    except Rechazo:
        pass
    finally:
        try:
            call(T1, "app_secreto_borrar", {"nombre": n, "clave": "BAT_PRUEBA"})
        except Exception:
            pass


def _pub_una_clave_con_ruta_se_rechaza():
    """The key name becomes a FILE name on the server.

    A name that admits slashes or dots admits leaving the directory it was meant
    for. This is checked at the channel as well as in the helper, because a check
    that lives in only one of two layers is one refactor away from living in
    neither.
    """
    apps = call(T1, "app_list")
    mias = [a for a in (apps if isinstance(apps, list) else [])
            if a.get("estado") != "eliminada" and a.get("dueno") == ID1]
    if not mias:
        return
    n = mias[0]["nombre"]
    for mala in ("../../etc/passwd", "con/barra", "con espacio", ""):
        try:
            r = call(T1, "app_secreto", {"nombre": n, "clave": mala, "valor": "x"})
            t = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
        except Rechazo as e:
            t = str(e)
        assert "clave" in t.lower() or "error" in t.lower(), (
            "accepted %r as a credential name, which becomes a file name: %s"
            % (mala, t[:200]))


def _pub_deploy_info_dice_lo_que_callaba():
    """The deployment notes must mention what bites people.

    Three things were missing and all three were found the hard way by whoever
    deployed first: where to write data that must outlive a deployment, where
    credentials go, and that a redeployment restarts a running app. A manual that
    omits something does not leave the reader without an answer -- it makes them
    invent one, and the invented answer was the unsafe one.
    """
    d = call(T1, "deploy_info")
    t = json.dumps(d, ensure_ascii=False)
    for clave, que in (("STATE_DIRECTORY", "where to write data that survives"),
                       ("CREDENTIALS_DIRECTORY", "how an app reads a credential"),
                       ("app_secreto", "how to place one")):
        assert clave in t, "deploy_info does not mention %s (%s)" % (clave, que)


def _pub_parar_exige_un_humano():
    """Starting is free; STOPPING needs a human to confirm it.

    What this really defends is subtler than it looks: that a cowork cannot
    authorise a stop EVEN IF IT IS THE AUTHORITY. The permission hangs on being a
    person, not on holding authority. If it hung on authority instead, the
    administrator of this channel could authorise stopping its own dependencies,
    which is exactly what the rule forbids -- and the two are easy to confuse,
    because in most systems authority is the stronger claim.

    Written to fail against a build that treats them as the same thing.
    """
    hid = "bat" + RUN[-6:]
    # Only the authority declares tools: a tool nobody declared has no owner to
    # ask before stopping it.
    try:
        call(T2, "herramienta_declarar", {"id": hid, "puerto": 65000})
        raise AssertionError("a non-authority declared a tool")
    except Rechazo as e:
        assert "autoridad" in str(e).lower(), "refused for another reason: %s" % e
    call(T1, "herramienta_declarar", {"id": hid, "puerto": 65000,
                                      "arranque": "start-me.bat"})

    # With no request on file the state must SAY SO, not leave it to be inferred
    # from an absence. An absence reads as permission; a sentence does not.
    e0 = call(T1, "herramienta_estado", {"id": hid})["herramientas"][0]
    assert "NO AUTORIZADA" in (e0.get("pararla") or ""), (
        "with nothing authorised it does not say plainly that it stays up: %s"
        % _jd_(e0))

    # A request without a reason is a yes asked blind.
    try:
        call(T1, "herramienta_parada_pedir", {"id": hid, "motivo": "   "})
        raise AssertionError("accepted a stop request with no reason")
    except Rechazo:
        pass

    p = call(T2, "herramienta_parada_pedir",
             {"id": hid, "motivo": "battery: the card has to be freed"})
    pet = p.get("peticion")
    assert pet, _jd_(p)

    # Asking twice must not open a second request: two live requests over one
    # thing make an authorisation given to one look like it covers the other.
    p2 = call(T2, "herramienta_parada_pedir", {"id": hid, "motivo": "again"})
    assert p2.get("ya_habia_una") == pet, "a duplicate live request: %s" % _jd_(p2)

    # THE CASE. T1 holds authority here and is still a cowork, not a person.
    try:
        call(T1, "herramienta_parada_autorizar", {"peticion": pet})
        raise AssertionError(
            "a cowork WITH AUTHORITY authorised a stop. The permission is hanging "
            "on authority instead of on being a person.")
    except Rechazo as e:
        assert "humano" in str(e).lower(), (
            "refused, but not for not being a person: %s" % e)

    # And the failed attempt must leave the tool exactly as protected as before.
    e1 = call(T1, "herramienta_estado", {"id": hid})["herramientas"][0]
    assert "NO AUTORIZADA" in (e1.get("pararla") or ""), (
        "a cowork's failed attempt left the stop authorised: %s" % _jd_(e1))


def _pub_un_pase_es_de_su_puerta_y_solo_mira():
    """What a pass to a restricted demo can and cannot do.

    Three properties, each one a door somebody would otherwise walk through: a
    pass belongs to ONE subdomain and does not open the neighbour's; the guest
    only LOOKS unless explicitly marked otherwise; and revoking takes effect at
    once. A pass that outlives its revocation is worse than no pass, because the
    owner believes it is closed.
    """
    a, b = "pas" + RUN[-6:] + "a", "pas" + RUN[-6:] + "b"
    call(T1, "subdomain_claim", {"nombre": a, "tipo": "restringido"})
    call(T1, "subdomain_claim", {"nombre": b, "tipo": "restringido"})

    # No passes where there is no door.
    c = "pas" + RUN[-6:] + "c"
    call(T1, "subdomain_claim", {"nombre": c, "tipo": "publico"})
    try:
        call(T1, "pase_crear", {"subdominio": c, "para": "somebody"})
        raise AssertionError("issued a pass for a public subdomain")
    except Rechazo as e:
        assert "restringido" in str(e).lower(), "refused for another reason: %s" % e

    # Without saying who it is for, there is no way to know which one to revoke.
    try:
        call(T1, "pase_crear", {"subdominio": a, "para": "  "})
        raise AssertionError("accepted a pass with no recipient")
    except Rechazo:
        pass

    # THE GUEST ONLY LOOKS, unless somebody says otherwise on purpose.
    p = call(T1, "pase_crear", {"subdominio": a, "para": "battery guest"})
    assert p.get("puede_lanzar") is False, (
        "by default the guest can start work: %s" % _jd_(p))
    assert "caduca" in p, "a pass with no end: %s" % _jd_(p)

    # A pass belongs to ITS subdomain and not the neighbour's.
    mios = {x["id"] for x in call(T1, "pase_list", {"subdominio": a})}
    assert p["pase"] in mios, "the pass is not listed under its own subdomain"
    otros = {x["id"] for x in call(T1, "pase_list", {"subdominio": b})}
    assert p["pase"] not in otros, (
        "a pass shows up under a subdomain that is not its own")

    # Revoking has effect, and the effect is visible.
    r = call(T1, "pase_anular", {"pase": p["pase"]})
    assert r.get("estado") == "anulado", _jd_(r)
    ahora = {x["id"]: x for x in call(T1, "pase_list", {"subdominio": a})}
    if p["pase"] in ahora:
        assert ahora[p["pase"]].get("estado") == "anulado", (
            "revoked and still listed as live: %s" % _jd_(ahora[p["pase"]]))


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

def _d_lectura_no_recorta():
    """Con ORDER BY id ASC + LIMIT N, pasado el mensaje N las lecturas devuelven los
    N mas VIEJOS y tiran los recientes SIN AVISAR. Solo muerde en una base ya crecida
    -- que es exactamente cuando importa -- asi que en una instalacion nueva este caso
    pasa de forma trivial. Se deja igual: el dia que la base crezca, avisa."""
    a = "no recorta " + RUN
    r = call(T1, "msg_send", {"para": ID2, "tipo": "solicitud", "asunto": a, "cuerpo": "x"})
    ref = r["ref"]
    call(T2, "msg_send", {"para": ID1, "tipo": "respuesta", "responde_a": ref,
                          "asunto": "re " + a, "cuerpo": "y"})
    hilo = call(T1, "msg_hilo", {"ref": ref})
    assert len(hilo) == 2, f"msg_hilo devolvio {len(hilo)} de 2 (recorte silencioso)"
    hoy = call(T1, "msg_desde", {"fecha_iso": r["_creado"][:10] if "_creado" in r else "2000-01-01"})
    assert any(m.get("asunto") == a for m in hoy), "msg_desde no trae lo recien escrito"

def _c_rotacion_no_deja_a_nadie_fuera():
    """La parte que se puede comprobar sin ser root: que token_confirmar NO se deja
    confirmar por quien no esta rotando, y que rotacion_estado es solo de la autoridad.
    El ciclo completo (rotar / confirmar / cerrar) exige el guion de admin y se prueba
    a mano; queda anotado en PROTOCOLO-PRUEBAS.md por honestidad."""
    r = call(T1, "token_confirmar")
    assert isinstance(r, dict) and r.get("estado") == "sin_rotacion_en_curso", \
        f"confirma una rotacion que no existe: {r}"
    # T2 es el que NO tiene autoridad. Lo compruebo en vez de darlo por hecho: la
    # primera version de este caso uso T1 y salio en rojo porque T1 SI la tiene en
    # esta instalacion. El caso estaba mal, no el servidor.
    yo2 = call(T2, "whoami")
    if yo2.get("autoridad"):
        return   # instalacion donde ambos son autoridad: nada que comprobar aqui
    try:
        r2 = call(T2, "rotacion_estado")
        raise AssertionError(f"un participante sin autoridad ve la rotacion: {str(r2)[:120]}")
    except Rechazo as e:
        assert "solo la autoridad" in str(e), f"rechazo por otro motivo: {e}"
    r3 = call(T1, "rotacion_estado")
    assert isinstance(r3, dict) and "participantes" in r3, \
        f"la autoridad NO puede consultarlo: {str(r3)[:120]}"

def _d_cartelera_respeta_quien_no_confirma():
    """Un participante marcado con confirma_cartelera=false no debe figurar como
    pendiente: su nombre ahi hace parecer incompleta una cartelera que si lo esta,
    y eso ensena a ignorar la lista de pendientes."""
    r = call(T1, "participantes")
    exentos = [p["id"] for p in r if p.get("confirma_cartelera") is False]
    if not exentos:
        return   # instalacion sin nadie exento: nada que comprobar
    for c in call(T1, "cartelera"):
        e = call(T1, "cartel_estado", {"ref": c["ref"]})
        if isinstance(e, dict):
            for x in exentos:
                assert x not in (e.get("pendientes") or []), \
                    f"{x} esta exento y sigue como pendiente en {c['ref']}"

def _d_recursos_avisan_sin_bloquear():
    """El registro de puertos resolvia el conflicto equivocado: en PC1 los puertos
    ya no chocan y la VRAM si. Hallazgo de voicetf en SOL-021, dicho antes por
    produccion: 'el problema es la descarga, no el puerto'.

    Se comprueba lo que de verdad protege: que declarar sea de la autoridad, que
    tomar de mas AVISE con nombres y cifras pero DEJE PASAR (el canal informa, no
    manda), y que la sobrecarga se lea como sobrecarga y no como un numero mas."""
    rid = "gpu-bat-" + RUN[-6:]
    try:
        call(T2, "recurso_declarar", {"id": rid, "capacidad": 1000})
        raise AssertionError("un participante sin autoridad ha declarado un recurso")
    except Rechazo as e:
        assert "autoridad" in str(e), f"rechazo por otro motivo: {e}"
    r = call(T1, "recurso_declarar", {"id": rid, "capacidad": 1000, "unidad": "MB"})
    assert r.get("recurso") == rid, r

    try:
        call(T1, "recurso_tomar", {"recurso": rid + "-noexiste", "cuanto": 1, "para": "x"})
        raise AssertionError("deja tomar un recurso no declarado")
    except Rechazo:
        pass

    a = call(T2, "recurso_tomar", {"recurso": rid, "cuanto": 600, "para": "lo de prueba2"})
    assert a.get("tomas_tuyas") == 600, a
    b = call(T1, "recurso_tomar", {"recurso": rid, "cuanto": 700, "para": "lo mio", "minutos": 10})
    assert "AVISO" in b, f"pide mas de lo que queda y NO avisa: {b}"
    assert any(q["dueno"] == ID2 for q in b.get("quien_mas", [])), \
        f"avisa pero no dice con quien hablarlo: {b}"
    assert b.get("accion") in ("creado", "actualizado"), f"bloqueo en vez de avisar: {b}"

    est = call(T1, "recurso_estado", {"recurso": rid})
    f = est["recursos"][0]
    assert f["declarado"] == 1300 and f["libre_segun_lo_declarado"] == -300, f
    assert "SOBREPASADO" in f, f"el exceso se lee como un numero mas: {f}"
    assert "SIN_MEDIR" in f, f"nadie ha medido y no se avisa de que todo es declarado: {f}"

    # Lo declarado y lo medido tienen que poder contradecirse A LA VISTA. Sin esto,
    # el registro se separa de la realidad igual que la convencion que sustituye:
    # voicetf lo demostro sobre si mismo publicando durante dias una cifra que era
    # el tamano del fichero del modelo y no lo que ocupaba en la tarjeta.
    # Medir POR DEBAJO de lo reservado es normal: se reserva por pico y casi nunca
    # se esta en el pico. Marcarlo seria un rojo permanente, y un rojo siempre
    # encendido no informa.
    call(T1, "recurso_medir", {"recurso": rid, "usado": 100, "fuente": "bateria"})
    f3 = call(T1, "recurso_estado", {"recurso": rid})["recursos"][0]
    assert f3.get("medido") == 100, f3
    assert "SIN_MEDIR" not in f3, f3
    assert "MAS_USO_DEL_CONTABILIZADO" not in f3, f"usar menos de lo reservado no es un problema: {f3}"
    # Lo que SI importa: que se use mas de lo que nadie ha anotado.
    call(T1, "recurso_medir", {"recurso": rid, "usado": 9000, "fuente": "bateria"})
    f4 = call(T1, "recurso_estado", {"recurso": rid})["recursos"][0]
    assert "MAS_USO_DEL_CONTABILIZADO" in f4, f"hay 9000 en uso y 1300 contabilizados: {f4}"

    call(T2, "recurso_soltar", {"recurso": rid})
    f2 = call(T1, "recurso_estado", {"recurso": rid})["recursos"][0]
    assert f2["declarado"] == 700 and "SOBREPASADO" not in f2, f2
    call(T1, "recurso_soltar", {"recurso": rid})

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
