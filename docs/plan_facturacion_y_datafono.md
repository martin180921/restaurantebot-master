# Plan por tandas: pago con datáfono y facturación electrónica (preparación)

> **Estado: NO se implementa todavía.** Este es el plan de ejecución cuando un
> restaurante lo pida. Se lee y se ejecuta **tanda por tanda**: cada tanda = **un commit
> funcional en `staging`**, probable por sí solo, que deja el sistema entero y desplegable.
> Producto: OKU. Paneles: `dashboard_admin` (Streamlit) y `print_agent`. Rama de trabajo
> `feature/...` → merge a `staging` → probar → `main`. Convención de commits del repo
> (`panel:`, `impresion:`, `infra:`, en español, describiendo el efecto para el usuario).

## Idea rectora (aplica a todo el plan)

1. **Flag apagado por defecto.** Cada función se enciende con un ajuste nuevo. Con el flag
   OFF el sistema se comporta **idéntico a hoy**. Ningún local del piloto ve un cambio hasta
   activarlo. Es la primera línea de defensa de cada tanda.
2. **Proveedor enchufable + implementación SIMULADA.** La factura (DIAN vía PAC) y el
   datáfono integrado viven detrás de **una interfaz**, con una implementación *simulada*
   que no llama a ningún servicio real. Eso permite **mostrarle al restaurante cómo se vería**
   (recibo con CUFE+QR, cobro con tarjeta) sin firmar con DIAN ni con una pasarela. Pasar de
   simulado a real **no toca la UI**.
3. **Reutilizar el patrón nube ↔ agente local.** El panel es nube y no habla con hardware
   del local. Ya resuelto para impresión (`print_jobs` + `print_agent` por polling). El
   datáfono por hardware usa el **mismo patrón**, no uno nuevo.
4. **La capa de pagos ya está preparada.** El libro `pagos` guarda `metodo/submetodo/
   comprobante/fecha` **por abono** y los planes de caja prohíben resumirlo *porque* la
   factura necesita el desglose de medios de pago. Solo se extiende, no se rehace.
5. **Datáfono manual primero, sin API.** El 90 % de los locales opera el datáfono a mano;
   OKU solo *registra* el cobro. Ese bloque (A) da valor inmediato y no depende de ninguna
   integración. Los bloques con API (B datáfono integrado, C factura real) van después y
   detrás de su adapter.

## Contexto de código (leer antes de empezar — referencias verificadas)

- `dashboard_admin/db.py`
  - `metodos_pago()` (~1536) → `{'efectivo': bool, 'transferencia': {clave: etiqueta}}` del
    ajuste JSON `metodos_pago`; fallback al set clásico. **Aquí entra `tarjeta`.**
  - `cargar_ajustes()` / tabla `ajustes` (clave/valor) → hogar de los flags nuevos.
- `dashboard_admin/views/pedidos.py`
  - `registrar_pago(ids, monto, metodo, submetodo, comprobante)` (~398); **coacciona el
    método a efectivo/transferencia en la línea ~414** (`metodo if metodo in (...) else
    "efectivo"`). Igual en `registrar_pago_items` (~642) y `_detalle_transferencia` (~542).
    `registrar_pago_mixto` / `_aplicar_tramos` (~554-631) son genéricos por método.
  - `dialog_cobrar(...)`: radio de método (~1479, `["💵 Efectivo","💳 Transferencia","🔀 Mixto"]`),
    detalle de transferencia + comprobante (~1531), tender/cambio de efectivo (~1550),
    tarjeta de confirmación (~1647), y el bloque de asiento + `enqueue_recibo` (~1655-1711).
- `dashboard_admin/views/caja.py`
  - `ingresos_esperados(fecha_apertura)` (~42) → `SELECT metodo, SUM(monto) ... GROUP BY
    metodo`; hoy solo devuelve `{efectivo, transferencia}` (descarta cualquier otro método).
  - `cerrar_caja(cierre_id, efectivo_esperado, transferencia_esperada, efectivo_real,
    transferencia_real, diferencia)` (~84) y el `UPDATE cierres_caja` (~92). Form de cierre
    del cajero en `_render_caja_simple` (~1639-1663) y el del admin en `_form_cierre`.
- `dashboard_admin/utils/print_jobs.py`
  - `enqueue_recibo(...)` (~139) arma el payload; `tiene_efectivo`/`abrir_cajon` solo con
    efectivo (~175); `_submetodo_label` (~30) traduce la billetera; `_branding_payload` (~16).
- `print_agent/agent.py`
  - `imprimir_recibo(printer, payload)` (~358): método único (~427-438) y tramos del mixto
    (~401-425); pie del recibo. Dispatcher por `tipo` (~1123-1135). **Aquí se pinta el rótulo
    "Datáfono" y el bloque fiscal (resolución + CUFE + QR).**
- `dashboard_admin/views/menu.py` (~978-1046) — editor del ajuste `metodos_pago` en Ajustes.
- `db/schema.sql` — `pagos` (`metodo VARCHAR(20)`, ya cabe "tarjeta", **sin migrar tipo**),
  `cierres_caja` (`efectivo_esperado/real`, `transferencia_esperada/real`), `print_jobs`,
  `auditoria` (append-only, modelo de la tabla fiscal).

## Invariantes que NINGUNA tanda puede romper

- **Conteo a ciegas** del cajero: un método nuevo no le filtra totales de venta.
- El **cajón SAT se abre solo con efectivo.** Tarjeta y transferencia jamás lo abren.
- El libro `pagos` conserva el **detalle por abono** (no fusionar tramos): base de la factura.
- **Flag OFF ⇒ comportamiento actual idéntico**, byte a byte en el flujo del cajero.
- Verificación ligera por defecto: `python -m py_compile ...` + script dirigido; no levantar
  BD ni navegador salvo que se pida.

---

# BLOQUE A — Datáfono manual (sin API)

Da valor inmediato: el datáfono físico (Redeban, Credibanco, Bold, Wompi…) lo opera la
persona; OKU registra que el cobro fue con tarjeta. **Sin hardware, sin API.**

**Decisión de modelo (subyace a todo el bloque):** la tarjeta es un **`metodo='tarjeta'`
nuevo**, no un submétodo de transferencia. `pagos.metodo` ya es `VARCHAR(20)` → **no hay
migración de tipo**. Se elige así porque la liquidación del datáfono llega al banco aparte y
con su comisión: el dueño la concilia como línea propia (ver Tanda A5). Si el dueño no quiere
separarla, es la única decisión a revisar en Notion antes de A1.

### Tanda A1 — Config: encender el método "Datáfono" (sin tocar el cobro aún)

Commit: `panel: metodo de pago datafono configurable en Ajustes (apagado por defecto)`

- **Archivos:** `dashboard_admin/db.py`, `dashboard_admin/views/menu.py`, `db/schema.sql`
  (solo comentario del seed; el ajuste `metodos_pago` ya existe).
- **Lógica/arquitectura:**
  - `metodos_pago()` pasa a devolver también `"tarjeta": bool` (default **False**). Leer la
    clave `tarjeta` del JSON de `metodos_pago`; ausente → False. Mantener el fallback clásico.
  - `views/menu.py` (editor de Ajustes ~1015-1046): añadir un `st.toggle("Aceptar datáfono /
    tarjeta")` que persiste `metodos_pago.tarjeta` junto a `efectivo`/`transferencia`.
  - Nada en el flujo de cobro todavía. Con esto el toggle existe pero no hace nada visible aún.
- **Verificación:** `py_compile` de db.py y menu.py; import de `metodos_pago` y comprobar que
  devuelve `tarjeta=False` por defecto y `True` tras guardar el toggle.

### Tanda A2 — Registrar un pago con método tarjeta (backend, aún sin UI)

Commit: `panel: registrar cobros con metodo tarjeta en el libro de pagos`

- **Archivos:** `dashboard_admin/views/pedidos.py`.
- **Lógica/arquitectura:**
  - Introducir un set único de métodos válidos (p. ej. `_METODOS_VALIDOS = {"efectivo",
    "transferencia", "tarjeta"}`) y usarlo en `registrar_pago` (línea ~414) y
    `registrar_pago_items` (~642) en vez de la coacción a dos valores. Regla: un método fuera
    del set cae a "efectivo" como hoy (defensivo).
  - `tarjeta` **no lleva submétodo**, pero **sí `comprobante`** (nº de voucher / aprobación):
    extender `_detalle_transferencia` (o un helper hermano) para que, en `tarjeta`, conserve
    `comprobante` y deje `submetodo=NULL`. En efectivo sigue NULL/NULL.
  - `_aplicar_tramos` ya es genérico → aceptar tramos `metodo='tarjeta'` sin cambios de firma.
- **Verificación:** `py_compile`; script dirigido (si hay BD de prueba) que llame
  `registrar_pago([id], monto, "tarjeta", comprobante="AUTH123")` y verifique una fila en
  `pagos` con `metodo='tarjeta', submetodo=NULL, comprobante='AUTH123'`.

### Tanda A3 — Datáfono en el checkout (el cajero ya puede elegirlo)

Commit: `panel: opcion Datafono en el cobro con numero de voucher`

- **Archivos:** `dashboard_admin/views/pedidos.py` (`dialog_cobrar`).
- **Lógica/arquitectura:**
  - Radio de método (~1479): si `metodos_pago()['tarjeta']`, agregar la píldora
    **"💳 Datáfono"**. Ojo de UI: "Transferencia" hoy usa 💳 → moverla a **📲** y dejarle 💳
    al datáfono, para no repetir icono.
  - Rama `es_tarjeta`: mostrar campo **"Aprobación / voucher (opcional)"** que va al
    `comprobante`; **no** mostrar tender ni cambio (no es efectivo → `monto_efectivo=0`).
  - Tarjeta de confirmación (~1638-1646): añadir la etiqueta `("💳 Datáfono", abono)` al
    `tramos_card`. Permitir `tarjeta` como tramo del **mixto** (efectivo+tarjeta / tarjeta+
    transferencia) reutilizando el reparto por tramos; si el mixto de 2 campos se complica,
    dejar el mixto-con-tarjeta para una tanda posterior y en A3 soportar solo tarjeta pura.
  - Asiento (~1672-1683): `metodo_pago = "tarjeta"` → `registrar_pago(..., "tarjeta",
    comprobante=voucher)`.
- **Verificación:** `py_compile`; revisar que la píldora solo aparece con el flag ON y que el
  cobro tarjeta puro no pide tender.

### Tanda A4 — Datáfono en el recibo impreso

Commit: `impresion: recibo muestra el metodo Datafono y su voucher`

- **Archivos:** `dashboard_admin/utils/print_jobs.py`, `print_agent/agent.py`.
- **Lógica/arquitectura:**
  - `enqueue_recibo`: `tiene_efectivo`/`abrir_cajon` **no cambian** (tarjeta ≠ efectivo → el
    cajón sigue sin abrirse; invariante ya cumplido). El `metodo="tarjeta"` viaja tal cual en
    el payload; el `comprobante` ya se incluye.
  - `agent.py::imprimir_recibo` (~427-438): el método único ya se capitaliza ("Tarjeta");
    imprimir el `comprobante` de tarjeta como "Voucher/Aprob. …" (hoy el `if comprobante`
    está atado a `metodo=='transferencia'` en ~437 → ampliarlo a tarjeta). En el mixto
    (~401-425), el tramo `tarjeta` se rotula igual.
  - `agent.py` trae recibos de muestra (~608-656): añadir uno de tarjeta para el `--test`.
- **Verificación:** `py_compile`; encolar un recibo tarjeta y revisar el payload; correr el
  recibo de muestra de tarjeta del agente en modo `dummy`.

### Tanda A5 — Arqueo: la tarjeta se concilia aparte (sin romper el conteo a ciegas)

Commit: `panel: arqueo separa el datafono del efectivo y la transferencia`

- **Archivos:** `db/schema.sql` (+ `_ensure_schema` defensivo del servicio),
  `dashboard_admin/views/caja.py`.
- **Lógica/arquitectura:**
  - Esquema: `ALTER TABLE cierres_caja ADD COLUMN IF NOT EXISTS tarjeta_esperada INTEGER NOT
    NULL DEFAULT 0` y `tarjeta_real INTEGER` (idempotente, aditivo; filas viejas quedan en 0).
  - `ingresos_esperados` (~42-61): devolver también `"tarjeta": por_metodo.get("tarjeta", 0)`
    (el `GROUP BY metodo` ya lo trae; hoy se descarta).
  - `cerrar_caja` (~84): ampliar firma/`UPDATE` con `tarjeta_esperada`/`tarjeta_real`. La
    **diferencia de caja física NO cambia** (tarjeta no entra al cajón, igual que la
    transferencia): `diferencia = efectivo_real − total_esperado` se mantiene.
  - Form de cierre (cajero ~1648-1663 y admin `_form_cierre`): añadir un
    `number_input("Datáfono verificado en el banco")` análogo al de transferencia. **Conteo a
    ciegas intacto:** al cajero no se le muestra el `tarjeta_esperada`, solo teclea lo real,
    igual que ya ocurre con la transferencia.
  - Histórico de cierres (~122): incluir las nuevas columnas en la lectura para el detalle.
- **Verificación:** `py_compile`; script dirigido: apertura → un pago `tarjeta` → cerrar →
  el cierre guarda `tarjeta_esperada` correcto y `diferencia` sigue solo sobre efectivo.

> **Fin del Bloque A: datáfono manual completo y desplegable, sin ninguna API.** Es lo que se
> le puede activar a un local "para que vean cómo se ve" en cuanto lo pidan.

---

# BLOQUE B — Datáfono integrado (con API/SDK)  ·  requiere decisión en Notion

OKU manda el monto a la terminal, esta cobra y devuelve la aprobación; el pago se registra
solo. Depende del Bloque A (el manual es el respaldo si la integración se cae).

**Arquitectura — dos caminos según el proveedor:**
- **B-nube:** pasarelas con terminal en la nube (Bold / Wompi-Bancolombia / Mercado Pago
  Point). El panel crea una "intención de cobro" por API y hace *polling* del estado. **Sin
  hardware que puentear.** Preferido.
- **B-local:** terminal solo por SDK Bluetooth/USB → se **reutiliza el patrón `print_agent`**:
  la nube encola la intención (tabla tipo `print_jobs`), un agente local la ejecuta contra la
  terminal y escribe el resultado. Solo si no hay opción de nube.

### Tanda B1 — Interfaz de pasarela + implementación simulada

Commit: `panel: interfaz de pasarela de pago con proveedor simulado (sin cobro real)`

- **Archivos:** nuevo `dashboard_admin/pasarela_pago.py`; ajuste `pasarela_pago` en `db.py`.
- **Lógica/arquitectura:** interfaz mínima y un `PasarelaSimulada` que aprueba tras unos
  segundos (para demo/pruebas), sin llamar a nada:

  ```text
  class PasarelaPago:
      def crear_cobro(monto, referencia) -> intent_id
      def estado_cobro(intent_id) -> "aprobado"|"rechazado"|"pendiente"|"cancelado"
  # PasarelaSimulada (demo) · PasarelaBold / PasarelaWompi (reales, por ajuste)
  ```
- **Verificación:** `py_compile`; test unitario del simulado (crear → pendiente → aprobado).

### Tanda B2 — Flujo de cobro integrado en el checkout (con el simulado)

Commit: `panel: cobro con datafono integrado (pasarela) con estado en vivo`

- **Archivos:** `dashboard_admin/views/pedidos.py`.
- **Lógica/arquitectura:** con `pasarela_pago` configurada, la píldora "💳 Datáfono" ofrece
  "integrado". Al confirmar: `crear_cobro(saldo, ref)` → UI "Pase la tarjeta en el
  datáfono…" y *polling* de `estado_cobro` con un `@st.fragment` (patrón de refresco del
  monitor, pausando mientras el diálogo está abierto). **Aprobado** → `registrar_pago(...,
  "tarjeta", comprobante=aprobacion)` + recibo (reusa A2/A4). **Rechazado/cancelado** → no se
  escribe nada; reintentar o cambiar de método. El manual (A3) queda como respaldo.
- **Verificación:** `py_compile`; con el simulado, cobro que aprueba registra 1 fila `pagos`;
  cobro que rechaza no registra nada.

### Tanda B3 — Adapter real + (si aplica) puente en agente local

Commit: `infra: adapter real de pasarela (Bold/Wompi) [+ pay_agent si es por SDK local]`

- **Archivos:** `dashboard_admin/pasarela_pago.py` (adapter real, credenciales por variables
  de entorno, **fuera del repo**); si es B-local: `print_agent/` extendido o hermano
  `pay_agent/` + tabla `cobros_datafono` en `db/schema.sql`.
- **Verificación:** en entorno de sandbox del proveedor; nunca credenciales reales en el repo.

---

# BLOQUE C — Facturación electrónica (DIAN)  ·  requiere decisión en Notion

**Contexto Colombia (para diseñar, no para decidir por el dueño):** hay dos documentos —
**documento equivalente P.O.S. electrónico** (el recibo se vuelve fiscal; no exige
identificar al cliente en montos bajos) y **factura electrónica de venta** (exige NIT/CC +
nombre, genera **CUFE + QR**, se valida ante DIAN directo o vía **proveedor tecnológico /
PAC**). Emitir requiere: registro DIAN, **resolución de numeración** (prefijo+rango) y
normalmente un PAC (Factus, Alegra, Siigo, Facturatech, herramienta gratuita DIAN…) con API.

### Tanda C1 — Cimientos: tabla fiscal + flags + interfaz + proveedor simulado

Commit: `panel: cimientos de facturacion electronica con proveedor simulado (apagado)`

- **Archivos:** `db/schema.sql` (+ `_ensure_schema`), `dashboard_admin/db.py` (ajustes),
  nuevo `dashboard_admin/facturacion.py`.
- **Lógica/arquitectura:**
  - Ajustes: `facturacion_electronica` (bool, OFF), `proveedor_factura` (str), datos de
    resolución (prefijo, rango, vigencia, NIT); **credenciales del PAC por variables de
    entorno, nunca en el repo**.
  - Tabla `documentos_fiscales` (append-only, estilo `auditoria`):

    ```text
    id, pedido_ids (JSON) | cobro_ref, tipo ('pos'|'factura'),
    numero (prefijo+consecutivo), cufe, qr_texto,
    estado ('borrador'|'emitido'|'rechazado'|'anulado'),
    proveedor, cliente_doc, cliente_nombre, cliente_email, monto,
    fecha, respuesta_cruda (JSONB)
    ```
  - Interfaz `facturacion.py` + `ProveedorSimulado` que devuelve CUFE+QR ficticios (para la
    demo), sin llamar a DIAN:

    ```text
    class ProveedorFactura:
        def emitir(doc) -> {numero, cufe, qr_texto, pdf_url, estado, respuesta_cruda}
        def anular(numero, motivo) -> {estado}   # nota crédito
    # ProveedorSimulado (demo) · ProveedorFactus / ProveedorAlegra (reales)
    ```
  - **Medios de pago de la factura** = desglose de `pagos` (por eso no se resume el libro).
- **Verificación:** `py_compile`; import de `facturacion` y `ProveedorSimulado.emitir` sobre
  un pedido de prueba devuelve un CUFE con forma válida.

### Tanda C2 — Emitir en el cobro (con el simulado) + identidad del cliente

Commit: `panel: emitir documento fiscal al cobrar (simulado) con datos del cliente`

- **Archivos:** `dashboard_admin/views/pedidos.py`.
- **Lógica/arquitectura:** con el flag ON, tras un cobro exitoso, paso opcional
  **"¿Factura electrónica?"** → NIT/CC, nombre, email (solo si el cliente la pide; el
  documento equivalente POS no exige identidad). Llama `facturacion.emitir(...)` (simulado
  por ahora), guarda la fila en `documentos_fiscales` y enlaza a los `pagos`/`pedidos`.
- **Verificación:** `py_compile`; con el simulado, un cobro con factura crea 1 fila en
  `documentos_fiscales` en estado `emitido`.

### Tanda C3 — Bloque fiscal en el recibo (CUFE + QR)

Commit: `impresion: recibo imprime factura electronica con CUFE y QR (si esta emitida)`

- **Archivos:** `dashboard_admin/utils/print_jobs.py`, `print_agent/agent.py`.
- **Lógica/arquitectura:** cuando el cobro tiene documento fiscal, `enqueue_recibo` añade al
  payload el bloque fiscal (encabezado "FACTURA ELECTRÓNICA DE VENTA" / "DOC. EQUIVALENTE
  POS", resolución, prefijo-consecutivo, CUFE, `qr_texto`). `agent.py::imprimir_recibo` pinta
  ese bloque y **renderiza el QR** (la librería de la impresora soporta QR; ver capacidades en
  `agent.py`). Sin documento fiscal → recibo "CUENTA" de hoy, sin cambios (los planes de caja
  ya separaron "CUENTA" no fiscal de la factura: este es el punto exacto de inserción).
- **Verificación:** `py_compile`; recibo de muestra con bloque fiscal simulado en modo `dummy`
  imprime encabezado + CUFE + QR.

### Tanda C4 — Vista "Facturas" en el panel (solo admin)

Commit: `panel: seccion Facturas en Administracion (listar, reimprimir, reenviar)`

- **Archivos:** nuevo `dashboard_admin/views/facturas.py`, `panel.py`, `auth.py`.
- **Lógica/arquitectura:** vista solo-admin (RBAC), colgada de Administración como se hizo con
  Personal: lista de `documentos_fiscales`, estado DIAN, reimprimir, reenviar por email.
  Conteo a ciegas: **no** visible para el rol caja.
- **Verificación:** `py_compile` de panel/auth/facturas; login admin ve la sección; login caja
  no, y no crashea si su sesión traía esa vista.

### Tanda C5 — Proveedor real (PAC): emisión, anulación y envío al cliente

Commit: `infra: adapter real del PAC de facturacion (emitir/anular/enviar)`

- **Archivos:** `dashboard_admin/facturacion.py` (adapter real), `views/facturas.py`
  (anulaciones/notas crédito). Credenciales del PAC **por variables de entorno**.
- **Lógica/arquitectura:** implementar `emitir`/`anular` contra el PAC elegido; el PAC suele
  enviar PDF/XML por email (y opcionalmente el bot de WhatsApp manda el QR/enlace). Cambiar el
  ajuste `proveedor_factura` de `simulado` al real: **la UI y el recibo no cambian**.
- **Verificación:** en el entorno de pruebas del PAC; validar un CUFE real; nunca credenciales
  reales en el repo.

---

# Orden y dependencias

```
Bloque A (datáfono manual, SIN API) — hacer primero, riesgo bajo
  A1 config → A2 registrar → A3 checkout → A4 recibo → A5 arqueo
        (A3 depende de A1/A2; A4 de A2; A5 de A2)

Bloque C (factura) — se puede DEMOSTRAR con el simulado sin DIAN
  C1 cimientos → C2 emitir(simulado) → C3 recibo → C4 vista → C5 PAC real
        (C independiente de A; comparte solo el recibo en C3)

Bloque B (datáfono integrado) — el más pesado, al final
  B1 interfaz → B2 checkout(simulado) → B3 adapter real
        (depende de A: el manual es el respaldo)
```

Ruta recomendada para "mostrar cómo se vería" sin compromiso: **A1–A5** (datáfono real, sin
API) + **C1–C3** con `ProveedorSimulado` (factura que se imprime con CUFE+QR de mentira). Con
eso el restaurante ve y toca ambas cosas; los adapters reales (B3, C5) solo se hacen cuando un
local se comprometa.

# Decisiones para el Registro de Decisiones en Notion (ANTES de A1 / C1 / B1)

- **A:** ¿tarjeta como `metodo` propio (recomendado) o submétodo de transferencia?
- **B:** ¿qué pasarela de datáfono y por qué camino (nube vs SDK local)? (contrato, costos).
- **C:** ¿qué PAC? ¿documento equivalente POS, factura de venta, o ambos, por restaurante?
- **Producto:** ¿facturación/datáfono son parte del plan de OKU o add-on de pago? (precio,
  alcance). Regla del repo: alcance/precio y elección de proveedor son *decisiones grandes* →
  van a Notion **antes** de escribir código. Este documento es solo el diseño técnico.
