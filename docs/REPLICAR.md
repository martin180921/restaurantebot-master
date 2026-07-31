# Replicar el sistema en un restaurante nuevo

Checklist completa para levantar una instancia independiente (modelo **deploy-por-restaurante**: cada restaurante tiene su propia base de datos y sus propios servicios; nada se comparte entre restaurantes).

Tiempo estimado: una tarde. Requisitos: cuenta de Railway, cuenta de Twilio, el PC del restaurante con la impresora térmica.

---

## 1. Base de datos (PostgreSQL en Railway)

1. Crea un proyecto nuevo en Railway → **+ New → Database → PostgreSQL**.
2. Copia la `DATABASE_URL` (pestaña *Connect*).

## 2. Configuración del restaurante (JSON + script)

1. Copia la plantilla y edítala con los datos reales:
   ```bash
   cp scripts/restaurante.example.json mi_restaurante.json
   ```
   Ahí defines: nombre/dirección/teléfono, saludo del bot, precios (plato del día, especiales, recargo de entrega), métodos de pago, **grupos del Plato del Día** (los pasos del configurador: mín/máx selecciones, si permite repetir), componentes iniciales, número de mesas y, opcionalmente, **extras incluidos por categoría** (`categorias_extras`, ver más abajo).
2. Corre el aprovisionador (usa `psycopg2`, no necesitas `psql`):
   ```bash
   pip install psycopg2-binary
   python scripts/provision_restaurante.py --config mi_restaurante.json --database-url "postgresql://..."
   ```
   El script crea el esquema completo, escribe la configuración, siembra grupos/componentes/mesas y **marca los seeds del bot** (para que el arranque no inserte platos de ejemplo). Es idempotente: re-correrlo actualiza la config sin duplicar nada. Al final imprime los bloques de variables de entorno del paso 3 y el `config.json` del paso 5, listos para pegar.

> Todo lo que escribe el script se puede cambiar después desde el panel: 🍔 Menú → ⚙️ Ajustes (identidad, saludo, pagos, precios) y 🍔 Menú → 🍽️ Plato del Día → 🧩 Grupos.

## 3. Servicios en Railway (3 deploys)

Para cada carpeta (`whatsapp_bot/`, `dashboard_admin/`, `app_cliente/`): **+ New → GitHub Repo**, apunta al repo y fija el *Root Directory* a la carpeta. Los `railway.toml` ya definen build y start; solo faltan las variables:

| Servicio | Variables |
|---|---|
| `whatsapp_bot` | `DATABASE_URL`, `APP_CLIENTE_URL` (la URL pública del servicio app_cliente), `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER` |
| `dashboard_admin` | `DATABASE_URL`, `RESTAURANTE_ID` (=1 salvo que sepas por qué no), `PANEL_PASSWORD_ADMIN`, `PANEL_PASSWORD_CAJA` |
| `app_cliente` | `DATABASE_URL`, `RESTAURANTE_ID` |

Notas:
- Genera dominio público (*Settings → Networking → Generate Domain*) para `app_cliente` y `dashboard_admin`; el del bot solo lo necesita Twilio.
- `PANEL_PASSWORD_ADMIN`/`CAJA`: contraseñas fuertes y **distintas por restaurante**. Los meseros no llevan contraseña: usan PIN de turno generado en Caja.
- El primer arranque del bot ejecuta `init_db()`: crea/migra el esquema por si el script no corrió antes. Ambos caminos son compatibles.

## 4. Twilio (WhatsApp)

1. Consola de Twilio → **Messaging → WhatsApp**. Para producción: número propio con perfil de WhatsApp Business aprobado; para pruebas basta el *Sandbox*.
2. Webhook de mensajes entrantes (*When a message comes in*):
   `https://TU-BOT.up.railway.app/webhook` (método POST).
3. `TWILIO_WHATSAPP_NUMBER` = el número (formato `+57...`).
4. Prueba: escribe cualquier cosa al número → debe responder el saludo con el enlace a la carta. El texto del saludo se edita en el panel (⚙️ Ajustes), sin redeploy.

## 5. Agente de impresión local (PC del restaurante)

1. Copia la carpeta `print_agent/` al PC (o clona el repo).
2. `pip install -r requirements.txt`
3. Crea `config.json` a partir de `config.example.json` con el bloque que imprimió el aprovisionador: `DATABASE_URL`, `RESTAURANTE_ID` y la conexión de la impresora (`type: "windows"` + `printer_name` — sale de `Get-Printer` en PowerShell — es la vía recomendada).
4. Prueba sin gastar papel: `python agent.py --dry-run`, y con impresora: `python agent.py --test`.
5. Deja el agente arrancando con Windows (tarea programada o acceso directo a `reiniciar_agente.bat` en `shell:startup`). Para actualizarlo después: `actualizar_agente.bat`.

## 6. QRs de mesa y enlaces

- Carta por mesa (QR impreso en cada mesa): `https://TU-APP-CLIENTE.up.railway.app/?table=<n>`
- Carta de domicilio (la manda el bot): el bot arma el enlace solo con `APP_CLIENTE_URL`.
- Panel: `https://TU-PANEL.up.railway.app`

## 7. Verificación de salida (10 minutos)

- [ ] El panel abre y muestra el **nombre del restaurante** en el login.
- [ ] ⚙️ Ajustes muestra la identidad y los métodos de pago del JSON.
- [ ] 🍽️ Plato del Día muestra los grupos y componentes configurados.
- [ ] La app cliente (`?table=1`) arma un Plato del Día completo y lo envía.
- [ ] El pedido aparece en el Monitor y la comanda sale por la impresora.
- [ ] Un cobro en efectivo abre el cajón y el recibo lleva el encabezado del restaurante.
- [ ] El bot de WhatsApp responde con el saludo y el enlace.

---

## Qué es configurable sin tocar código

| Qué | Dónde |
|---|---|
| Nombre, dirección, teléfono (panel + recibos) | Panel → ⚙️ Ajustes |
| Saludo del bot de WhatsApp | Panel → ⚙️ Ajustes |
| Métodos de pago (efectivo, billeteras) | Panel → ⚙️ Ajustes |
| Precios planos y recargo de entrega | Panel → ⚙️ Ajustes |
| Grupos del Plato del Día (pasos, mín/máx, repetir) | Panel → 🍽️ Plato del Día → 🧩 Grupos |
| Opciones/stock de cada grupo | Panel → 🍽️ Plato del Día / 📦 Inventario |
| Categorías de la carta (nombre, emoji, orden, horario) | Panel → 🍔 Menú → ⚙️ Ajustes → 🏷️ Categorías |
| Extras incluidos por categoría (entrada/bebida sin costo) | Panel → 🍔 Menú → ⚙️ Ajustes → 🏷️ Categorías |
| Carta (las 4 clásicas + las que agregue el restaurante) | Panel → 🍔 Menú |
| Mesas | Panel → 🪑 Mesas |

Lo que sigue requiriendo deploy/env: credenciales de Twilio, contraseñas del panel, `RESTAURANTE_ID`, URL de la app cliente y zona horaria (hoy fija en America/Bogota).

### Extras incluidos por categoría (entrada/bebida sin costo)

Una categoría del catálogo (p. ej. Especiales, A la carta, o una que agregue el restaurante) puede marcarse para que sus platos ofrezcan, sin costo extra, un selector opcional de uno o más grupos del Plato del Día (típicamente Entrada y/o Bebida) — el clásico "corrientazo" con sopa y jugo incluidos. Se activa por categoría desde 🏷️ Categorías; **ninguna categoría nueva lo trae por defecto** (catálogo simple).

Dos cosas a tener en cuenta:

- **Comparte inventario con el Plato del Día.** Si Sopa del día tiene stock/control de porciones, un Especial que la incluye descuenta del MISMO contador que el Plato del Día. Es el comportamiento correcto (es la misma olla de sopa), pero avísalo al restaurante para que no le parezca un error si ve bajar las porciones más rápido de lo esperado.
- Solo se pueden marcar como extra los grupos de **selección única** del Plato del Día (mín/máx = 1, como Entrada o Bebida); un grupo multi-selección (como Acompañamientos) no aparece como opción en el editor.
