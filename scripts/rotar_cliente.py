# -*- coding: utf-8 -*-
"""Canjea un código de rotación y deja el token nuevo en su sitio, en PC1.

QUÉ HACE, en orden:
  1. Pide el código sin eco (no queda en el historial ni en la lista de procesos).
  2. Genera el token nuevo AQUÍ. El canal no emite ni transmite credenciales:
     solo guarda su SHA-256.
  3. Lo canjea en POST /rotacion (cuerpo, nunca en la URL, por TLS).
  4. Escribe el token en la ruta que el canal tiene registrada para ese
     participante (infra token-<id>), con copia de seguridad de la anterior.
  5. Verifica que el token nuevo abre, y llama a token_confirmar() con él.

EL TOKEN NO SE IMPRIME NUNCA. Ni en pantalla, ni en el log, ni de vuelta al chat.

Uso:  python rotar_cliente.py <id>
      python rotar_cliente.py produccion
"""
import getpass, json, os, secrets, shutil, sys, urllib.request, urllib.error, datetime

# Este guion se publica, asi que NO lleva dentro los datos de ninguna instalacion:
# apuntar por defecto al servidor de otro es un defecto, no una comodidad.
# Se configura por variables de entorno o por un fichero JSON al lado del guion
# (evastate.local.json, ignorado por git):
#     {"url": "https://state.tu-dominio.com",
#      "admin": "/ruta/al/token-de-un-participante-con-autoridad"}
def _config():
    cfg = {}
    aqui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evastate.local.json")
    if os.path.exists(aqui):
        try:
            cfg = json.load(open(aqui, encoding="utf-8"))
        except Exception as e:
            sys.exit(f"{aqui} no es un JSON valido: {e}")
    url = os.environ.get("EVASTATE_URL") or cfg.get("url")
    admin = os.environ.get("EVASTATE_ADMIN") or cfg.get("admin")
    if not url or not admin:
        sys.exit("Falta configuracion. Define EVASTATE_URL y EVASTATE_ADMIN, o crea\n"
                 f"  {aqui}\n"
                 '  {"url": "https://state.tu-dominio.com", "admin": "/ruta/token-admin"}')
    return url.rstrip("/"), admin


BASE, MI_TOKEN = _config()
UA = os.environ.get("EVASTATE_UA", "eva-state-cliente/1.0")


def mcp(token, tool, args=None):
    p = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": tool, "arguments": args or {}}}
    r = urllib.request.Request(f"{BASE}/{token}/mcp", json.dumps(p).encode(),
        {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream", "User-Agent": UA})
    b = urllib.request.urlopen(r, timeout=30).read().decode()
    for l in b.splitlines():
        if l.startswith("data: "):
            b = l[6:]; break
    d = json.loads(b)
    c = d.get("result", {}).get("content", [{}])[0].get("text", "")
    try:
        return json.loads(c)
    except Exception:
        return c


def main():
    if len(sys.argv) < 2:
        sys.exit("uso: python rotar_cliente.py <id-del-participante>")
    pid = sys.argv[1].strip().lower()

    # ── dónde vive su token, según el canal (no según mi memoria) ─────────────
    mi = open(MI_TOKEN, encoding="utf-8").read().strip()
    fichas = [i for i in mcp(mi, "infra_list") if i.get("id") == f"token-{pid}"]
    if not fichas:
        sys.exit(f"El canal no tiene registrada la ruta del token de '{pid}'.\n"
                 f"Regístrala antes con infra_put id=token-{pid}, o la rotación\n"
                 f"dejará el token nuevo en el aire.")
    f = fichas[0]
    ruta = f.get("ruta")
    ruta2 = f.get("ruta_2")
    print(f"  participante : {pid}")
    print(f"  su token vive: {ruta}")
    if ruta2:
        print(f"  Y TAMBIEN EN: {ruta2}")
        print("  <<< OJO: son DOS sitios. Este guion solo puede escribir el primero.")
    if not ruta or not os.path.isabs(ruta):
        sys.exit(f"La ruta registrada no es utilizable desde aquí: {ruta!r}")
    if not os.path.exists(ruta):
        sys.exit(f"No existe el fichero {ruta}. Compruébalo antes de rotar.")

    print()
    codigo = getpass.getpass(
        "Pega el CODIGO de rotacion de 24 caracteres (no tu frase; no se vera): ").strip()
    if not codigo:
        sys.exit("No escribiste nada.")
    # El error simetrico: pegar la frase donde va el codigo. Da 403 "codigo no
    # valido", que manda a mirar el codigo en vez de lo que se esta pegando.
    if " " in codigo or len(codigo) != 24:
        sys.exit(f"Eso no tiene forma de codigo de rotacion ({len(codigo)} caracteres"
                 + (", con espacios" if " " in codigo else "") + ").\n"
                 "Un codigo son 24 caracteres sin espacios. Si has pegado tu frase de\n"
                 "seguridad, no es aqui: este guion canjea un codigo, no autoriza nada.\n"
                 "No he mandado nada al canal.")

    # ── el cliente genera SU token; el servidor nunca lo emite ────────────────
    nuevo = secrets.token_urlsafe(36)

    # ESCRITURA ADELANTADA, y no es paranoia: el 2-sep el canje se completó y el
    # proceso murió antes de guardar el token. El servidor tenía registrado un
    # token vigente que no existía en ninguna parte, y cerrar la rotación habría
    # dejado al participante fuera del canal. Regla que sale de ahí: nada que solo
    # exista en memoria puede volverse autoritativo en el servidor. Primero al
    # disco, y solo después el paso irreversible.
    pend = f"{ruta}.pendiente"
    with open(pend, "w", encoding="utf-8") as fh:
        fh.write(nuevo)
    print(f"  token nuevo guardado en {os.path.basename(pend)} ANTES de canjear")

    cuerpo = json.dumps({"codigo": codigo, "token_propuesto": nuevo}).encode()
    req = urllib.request.Request(f"{BASE}/rotacion", cuerpo,
        {"Content-Type": "application/json", "User-Agent": UA})
    def abortar(msg):
        os.remove(pend)          # el canje no llegó a ocurrir: nada que conservar
        sys.exit(msg)

    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
    except urllib.error.HTTPError as e:
        detalle = e.read().decode()[:300]
        if e.code == 403:
            abortar("Código no válido: ya usado, anulado, o mal copiado.\n"
                    "Copia SOLO el código del panel, no el bloque de instrucciones.")
        if e.code == 410:
            abortar("Código caducado. Pide otro desde el panel.")
        if e.code == 409:
            abortar(f"El canal rechazó el canje: {detalle}")
        abortar(f"El canal rechazó el canje (HTTP {e.code}): {detalle}")
    except Exception as e:
        # Aquí NO se borra el pendiente: la petición pudo llegar y perderse la
        # respuesta. Si el canje ocurrió, ese fichero es el único sitio donde
        # existe el token nuevo.
        sys.exit(f"Fallo de red durante el canje: {e}\n"
                 f"NO borres {pend}: si el canje llegó a completarse, ese fichero\n"
                 f"tiene el unico ejemplar del token nuevo. Avisa antes de tocarlo.")
    if r.get("id") != pid:
        sys.exit(f"Ese código NO es de '{pid}', es de '{r.get('id')}'.\n"
                 f"El canje YA se hizo sobre '{r.get('id')}' y su token nuevo está en {pend}.")
    print("  canje aceptado por el canal" + ("  (reintento de una rotación a medias)"
                                             if r.get("reintento") else ""))

    # ── copia de seguridad ANTES de sobrescribir ─────────────────────────────
    sello = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    resp = f"{ruta}.antes-{sello}"
    shutil.copy2(ruta, resp)
    os.replace(pend, ruta)       # el pendiente pasa a ser el bueno, de una pieza
    print(f"  token nuevo en su sitio (el anterior queda en {os.path.basename(resp)})")

    # ── comprobar que abre de verdad, antes de cantar victoria ───────────────
    try:
        quien = mcp(nuevo, "whoami")
    except Exception as e:
        shutil.copy2(resp, ruta)
        sys.exit(f"El token nuevo NO abre ({e}). He restaurado el anterior; nadie se queda fuera.")
    if not isinstance(quien, dict) or quien.get("id") != pid:
        shutil.copy2(resp, ruta)
        sys.exit(f"El token nuevo devuelve una identidad rara ({quien}). Anterior restaurado.")
    print(f"  verificado: el token nuevo abre como '{pid}'")

    # ── confirmar con el nuevo (solo cuenta si llega con él) ─────────────────
    c = mcp(nuevo, "token_confirmar")
    estado = c.get("estado") if isinstance(c, dict) else str(c)[:80]
    print(f"  token_confirmar -> {estado}")

    print()
    print("  LISTO. El token ANTIGUO de este participante sigue funcionando hasta")
    print("  que cierres la rotación desde la consola (rotacion_cerrar, con la frase).")
    if ruta2:
        print()
        print(f"  PENDIENTE Y NO LO PUEDO HACER YO: '{pid}' tiene una segunda copia en")
        print(f"    {ruta2}")
        print("  Si no se propaga ahí, la rotación PARECERÁ hecha y seguirá entrando")
        print("  con el viejo. Avísale antes de cerrar.")


main()
