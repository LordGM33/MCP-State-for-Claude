# Alcances y límites

Escrito con la doctrina del proyecto: los límites conocidos se declaran,
no se descubren en producción.

## Alcance (lo que SÍ hace)

- Coordinación entre N participantes con identidad verificable por token:
  mensajes (5 tipos), solicitudes con ref estable y estado, decisiones
  append-only, hechos canónicos, inventario de infraestructura, búsqueda.
- Despliegue de sitios estáticos y apps dinámicas a subdominios con TLS
  automático, sin repartir credenciales SSH (HTTPS + token, límite 50 MB).
- Gestión de apps por MCP: status, logs, start/stop/restart.
- Respaldo diario consistente del SQLite con verificación de integridad y
  retención de 14 días.

## Fuera de alcance (deliberado)

- **Inferencia.** No transporta prompts a modelos remotos ni resultados de
  razonamiento como servicio. Es coordinación (R-007 del proyecto: el
  cerebro es local; condición de financiamiento, no preferencia).
- **Secretos.** No es un gestor de claves: se guardan rutas/punteros, jamás
  valores. Tampoco datos biométricos.
- **Tiempo real.** Sin websockets ni push: los clientes consultan
  (`state_overview()` al abrir sesión). Para el volumen del proyecto, basta.
- **Alta disponibilidad.** Un VPS, un SQLite. La caída se tolera: el
  protocolo de fase 1 (doble escritura con los archivos puente) y el
  respaldo diario acotan la pérdida.

## Límites conocidos (honestos, con su porqué)

1. **El token viaja en la URL.** Cómodo para clientes simples, pero las URLs
   pueden quedar en logs de proxys/historiales. Mitigado: TLS extremo a
   extremo, tokens de 48 chars rotables (`participante.py rotar`), y Caddy no
   registra rutas de state. Si el riesgo crece: migrar a header Authorization.
2. **Sin límite de tasa.** Un token comprometido puede escribir sin freno.
   Mitigado por Cloudflare delante y por la rotación; pendiente si se abre a
   terceros.
3. **Apps dinámicas con salida a red abierta.** Necesario para que un canal
   (Caín) llame fuera; implica que una app maliciosa podría hacer spam.
   Compensado: solo participantes dados de alta por el admin pueden
   desplegar, y cada app corre con DynamicUser sin privilegios y 512M/80%.
4. **Alta/rotación reinicia el servicio** (~2 s). Con clientes stateless no
   se pierde estado, solo puede fallar la petición en vuelo. Aceptado por
   simplicidad; si duele, recarga por SIGHUP.
5. **Cloudflare filtra User-Agents de librerías** (403 al `Python-urllib`
   por defecto). Todo cliente debe mandar UA propio. (gotcha conocido de Cloudflare).
6. **`cmd` de las apps es ejecución de código remoto POR DISEÑO** para
   participantes autorizados. La defensa no es impedirlo sino contenerlo
   (sandbox) y controlar el alta (solo el admin humano crea participantes).
7. **Python del sistema para las apps** (sin builds ni venvs automáticos):
   una app con dependencias debe vendorizarlas en su tar. v1 consciente.

## Decisiones de gobernanza vigentes

- Altas de participantes (coworks Y agentes): **solo el admin humano**
  (el administrador humano). Un cowork nuevo le solicita su id ANTES de usar el canal.
- Fase 1 de migración: doble escritura; los archivos puente mandan hasta una
  semana sin discrepancias entre canal y archivo.
