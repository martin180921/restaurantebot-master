# Plan: Caja como centro de operación del cajero (por tandas)

> Plan de implementación autocontenido, pensado para ejecutarse tanda por tanda
> (cada tanda = un commit funcional en `staging`, probable por sí solo).
> Producto: OKU, panel `dashboard_admin` (Streamlit). Rama de trabajo: `feature/...`
> → merge a `staging`. Convención de commits: prefijo `panel:` (e `impresion:` para
> la Tanda 3), mensaje en español.

## Objetivo

El rol **caja** ve la navegación Monitor · Menú · Mesas · Nuevo pedido · Caja.
Hoy el cobro está repartido entre Monitor y Caja. La meta: que **todo lo urgente
del cajero viva en la vista Caja** (cobrar por mesa y por pedido, vueltas,
domicilios, señales del salón) y que el Monitor quede como pantalla de
visualización/estado, sin cobro. El botón Caja debe ser el más visible de la nav.

## Contexto de código (leer antes de empezar)

- `dashboard_admin/views/caja.py` — vista Caja. Dos renders:
  - `_render_cierre()` → admin (`auth.can("see_revenue")`).
  - `_render_caja_simple()` → cajero: home de 4 tarjetas grandes (Cobrar mesa,
    Repartidores, Gasto de caja, Cerrar turno) + Abrir cajón + Equipo del turno.
  - Patrón **pedir/abrir** para diálogos anidados: `_pedir_dialogo_simple(kind, **args)`
    guarda la intención en `st.session_state["_caja_simple_dialog"]` y hace rerun;
    `_abrir_dialogo_simple_pendiente()` abre el diálogo real en el run siguiente.
    Streamlit NO permite abrir un `@st.dialog` desde dentro de otro en el mismo run.
- `dashboard_admin/views/monitor_mesas.py` — Monitor maestro-detalle del salón +
  pestaña de pedidos web. Contiene:
  - `_banner_cambio(suf)` (líneas ~65-95): banner de "Entregar cambio: $X" que lee
    `st.session_state["cambio_pendiente"]` (lo deja `pedidos.dialog_cobrar` tras un
    cobro en efectivo con vuelto), ventana `CAMBIO_WINDOW = 45` s, botón "✓ Entregado".
  - Botones de cobro tras `auth.can("cobrar")`: `mon_cobrar_mesa_<mid>` (mesa
    completa, ~línea 928) y `cobrar_<uid>` (por pedido, ~líneas 1088 y 1322).
  - Fragmentos en vivo `@st.fragment(run_every="30s")` con pausa mientras hay un
    diálogo abierto (`_reanudar_refresco`, `_pedir_dialogo`).
  - Contadores del salón (`por_cobrar`, tarjetas `ped-stat`, ~líneas 727-748).
- `dashboard_admin/views/pedidos.py` — `dialog_cobrar(ids, titulo, saldo, uid)`:
  modal compartido de cobro (efectivo/transferencia, abono parcial, deja
  `cambio_pendiente`). NO tocar su lógica.
- `dashboard_admin/panel.py` — nav lateral. Los botones de nav usan claves
  `nav_active_<view>` / `nav_inactive_<view>` y el CSS los estiliza por
  `[class*="st-key-nav_..."]`. Rol en la variable `role` (`auth.CAJA`, `auth.ADMIN`).
- `dashboard_admin/utils/print_jobs.py` — `enqueue_prerecibo(pedido_ids, titulo)`
  encola el job `"prerecibo"`.
- `print_agent/agent.py` (~líneas 515-522) — imprime el PRERECIBO con encabezado y
  la línea `** NO ES FACTURA VALIDA **`.

**Invariantes que NO se pueden romper:**
- Conteo a ciegas: el cajero (sin `see_revenue`) nunca ve montos esperados ni
  ventas acumuladas. Ningún cambio puede filtrar totales de venta a la caja simple.
- RBAC: toda acción de dinero tras `auth.can("cobrar")` / `auth.can("manage_caja")`.
- No duplicar lógica de negocio: reutilizar `pedidos.dialog_cobrar`, los helpers de
  BD existentes y el patrón pedir/abrir.
- Verificación ligera por defecto: compilar/importar (`python -m py_compile ...`) y
  scripts dirigidos; no levantar BD ni navegador salvo que se pida.

---

## Tanda 1 — Nav destacada + banner de vueltas + Monitor sin cobro

Commit sugerido: `panel: caja protagonista en la nav, vueltas en caja y monitor sin cobro`

### 1.1 Botón "Caja" el más visible de la nav
- En `panel.py`, añadir un bloque CSS dirigido a
  `[class*="st-key-nav_active_caja"] button` y `[class*="st-key-nav_inactive_caja"] button`:
  fondo sólido verde `#16a34a` (o índigo de marca `#4b43b0`), texto blanco,
  `font-weight:700`, algo más de padding — que destaque incluso inactivo.
  Mantener el icono (`--ic`) recolorendo el `::before` a blanco.
- Aplicarlo **solo cuando `role == auth.CAJA`** (inyectar ese `<style>`
  condicionalmente en `_render_desktop_shell`), para no alterar la nav del admin.

### 1.2 Banner de cambio/vueltas en Caja
- Extraer `_banner_cambio(suf)` de `monitor_mesas.py` a un módulo compartido
  (opción simple: moverla a `views/pedidos.py`, que ya es el dueño de
  `cambio_pendiente`; monitor la importa desde ahí).
  Ojo: la versión del monitor llama `_reanudar_refresco()` al pulsar "Entregado" —
  parametrizar con un callback opcional `on_entregado=None` para no acoplar caja
  al estado de refresco del monitor.
- Llamarla al inicio de `_render_caja_simple()` (y de `_render_cierre()` para el
  admin) con sufijo propio, p. ej. `_banner_cambio("caja")`.

### 1.3 Quitar el cobro del Monitor
- En `monitor_mesas.py`, eliminar (para TODOS los roles — decisión: un solo lugar
  de cobro) los botones:
  - `💵 Cobrar mesa` (`mon_cobrar_mesa_<mid>`),
  - `💵 Cobrar $X` por pedido en salón (`cobrar_<uid>`, ~línea 1088),
  - `💵 Cobrar $X` por pedido web (`cobrar_<uid>`, ~línea 1322),
  y las ramas de `_pedir_dialogo("cobrar", ...)` que solo esos botones usaban.
- Sustituirlos por un hint estático no accionable, p. ej. caption
  `"💵 Se cobra en Caja"` donde estaba el botón de mesa (mantiene el hábito visual).
- El banner `_banner_cambio("salon"/"web")` puede QUEDARSE en monitor (no estorba
  y refuerza), pero el origen del cobro ya será Caja.
- Dejar intactos: Precuenta, Marcar listo, Cancelar, Cambio de mesa, edición.
- Revisar que no queden imports/keys huérfanos; el CSS de
  `st-key-mon_cobrar_mesa_` en panel.py puede quedarse (regla muerta inofensiva)
  o limpiarse.

### Verificación Tanda 1
`python -m py_compile dashboard_admin/views/caja.py dashboard_admin/views/monitor_mesas.py dashboard_admin/views/pedidos.py dashboard_admin/panel.py`
y grep de que no queda ningún `st.button` de cobro en monitor_mesas.

---

## Tanda 2 — Cobro completo dentro de Caja (mesa + pedido + detalle)

Commit sugerido: `panel: cobro por mesa y por pedido con detalle de cuenta en caja`

### 2.1 Cobrar por mesa Y por pedido
- Hoy `_dialog_cobrar_mesas()` (caja.py, ~línea 1283) lista cuentas agrupadas por
  mesa (`_cuentas_por_cobrar_hoy()`) con un único botón "Cobrar" por grupo.
- Cambiar el flujo a **dos niveles**: el diálogo lista las cuentas; al elegir una
  se abre un diálogo de **detalle de cuenta** (nuevo, ver 2.2) desde donde se
  cobra todo o por pedido.

### 2.2 Detalle de la cuenta antes de cobrar (escoger mesa → ver todo → confirmar)
- Nuevo `@st.dialog("🧾 Cuenta") _dialog_detalle_cuenta(ids, nombre, tipo)`:
  - Carga los pedidos por id (`pedidos` + ítems via `utils.items.parse_items` /
    `items_para_ticket`, como hace `enqueue_prerecibo`) y muestra: por cada pedido,
    sus platos con cantidad y precio, abonado y saldo; al pie el saldo total de la
    cuenta. Esto NO viola el conteo a ciegas: es el detalle de UNA cuenta (lo mismo
    que imprime la precuenta), no un acumulado de ventas del turno.
  - Botones:
    - `💵 Cobrar todo ($saldo_total)` → `_pedir_dialogo_simple("cobrar", ids=todos, ...)`.
    - Por pedido (si la mesa tiene >1): `Cobrar #id ($saldo)` →
      `_pedir_dialogo_simple("cobrar", ids=[id], ...)`.
    - `🧾 Imprimir cuenta` (ver 2.3).
    - `Volver`.
  - Registrar el nuevo `kind` (p. ej. `"detalle_cuenta"`) en
    `_abrir_dialogo_simple_pendiente()` para poder volver a él tras un cobro parcial
    si resulta natural; como mínimo, entrada directa desde `_dialog_cobrar_mesas`.
- `_dialog_cobrar_mesas` pasa a: fila por cuenta con saldo → botón "Ver / Cobrar"
  que abre el detalle (via pedir/abrir). Mantener el expander "Cobradas hoy"
  (reimpresión) tal cual.

### 2.3 Precuenta oficial ("Cuenta", sin leyenda de no válida)
- Requisito del dueño: el documento de cuenta que se lleva a la mesa debe verse
  **oficial**, sin el aviso "NO ES FACTURA VALIDA".
- Cambios:
  - `print_agent/agent.py` (~515-522): renombrar encabezado de `PRERECIBO` a
    `CUENTA` y **eliminar la línea `** NO ES FACTURA VALIDA **`**. Mantener el
    resto (ítems, abonado, saldo, mesa, branding).
  - UI: renombrar los botones "🧾 Precuenta" / "🧾 Precuenta mesa" del monitor y el
    nuevo botón de caja a "🧾 Cuenta" (o "Imprimir cuenta").
  - No cambiar el nombre del job (`"prerecibo"`) ni la firma de
    `enqueue_prerecibo`: los print_agent desplegados en los locales se actualizan
    aparte; cambiar solo el TEXTO impreso hace el cambio retro-compatible.
- ⚠️ Nota para el implementador (dejarla también en el commit): este documento
  sigue **sin ser una factura fiscal** (no es facturación electrónica DIAN). Se
  retira la leyenda a pedido del producto; no añadir la palabra "FACTURA" al
  encabezado — usar "CUENTA". Si el dueño pide "Factura", escalar la decisión
  al Registro de Decisiones en Notion antes de implementar.
- Commit de esta parte con prefijo `impresion:` si se separa, o mencionarlo en el
  cuerpo del commit de la tanda.

### Verificación Tanda 2
`py_compile` de caja.py + print_agent/agent.py; script dirigido que importe caja y
llame `_cuentas_por_cobrar_hoy` con BD de prueba solo si ya está disponible.

---

## Tanda 3 — Señales en vivo, discretas (sin llenar la pantalla)

Commit sugerido: `panel: caja en vivo con señales discretas de salon y domicilios`

Principio de esta tanda: **números, no listas**. Nada de tarjetas por pedido; solo
contadores que actualizan las tarjetas ya existentes.

### 3.1 Refresco en vivo de la caja simple
- Envolver el cuerpo dinámico de `_render_caja_simple()` (subtítulos de tarjetas,
  contadores, banner de vueltas, historial) en un `@st.fragment(run_every="30s")`,
  siguiendo el patrón de monitor_mesas: **pausar el refresco mientras un diálogo
  esté abierto/pendiente** (si `_caja_simple_dialog` o `_dlg_abrir_caja_open`
  están activos, el fragmento no debe forzar rerun que cierre el modal).
  Replicar el mecanismo `_reanudar_refresco`/pausa de monitor_mesas (leerlo antes;
  no inventar uno nuevo).
- El banner de vueltas (1.2) queda dentro del fragmento para aparecer sin
  interacción.

### 3.2 Contador de domicilios / para llevar pendientes (solo el número)
- Fuente: `pedidos_domicilio_pendientes()` ya existe en caja.py (domicilio +
  para_llevar de hoy, con saldo, sin base asignada).
- Mostrar SOLO un número: en la tarjeta "Repartidores", subtítulo compuesto, p. ej.
  `"2 en ruta · 3 domicilios por asignar"` (omitir la parte que esté en 0).
  Si se prefiere separado: una línea fina tipo `ped-stat` bajo las tarjetas con
  `🛍️ N domicilios pendientes`. Nunca listar los pedidos fuera del diálogo.

### 3.3 Alerta discreta de mesas listas para cobrar
- "Lista para cobrar" = todos sus pedidos entregados y con saldo (estado AZUL del
  monitor, `monitor_mesas.AZUL`). Reproducir el criterio con una consulta ligera
  en caja.py (pedidos de hoy no cancelados, agrupados por mesa, todos
  `estado='entregado'` y saldo > 0) — no importar el dataframe completo del monitor.
- Mostrarlo en la tarjeta "Cobrar mesa": subtítulo p. ej.
  `"5 cuentas · 2 mesas listas ✅"`; si hay mesas listas, resaltar el borde de la
  tarjeta (`border-left` verde) como única señal visual. Sin toasts, sin banners,
  sin sonido.
- Dentro de `_dialog_cobrar_mesas` / detalle, ordenar las cuentas con las mesas
  listas primero y marcarlas con `✅ lista`.

### Verificación Tanda 3
`py_compile`; revisar manualmente (si hay entorno) que abrir un diálogo con el
fragmento activo no lo cierra a los 30 s — es el bug clásico de este patrón.

---

## Orden y dependencias

1. **Tanda 1** — independiente, bajo riesgo, cambia hábitos (cobro solo en Caja):
   desplegar primero y validar con el piloto.
2. **Tanda 2** — depende de 1.3 (el cobro ya debe vivir en Caja para que el
   detalle sea el camino natural). Incluye el cambio en print_agent.
3. **Tanda 3** — depende de 2 (los contadores apuntan a los diálogos nuevos).

Cada tanda: rama `feature/`, merge a `staging`, prueba, luego `main`.
