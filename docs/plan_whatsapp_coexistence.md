# Plan de implementación · WhatsApp Cloud API directo + Coexistence (feature/wa-cloud-api)

Spec autocontenido para migrar el `whatsapp_bot` de **Twilio** al **Cloud API de Meta, sin
intermediarios**, usando **Coexistence**: el restaurante conserva su número y su app de WhatsApp
Business, el bot contesta por API, y cuando alguien del local responde a mano desde el celular el bot
**se calla solo** con ese cliente.

Primer restaurante: **San Pablo** (`RESTAURANTE_ID=1`). Costo mensual del proveedor: **$0**.

> **Reemplaza al plan anterior con 360dialog.** Se descartó su cuota de ~€49/mes por número (~$265/mes
> con los 5 restaurantes de la meta) a cambio de asumir el trámite de Tech Provider, que es gratis en
> dinero y caro en tiempo.

---

## La idea que ordena todo el plan

El trámite con Meta tarda semanas y **no bloquea el código**. Son dos vías en paralelo:

| Vía | Quién | Cuándo |
|---|---|---|
| **A · Trámite** — Verificación de negocio → App Review → Tech Provider | Martin, a mano | Empieza **hoy**, tarda semanas |
| **B · Código** — el bot entero contra Cloud API, con el **número de prueba gratuito** de Meta | Claude Code | Empieza **hoy**, no espera a nada |

El código es **idéntico** con número de prueba y con el número real de San Pablo: mismos webhooks,
mismos endpoints, mismo payload. Solo cambian las credenciales. Lo único que no se puede probar hasta
que Meta apruebe es el Embedded Signup (P7) y la conexión del número real.

**Meta regala un número de prueba** al crear la app, que envía gratis a **hasta 5 destinatarios
verificados**. Con eso se desarrolla y se prueba P1–P6 completo.

---

## Por qué este camino

- **Meta no cobra hosting ni cuota de plataforma** por el Cloud API. El acceso es gratis.
- **Los mensajes de este bot son gratis.** Solo responde dentro de la ventana de 24 h que abre el
  cliente al escribir → son mensajes de servicio, que Meta no cobra. Cero plantillas, cero costo.
- **Twilio no soporta Coexistence**, que es el requisito que manda: conservar el número que San Pablo
  ya usa y que el dueño pueda seguir respondiendo desde su celular.
- **Coexistence exige ser Solution Partner o Tech Provider.** Ese es el peaje, y se paga en trámite.

**Efecto lateral que conviene ver:** ser Tech Provider convierte a OKU en una plataforma ante Meta.
Cada restaurante se conecta bajo tu app con su propia WABA. Es lo correcto para una flota de 5, y es
la misma disciplina «API-first» que ya aplicas en el otro proyecto — pero también significa que el
onboarding de cada cliente pasa a ser responsabilidad tuya.

---

## Contexto del sistema (leer antes de tocar nada)

- `whatsapp_bot/main.py` es un **único archivo FastAPI**. Hoy hace tres cosas:
  1. `init_db()` — **es el dueño del esquema** de toda la base (~23 tablas). Corre en cada arranque.
     Toda tabla nueva se declara aquí, aditiva: `CREATE TABLE IF NOT EXISTS` + `ALTER … ADD COLUMN
     IF NOT EXISTS`. Nunca destructiva.
  2. `POST /webhook` — valida la firma de Twilio y lanza `_enviar_bienvenida` en `BackgroundTasks`.
  3. `enviar_mensaje()` — cliente de Twilio.
- El bot **no conversa**: a cualquier mensaje responde con el saludo + enlace a la carta digital
  (`APP_CLIENTE_URL/?tel=<numero>`). Esa sigue siendo la lógica de producto; **no la cambies aquí**.
- Saludo y nombre salen de `ajustes` (`bot_saludo`, `restaurante_nombre`), con cache de 60 s y
  fallback quemado (`_branding()`). Ese patrón se reutiliza para la config nueva.
- La tabla `clientes (telefono PK, nombre, direccion, …)` ya existe y la alimenta `app_cliente`. Es
  donde aterrizan los contactos sincronizados.
- Zona horaria fijada a `America/Bogota` en el proceso y en la conexión. No tocar.
- Convenciones: commits `bot: ...` en español describiendo el efecto para el usuario; rama
  `feature/wa-cloud-api` → `staging` → `main`; verificación ligera (compilar/importar + script
  dirigido); **nunca commitear `.env`, el `APP_SECRET` ni ningún token**.

---

## Principios (no negociables)

1. **El webhook nunca falla.** Responde `200` en menos de un segundo, pase lo que pase. Todo el
   trabajo real va en `BackgroundTasks`. Un error o un timeout hace que Meta reintente, y los
   reintentos generan saludos duplicados (ya pasó con Twilio; por eso existe el patrón actual).
2. **Idempotencia por `wamid`.** Meta reintenta. Todo evento se descarta si su `id` ya se procesó.
   Misma idea que `idem_key` en `pedidos`.
3. **Aditivo en la base.** `init_db()` debe correr contra la base de San Pablo en producción sin
   romper nada.
4. **El proveedor vive detrás de un módulo.** Ni `main.py` ni el panel hablan con Graph API
   directamente.
5. **Cero credenciales en el repo.** `WA_TOKEN`, `WA_APP_SECRET` y `WA_VERIFY_TOKEN` son variables de
   Railway.
6. **Un interruptor de apagado** que no requiera redeploy.

---

## Vía A · Trámite con Meta (sin código; empieza hoy, es la ruta crítica)

### A1 · App de Meta y portafolio de negocio

- Crear una **app de Meta** con el caso de uso **WhatsApp**, conectada a un **portafolio de negocio**.
- Este portafolio es **el tuyo** (OKU como proveedor), **no** el del restaurante. Ojo con la
  diferencia: el de San Pablo se crea aparte, durante el Embedded Signup, y **debe quedar a nombre del
  restaurante** — ese no se puede cambiar después.
- Activar **2FA** en el Business Manager.

### A2 · Verificación de negocio ⚠️ ruta crítica

Nombre legal, dirección, teléfono, email y **sitio web**. Meta puede pedir documentos (cámara de
comercio, RUT). **Tarda semanas y varía por región.** Es lo primero que hay que mandar; todo lo demás
depende de esto.

### A3 · Ajustes de la app — **aquí hay una deuda que no es técnica**

App Review exige que la app tenga **ícono, política de privacidad (URL pública) y categoría**.

> [!warning] Esto te bloquea hoy
> En el estado del proyecto, **landing, logo e identidad visual figuran como "sin hacer"**. Ahora no
> son solo pendientes de marca: **bloquean el camino técnico**. Necesitas, como mínimo:
> - una URL pública con la **política de privacidad** de OKU,
> - un **ícono** de app.
>
> No hace falta la landing completa ni la identidad final. Hace falta una página que exista y un PNG.

### A4 · Los dos videos de App Review — **requisito con costo de código oculto**

Meta pide dos videos:

1. Un mensaje **creado y enviado desde tu app**, recibido en WhatsApp. → Lo cubre P1–P4 con el número
   de prueba. Gratis.
2. **Tu app siendo usada para crear una plantilla de mensaje.**

> [!warning] El segundo video obliga a construir algo que el producto no necesita
> El panel de OKU **no gestiona plantillas** — y el bot no las usa, porque solo responde dentro de la
> ventana de 24 h. Para pasar App Review hay que construir una pantalla mínima de plantillas igual.
> Está en **P8**. No es opcional y no es capricho tuyo: es peaje de Meta.

### A5 · App Review

Solicitar **Advanced access** a:

- `whatsapp_business_messaging` — enviar mensajes en nombre de los clientes.
- `whatsapp_business_management` — acceder a las WABAs de los clientes. Sin esto, las llamadas contra
  WABAs que no son tuyas devuelven error `200`.

### A6 · Conectar el número de San Pablo (cuando A5 pase)

Antes de tocar el número, el trámite del restaurante:

1. **Portafolio de Meta a nombre de San Pablo**, con nombre legal, dirección, web y teléfono.
   **No se puede cambiar después de registrar el número.**
2. **Dejar el perfil de WhatsApp Business como debe quedar** — foto, nombre visible, dirección,
   horario. **La foto no se puede cambiar después del onboarding.**
3. **App de WhatsApp Business actualizada** (mínimo 2.24.17) y el celular a mano: se escanea un QR.
4. **Apagar en la app el mensaje de bienvenida y el de ausencia.** Siguen funcionando tras conectar;
   si los dejas, el cliente recibe **dos respuestas**.
5. Avisarles de lo que cambia: listas de difusión quedan de solo lectura; se desactivan mensajes
   temporales, "ver una vez" y ubicación en tiempo real; los dispositivos vinculados se desconectan y
   hay que revincular (WhatsApp para Windows y WearOS dejan de servir). Chats, contactos y etiquetas
   siguen igual.
6. Correr el Embedded Signup (P7) y **sincronizar dentro de las 24 h** (P9).

> **Dos reglas operativas permanentes para el local:**
> **(a)** No desinstalar la app de WhatsApp Business — desconecta la cuenta.
> **(b)** Abrirla al menos **una vez cada 13 días**. Si el dispositivo primario queda inactivo ~14
> días, Meta desconecta el número y el bot muere **en silencio**.

---

## Vía B · Código

### P0 · Arreglar la comanda de los pedidos web ⚠️ bloqueante, va primero

`app_cliente.guardar_pedido()` (domicilios y para llevar de la app pública) **no encola la comanda**:

| Origen | Encola comanda al crear |
|---|---|
| POS mesa (`crear_pedido_manual`) | ✅ `enqueue_comanda` |
| POS domicilio (`crear_pedido_entrega`) | ✅ `enqueue_comanda` |
| Cliente QR de mesa (`guardar_pedido_mesa`) | ✅ `_encolar_comanda` |
| **Cliente domicilio / para llevar** (`guardar_pedido`) | ❌ **nada** |

El pedido aparece en el Monitor pero no sale papel salvo reimpresión manual desde "⋯ → 🍳 Comanda".
Hoy no se nota porque casi nadie usa ese canal — y es justo el canal al que se va a mover todo el
domicilio.

**Archivo:** `app_cliente/cliente_app.py`, `guardar_pedido` (~línea 898).

- Replicar el patrón exacto de `guardar_pedido_mesa`: bandera `creado`, encolar **fuera** del txn (un
  fallo de impresión no debe revertir la venta) y **solo en creación real** — en los reintentos
  idempotentes la comanda ya se encoló.
- Etiqueta: `🛵 Domicilio · <nombre>` / `🛍️ Para llevar · <nombre>`, coherente con `TIPO_BADGE` del
  monitor.
- Llevar `tipo_entrega`, `telefono` y `direccion` en el payload, como hace `enqueue_comanda` del
  panel, para que el repartidor tenga la dirección impresa.

**Verificación:** crear un domicilio desde la app pública → fila en `print_jobs` con
`abrir_cajon=False` y dirección en el payload. Reenviar con la misma `idem_key` → **no** se encola de
nuevo.

**Commit:** `cliente: la comanda de los pedidos a domicilio sale sola a la cocina`

---

### Modelo de datos nuevo

En `init_db()` de `whatsapp_bot/main.py`, aditivo.

```sql
-- Eventos ya procesados (anti-duplicado ante reintentos de Meta).
CREATE TABLE IF NOT EXISTS wa_eventos (
    wamid   VARCHAR(120) PRIMARY KEY,
    tipo    VARCHAR(30)  NOT NULL,      -- 'entrante' | 'echo' | 'estado' | 'cuenta' | 'contacto'
    visto   TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_eventos_visto ON wa_eventos (visto);

-- Estado del bot por contacto: hasta cuándo está pausado y cuándo saludó por última vez.
CREATE TABLE IF NOT EXISTS wa_contactos (
    telefono       VARCHAR(40) PRIMARY KEY,   -- solo dígitos, con indicativo, sin '+'
    pausado_hasta  TIMESTAMP,
    pausa_motivo   VARCHAR(30),               -- 'humano' | 'manual'
    ultimo_saludo  TIMESTAMP,
    actualizado    TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Bitácora cruda de lo recibido: sin esto, depurar en producción es a ciegas.
CREATE TABLE IF NOT EXISTS wa_log (
    id      SERIAL PRIMARY KEY,
    campo   VARCHAR(40),
    payload TEXT,
    creado  TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wa_log_creado ON wa_log (creado);
```

Ajustes nuevos (mismo `INSERT … ON CONFLICT DO NOTHING` que los existentes):

| Clave | Default | Qué hace |
|---|---|---|
| `bot_activo` | `'1'` | Interruptor de apagado sin redeploy |
| `bot_pausa_min` | `'45'` | Minutos que el bot calla tras una respuesta humana |
| `bot_saludo_cooldown_min` | `'180'` | No repetir el saludo al mismo número dentro de este lapso |

**Poda:** borrar filas de `wa_eventos` y `wa_log` de más de 30 días al final de `init_db()`. Corre en
cada arranque; es suficiente, no montes un cron.

**Variables de entorno** (`whatsapp_bot/.env.example`):

```
WA_TOKEN=              # token del system user (permanente)
WA_PHONE_NUMBER_ID=    # id del número, NO el número
WA_APP_SECRET=         # para validar X-Hub-Signature-256
WA_VERIFY_TOKEN=       # cadena inventada, para el GET de verificación
WA_API_VERSION=v23.0
```

Fuera: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`, `TWILIO_VALIDATE`, y la
dependencia `twilio` de `requirements.txt`.

---

### P1 · Módulo de proveedor (Cloud API)

**Archivo nuevo:** `whatsapp_bot/proveedor.py`. Superficie pública, y nada más:

- `enviar_texto(telefono, cuerpo) -> bool`
  `POST https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_NUMBER_ID}/messages`,
  header `Authorization: Bearer {WA_TOKEN}`:
  ```json
  {"messaging_product":"whatsapp","recipient_type":"individual",
   "to":"573001234567","type":"text","text":{"preview_url":true,"body":"…"}}
  ```
  **Ojo con el destino:** Twilio usaba `whatsapp:+57300…`; aquí es **solo dígitos, con indicativo y
  sin `+`**. Escribe `_normalizar_tel()` y úsalo en los dos sentidos. Timeout explícito (10 s), nunca
  propaga excepciones, registra y devuelve `False`.
- `parsear_webhook(payload) -> list[Evento]`
  Aplana `entry[].changes[]` y devuelve dataclasses `Evento(tipo, wamid, telefono, texto, crudo)`:

  | `field` | `tipo` | De dónde sale el teléfono |
  |---|---|---|
  | `messages` (con `messages[]`) | `entrante` | `messages[].from` |
  | `messages` (con `statuses[]`) | `estado` | `statuses[].recipient_id` |
  | `smb_message_echoes` | `echo` | `message_echoes[].to` ← **el cliente es el `to`, no el `from`** |
  | `smb_app_state_sync` | `contacto` | `state_sync[].contact.phone_number` |
  | `account_update` | `cuenta` | — |

  Cualquier `field` desconocido → lista vacía, sin reventar.

**Sin lógica de negocio.** Este módulo no sabe qué es un pedido.

**Verificación:** `pytest` con los payloads de ejemplo de la documentación de Meta (`messages` de
texto y `smb_message_echoes`) → comprobar `tipo`, `telefono` y `wamid`.

**Commit:** `bot: capa de proveedor para hablar con el Cloud API de Meta`

---

### P2 · Webhook: verificación GET y firma HMAC

Meta exige **dos cosas** que Twilio no pedía igual:

- **`GET /webhook`** — handshake de alta. Si `hub.mode == "subscribe"` y
  `hub.verify_token == WA_VERIFY_TOKEN`, devolver **`hub.challenge` como texto plano** (no JSON).
  Si no cuadra → `403`. Sin esto Meta no acepta registrar la URL.
- **`POST /webhook`** — validar `X-Hub-Signature-256`: HMAC-SHA256 del **cuerpo crudo** con
  `WA_APP_SECRET`, comparado con `hmac.compare_digest` (tiempo constante, nunca `==`).
  **Lee el cuerpo con `await request.body()` antes de parsear el JSON** — si parseas y re-serializas,
  la firma no cuadra nunca. Este es el error clásico y cuesta una tarde.
- `WA_VALIDATE` (default `"true"`) para desactivarlo en local, igual que el `TWILIO_VALIDATE` que
  reemplaza.

Borrar `_url_publica()` y todo el aparato de Twilio.

**Verificación:** `GET /webhook?hub.mode=subscribe&hub.verify_token=…&hub.challenge=123` → cuerpo
`123`, sin comillas. `POST` con firma mala → `403`.

**Commit:** `bot: webhook del Cloud API con verificación y firma de Meta`

---

### P3 · Enrutado, idempotencia y respuesta rápida

`POST /webhook` recibe **JSON** (antes era `form-urlencoded`):

```
1. si WA_VALIDATE y la firma no cuadra → 403
2. payload = json.loads(cuerpo_crudo)   (envuelto en try; JSON malo → 200, no 400)
3. background_tasks.add_task(_procesar, payload)
4. return {"status": "ok"}              ← siempre
```

`_procesar(payload)`, síncrono, en el hilo de fondo:

1. Guardar el crudo en `wa_log` (tolerante a fallos; un error de log nunca corta el flujo).
2. `eventos = proveedor.parsear_webhook(payload)`.
3. **Reclamar el `wamid`**: `INSERT INTO wa_eventos … ON CONFLICT DO NOTHING RETURNING wamid`.
   Sin fila → ya se procesó → `continue`.
4. Despachar: `entrante` → `_atender_entrante` (P4) · `echo` → `_pausar_por_humano` (P4) ·
   `contacto` → `_upsert_contacto` (P6) · `cuenta` → `_alerta_cuenta` (P6) · `estado` → nada (queda
   en `wa_log`).

**Verificación:** `curl` con un payload de `messages` y firma válida → `200` y fila en `wa_log`; el
mismo `curl` repetido → `200` sin segundo saludo.

**Commit:** `bot: procesa los webhooks sin duplicar ante reintentos`

---

### P4 · El corazón: cuándo contesta el bot y cuándo se calla

**`_pausar_por_humano(ev)`** — con cada `smb_message_echoes`, es decir cada vez que alguien del local
escribe desde el celular o desde WhatsApp Web:

```
minutos = ajuste_int('bot_pausa_min', 45)
UPSERT wa_contactos (telefono = ev.telefono)
  SET pausado_hasta = NOW() + minutos, pausa_motivo = 'humano', actualizado = NOW()
```

Nada más. No responde, no notifica. El humano ya está en la conversación.

**`_atender_entrante(ev)`** — puertas en este orden; la primera que cierre corta:

1. `bot_activo != '1'` → no responder. (Interruptor de apagado.)
2. `wa_contactos.pausado_hasta > NOW()` → no responder. **Aquí el bot respeta al humano.**
3. `ultimo_saludo` dentro de `bot_saludo_cooldown_min` → no responder. Sin esto el bot manda el mismo
   enlace cinco veces a quien escribe cinco mensajes cortos, que es como escribe la gente en WhatsApp.
4. Enviar el saludo con `proveedor.enviar_texto(...)`, reusando `mensaje_bienvenida()` y `_branding()`
   tal como están.
5. Si el envío devolvió `True` → `ultimo_saludo = NOW()`. **Si devolvió `False`, no marcar**, para que
   el próximo mensaje reintente.

**Reloj:** `NOW()` de Postgres en todas las comparaciones, nunca `datetime.now()` de Python. La
conexión ya está en `America/Bogota`.

**Verificación con el número de prueba** (añade tu celular y el del dueño a los 5 destinatarios
verificados):

1. Escribes → el bot responde el enlace.
2. Respondes desde el otro teléfono simulando al dueño (**esto solo se puede probar de verdad con
   Coexistence activa**; mientras tanto, dispara `_pausar_por_humano` a mano desde un script).
3. Escribes otra vez → el bot calla. Fila en `wa_contactos` con `pausado_hasta` futuro.
4. `bot_pausa_min = 1`, esperas dos minutos, escribes → vuelve a responder.

**Commit:** `bot: se calla cuando el restaurante responde a mano y se reactiva solo`

---

### P5 · Ajustes en el panel

**Archivo:** `dashboard_admin/views/menu.py`, pestaña **⚙️ Ajustes** (donde ya vive `bot_saludo`).
Mismo patrón de lectura/escritura sobre `ajustes` que los controles de al lado:

- **Interruptor** "🤖 El bot responde automáticamente" → `bot_activo`.
- **Slider** "Minutos que el bot espera tras una respuesta manual" (15–180, default 45).
- **Slider** "No repetir el saludo antes de" (30–480 min, default 180).

Textos en tono simple. Nada de "webhook", "coexistence" ni "API" en la UI: esto lo lee el dueño del
restaurante.

*Opcional si sobra tiempo:* tabla de contactos pausados ahora (`pausado_hasta > NOW()`) con botón
"Reactivar el bot". Útil, no bloqueante.

**Commit:** `panel: controles del bot de WhatsApp en Ajustes`

---

### P6 · Contactos sincronizados y alerta de desconexión

**`_upsert_contacto(ev)`** — de `smb_app_state_sync`:

- `action = "add"` → `UPSERT` en la tabla **`clientes`** existente: `telefono` normalizado como PK,
  `nombre` = `full_name`. **No tocar `direccion`** — la que está ahí la escribió el propio cliente al
  pedir y es mejor dato que la agenda del celular.
- `action = "remove"` → **no borrar la fila.** Ese cliente puede tener pedidos históricos.
- Efecto lateral bueno: `_pantalla_gate()` de `app_cliente` ya busca por teléfono con
  `buscar_cliente()`, así que los clientes de la agenda entran a la carta con el nombre precargado.

**`_alerta_cuenta(ev)`** — de `account_update`. `PARTNER_REMOVED` y `ACCOUNT_OFFBOARDED` significan
**que el número se desconectó y el bot dejó de funcionar**. Causas típicas: `PRIMARY_INACTIVITY`
(nadie abrió la app en 14 días), `CHANGE_NUMBER`, `USER_RE_REGISTERED`.

- Registrar en `wa_log` con el `reason` visible.
- Alertar por el canal de Telegram que ya usa `scripts/monitor_salud.py`. **Extrae la función de
  envío a un helper compartido** en vez de duplicar el código del script.
- Es tan importante como la alerta de "dejó de imprimir": el restaurante no se entera solo.

**Commit:** `bot: sincroniza contactos y avisa si el número se desconecta`

---

### P7 · Embedded Signup (solo tras aprobar App Review)

Es lo único que exige código de front y no se puede probar antes de A5.

**Dónde ponerlo.** `dashboard_admin` es Streamlit y no se lleva bien con el SDK JS de Facebook. **No
pelees con Streamlit**: sirve una **página HTML estática desde el propio `whatsapp_bot`** (ya es
FastAPI): `GET /onboarding`, protegida por una clave de un solo uso. Es una página que vas a usar
cinco veces en tu vida, una por restaurante.

- SDK JS de Facebook con `FB.login`, `config_id` de tu configuración de Embedded Signup y:
  ```js
  { "config_id": "<CONFIGURATION_ID>", "response_type": "code",
    "override_default_response_type": true,
    "extras": { "setup": {}, "featureType": "whatsapp_business_app_onboarding",
                "sessionInfoVersion": "3" } }
  ```
  `featureType` es lo que activa Coexistence. **Sin él sale el flujo normal y el restaurante pierde su
  historial.** Usa Embedded Signup **con session logging** (requisito de Meta).
- Verificar que quedó bien: la pantalla de selección de WABA debe estar reemplazada por *"Connect a
  WhatsApp Business App"*.
- Al terminar, el evento devuelve `event: "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"` con `waba_id` y
  un `code` intercambiable.
- **`POST /onboarding/callback`** — intercambia el `code` por token, suscribe tu app a la WABA,
  **salta el registro del número** (ya está registrado) y guarda `phone_number_id` y `waba_id`.
- Comprobar el estado: `GET /{phone_number_id}?fields=is_on_biz_app,platform_type` debe devolver
  `is_on_biz_app: true` y `platform_type: "CLOUD_API"`.
- Suscribir la WABA a los campos: `messages`, `smb_message_echoes`, `smb_app_state_sync`, `history`,
  `account_update`.
- **Token permanente:** usa un **system user** del Business Manager, no un token de usuario. Los de
  usuario caducan y el bot se cae un martes cualquiera sin explicación.

**Commit:** `bot: onboarding de restaurantes con Embedded Signup y coexistencia`

---

### P8 · Pantalla mínima de plantillas (peaje de App Review)

**No la necesita el producto.** La necesita el segundo video de A4: *"tu app siendo usada para crear
una plantilla de mensaje"*.

**Archivo:** `dashboard_admin/views/` — vista nueva, **solo rol `admin`**, oculta para caja y meseros.

- Listar plantillas: `GET /{waba_id}/message_templates`.
- Crear una: `POST /{waba_id}/message_templates` con nombre, idioma (`es`), categoría (`UTILITY`) y
  cuerpo.
- Mostrar el estado que devuelve Meta (`APPROVED` / `PENDING` / `REJECTED`).

Eso es todo. No construyas un editor de plantillas con variables, botones y previsualización: no lo
vas a usar. **Grábale el video en cuanto funcione.**

Si algún día quieres avisar "tu pedido va en camino" fuera de la ventana de 24 h, esta pantalla ya
estará — pero eso es otra decisión y tiene costo por mensaje.

**Commit:** `panel: gestión mínima de plantillas de WhatsApp`

---

### P9 · Historial y contactos (una sola vez, dentro de 24 h)

Tras el onboarding tienes **24 horas** para sincronizar, o hay que desconectar al cliente y repetir el
flujo entero. **Cada llamada se puede hacer una sola vez.**

- `POST /{phone_number_id}/smb_app_data` con `{"sync_type": "smb_app_state_sync"}` → contactos, que
  llegan por webhook y entran por P6.
- `POST /{phone_number_id}/smb_app_data` con `{"sync_type": "history"}` → historial de 180 días en
  webhooks `history`, por fases (0/1/2) y trozos (`chunk_order`, `progress`).
- Dile al restaurante que **deje la app abierta** durante la sincronización.
- Un solo webhook puede describir **miles** de mensajes: captura el cuerpo primero y procésalo
  después, nunca en línea.
- Si el negocio no compartió el historial, llega un `history` con error `2593109`. No es un fallo tuyo.

> [!question] Sin decidir: qué haces con el historial
> Son conversaciones personales de los clientes de San Pablo y **hoy ninguna función del producto las
> usa**. Guardar datos personales que no ocupas es pasivo, no activo. Sugerencia: sincronizar
> contactos sí (alimentan `clientes` y mejoran la carta), y volcar el historial a una tabla `wa_historial`
> cruda **solo si vas a construir la bandeja de entrada en el panel**. Decídelo antes de P7, porque
> después no hay segunda oportunidad.

**Commit:** `bot: sincronización inicial de contactos e historial`

---

### P10 · Documentación

- `docs/REPLICAR.md` — la sección **"4. Twilio (WhatsApp)"** queda obsoleta. Reescribir como
  "4. WhatsApp (Cloud API + Coexistence)" con el trámite de la Vía A resumido y las dos reglas
  operativas permanentes. Variables nuevas de `whatsapp_bot`.
- `docs/SOPORTE.md` — runbook *"el bot no responde"*: (1) ¿`bot_activo` encendido? (2) ¿el contacto
  está pausado en `wa_contactos`? (3) ¿hay `account_update` reciente en `wa_log`? (4) ¿alguien abrió
  la app en los últimos 13 días? (5) ¿caducó el token?
- `docs/ARQUITECTURA.md` — cambiar "bot vía Twilio" por Cloud API y añadir las tablas `wa_*`.

**Commit:** `infra: documenta la migración de WhatsApp al Cloud API de Meta`

---

## Orden de ejecución

1. **Hoy, en paralelo:** arrancar **A2** (verificación de negocio, es la ruta crítica) y empezar
   **P0**.
2. P0 a `main` y deploy. Los domicilios web ya imprimen. *Solo entonces* sigue lo demás.
3. **A1 + A3** mientras tanto: la app de Meta, el ícono y la política de privacidad. A3 depende de que
   exista una URL pública — si no la hay, **es tu bloqueante real, no el código**.
4. P1–P6 contra el **número de prueba**, en `staging`. Aquí ya tienes un bot funcionando.
5. P8 y grabar los dos videos → **A4/A5**, mandar App Review.
6. Aprobado: **A6** (trámite del restaurante) → P7 → P9, todo el mismo día, por la ventana de 24 h.
7. Deploy con `bot_activo = '0'`. Escribes al número: no debe pasar nada, pero **debe aparecer la fila
   en `wa_log`**. Eso confirma que el webhook llega y la firma valida.
8. `bot_activo = '1'`. Ciclo completo de P4 con el dueño presente.
9. El primer día, mira `wa_log` un par de veces. Es la única forma de ver lo que no anticipaste.

---

## Cosas que Claude Code te va a preguntar, y qué responder

| Pregunta probable | Respuesta |
|---|---|
| "¿Mantengo Twilio en paralelo por si acaso?" | **No.** Se borra. Twilio no soporta Coexistence; no hay vuelta atrás que valga la pena mantener. |
| "¿Uso el SDK oficial de Meta / `pywa` / similar?" | **No.** `requests` contra Graph API. Son tres endpoints; un SDK es dependencia sin beneficio. |
| "¿Parseo el JSON y luego valido la firma?" | **No.** Firma sobre el **cuerpo crudo**, antes de parsear. Es el error clásico. |
| "¿El `hub.challenge` lo devuelvo como JSON?" | **No.** Texto plano. Con comillas, Meta rechaza la URL. |
| "¿Token de usuario o de system user?" | **System user.** Los de usuario caducan y el bot se cae sin aviso. |
| "¿Monto el Embedded Signup dentro de Streamlit?" | **No.** Página HTML estática servida por el FastAPI del bot. No pelees con Streamlit por algo que usarás cinco veces. |
| "¿Construyo un editor de plantillas completo?" | **No.** Lo mínimo para grabar el video de App Review (P8). Listar y crear, nada más. |
| "¿Guardo el historial de chat para dar contexto al bot?" | **No.** El bot no conversa: manda un enlace. No inventes una máquina de estados; se quitó a propósito. |
| "¿Hago que el bot avise cuando el pedido esté listo?" | **No en este plan.** Sale de la ventana de 24 h → plantilla de utilidad y costo por mensaje. Otra decisión. |
| "¿Estado de pausa en Redis o en memoria?" | **Postgres.** Railway reinicia el servicio y la memoria se pierde; además el panel tiene que leerlo. |
| "¿`datetime.now()` para los vencimientos?" | **No.** `NOW()` de Postgres. |
| "¿Creo un `CLAUDE.md` o README en `whatsapp_bot/`?" | **No.** La documentación va en `docs/`. |

---

## Riesgos conocidos

- **La ruta crítica no es el código, es la verificación de negocio.** Tarda semanas y varía por
  región. Si no empieza hoy, nada de lo demás importa.
- **La deuda de marca bloquea el trámite.** Sin política de privacidad publicada ni ícono, App Review
  no arranca. Es la primera vez que "landing e identidad sin hacer" tiene consecuencia técnica.
- **App Review puede rechazarte** y hay que reenviar. Presupuesta al menos un rechazo.
- **La sincronización es irrepetible.** 24 h, una llamada por tipo. Si se pasa, hay que desconectar al
  cliente y repetir el Embedded Signup entero.
- **Mientras dura el trámite, San Pablo no tiene bot nuevo.** Confirma qué tienen hoy: si es el
  *sandbox* de Twilio, en la práctica no sirve para clientes reales (obliga a escribir `join <código>`
  y caduca a los 3 días). Si tienen un número de producción de Twilio funcionando, **no lo apagues**
  hasta que el camino nuevo esté probado.
- **Throughput fijo de 20 mensajes/segundo** en números con Coexistence. Irrelevante a este volumen;
  anotado por si algún día deja de serlo.
- **Error `131060`** en las primeras horas es normal en coexistencia cuando alguien escribe por primera
  vez. Se resuelve solo en segundos. No lo trates como fallo.
- **Sin insignia azul (OBA)** en cuentas de coexistencia, y el nombre visible no se revisa
  automáticamente. Se puede aplicar a Meta Verified aparte.
- **Bus factor 1.** Este plan te deja siendo Tech Provider ante Meta, con un Embedded Signup propio y
  tablas nuevas que solo tú entiendes. Los commits en español y el runbook de `docs/SOPORTE.md` no son
  adorno: son la única documentación que va a existir.
