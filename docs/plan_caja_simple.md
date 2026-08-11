# Plan de implementación · Caja simple (feature/caja-simple)

Spec autocontenido para implementar el rediseño de la vista Caja del `dashboard_admin`.
Objetivo: el rol **caja** ve una interfaz mínima de botones grandes apta para cualquier
persona; el rol **admin** conserva la interfaz completa actual. No se toca la lógica de
negocio de cobros ni el modelo de datos de `pagos` (base de la futura facturación
electrónica y del pago dividido entre varias personas).

## Contexto del sistema

- App Streamlit multi-página en `dashboard_admin/`; router en `panel.py` (`_dispatch`),
  vistas en `views/`, RBAC en `auth.py` (roles `admin`, `caja`, `mesero`; capacidades en
  `_CAPS`, vistas por rol en `ROLE_VIEWS`).
- La caja actual vive en `views/caja.py`: `render()` monta 3 pestañas (Cierre / Inventario /
  Importar); `_render_cierre()` es el arqueo con conteo a ciegas (la caja NO ve lo esperado;
  el admin sí, vía `auth.can("see_revenue")`).
- El cobro vive en `views/pedidos.py`: `dialog_cobrar(ids, titulo, total, uid)` (modal),
  `registrar_pago(...)`, `registrar_pago_mixto(...)`, `registrar_pago_items(...)` y el libro
  `pagos` (columnas: `pedido_id, monto, metodo, submetodo, comprobante, fecha`). Un pedido
  admite N abonos de N personas con métodos distintos — **esto no se altera**.
- Impresión vía `utils/print_jobs.py`: `enqueue_recibo(ids, titulo, total, abono, metodo,
  imprimir=True)`; el payload lleva `abrir_cajon` (True si hubo efectivo). Con
  `imprimir=False` se encola solo la apertura de cajón.
- Personal en `empleados.py`: `crear_empleado`, `regenerar_pin`, `reactivar_acceso(emp_id)`,
  `bloquear_meseros()`, `admin_pin_valido(pin)`, `listar_empleados(...)`. La vista de
  gestión es `views/meseros.py`. El cierre de caja bloquea a todos los meseros
  (`cerrar_caja` → `bloquear_meseros`), por eso hay que reactivarlos cada apertura.
- Auditoría: `audit.registrar(evento, entidad, entidad_id, detalle_dict)`.
- Convenciones: commits `panel: ...` en español; verificación ligera (compilar/importar +
  script dirigido); nunca commitear `.env`; rama de trabajo `feature/caja-simple` → merge a
  `staging`.

## Principios (no negociables)

1. **Una sola vista, dos renders.** No crear `caja_simple.py`. Dentro de `views/caja.py`,
   bifurcar por rol: `admin` → render actual intacto; `caja` → render simple. El selector es
   `auth.can("see_revenue")` (ya distingue admin de caja) o `auth.current_role()`.
2. **La lógica de negocio no se duplica ni se mueve.** Los helpers de BD de `caja.py`
   (`abrir_caja`, `cerrar_caja`, `registrar_gasto`, bases de repartidor, etc.) y los de
   `pedidos.py` se reutilizan tal cual. Solo cambia qué se pinta y en qué orden.
3. **No tocar:** conteo a ciegas, candados H1 (bases exclusivas) y H2 (pago aplicado real),
   regla anti-skimming (`cobro_iniciado`), ni el desglose por tramos del libro `pagos`
   (jamás fusionar/resumir tramos: la facturación electrónica necesitará el detalle
   método/submetodo/comprobante por abono).
4. Toda acción nueva con efecto en dinero se audita con `audit.registrar`.
5. Textos de UI en español, tono simple ("Cobrar mesa", no "Gestionar transacciones").

## Paquetes de trabajo (en orden; un commit por paquete)

### P1 · Bifurcación por rol + home del cajero

**Archivo:** `views/caja.py`.

- En `render()`: si `auth.can("see_revenue")` (admin) → comportamiento actual (3 pestañas).
  Si no (rol caja) → `_render_caja_simple()`, sin pestañas (Inventario e Importar dejan de
  mostrarse al cajero; siguen en la vista del admin).
- `_render_caja_simple()`:
  - Sin turno abierto (`cierre_activo()` es None): tarjeta "La caja se encuentra cerrada" +
    number_input de base + botón grande "🟢 Abrir caja" (reusar `abrir_caja`). Ver P4
    (activación de meseros en la apertura).
  - Con turno abierto: cabecera compacta (hora de apertura + base + badge "🙈 Conteo a
    ciegas") y una **cuadrícula 2×2 de botones grandes** (usar `st.columns(2)` y CSS por
    `st-key` como ya se hace en `_render_cierre` con `.st-key-btn_finalizar_cierre`;
    altura ≈64px, fuente ≥1.05rem):
    1. **💵 Cobrar mesa** → abre `_dialog_cobrar_mesas` (P2). Subtítulo: nº de mesas/pedidos
       con saldo (consulta ligera sobre `pedidos` con `pagado=FALSE`,
       `estado<>'cancelado'`, `fecha::date=CURRENT_DATE`; tolerante a fallos → "—").
    2. **🛵 Repartidores** → sección/diálogo que reusa `_dialog_base`, las tarjetas de bases
       abiertas y `_dialog_retorno` ya existentes (mover ese bloque de
       `_seccion_flujo_caja` a una función reutilizable). Subtítulo: "N en ruta" si hay
       bases abiertas.
    3. **🧾 Gasto de caja** → `_dialog_gasto` existente. Subtítulo: "N sin devolución" si
       hay gastos abiertos (`movimientos_abiertos(cid, "gasto")`).
    4. **🔒 Cerrar turno** → el formulario de cierre actual (extraer de `_render_cierre` a
       `_form_cierre(cierre)` y llamarlo desde ambos renders). El cajero NO ve el historial
       de turnos cerrados ni las métricas de ventas (ya condicionadas por `ver_esperado`);
       el histórico de movimientos del turno queda dentro de un `st.expander`.
- El render del admin no cambia visualmente en este paquete (solo refactors internos:
  extraer `_form_cierre` y la sección de repartidores a funciones compartidas).

**Verificación:** `python -m py_compile dashboard_admin/views/caja.py` + import del módulo.
Prueba manual mínima si hay entorno: login como caja → home 2×2; login admin → pestañas.

### P2 · Entrada directa "Cobrar mesa" para el cajero

**Archivos:** `views/caja.py` (nuevo diálogo), reusa `views/pedidos.py`.

- `@st.dialog("💵 Cobrar")` → `_dialog_cobrar_mesas()`: lista las cuentas cobrables de HOY
  agrupadas: mesas con saldo (agrupar pedidos por `mesa` con `pagado=FALSE`,
  `estado<>'cancelado'`) y pedidos de entrega con saldo sin base asignada. Cada fila:
  "Mesa 4 · $86.000 · restan $56.000" + botón "Cobrar". Al pulsarlo → cerrar este diálogo y
  abrir `pedidos.dialog_cobrar(ids, titulo, saldo, uid)` (el modal de cobro existente, que
  ya soporta por-plato/mixto/parcial → el pago dividido entre varias personas sigue igual:
  se cobra a una persona, se reabre y se cobra a la siguiente sobre el saldo restante).
- Nota Streamlit: un `st.dialog` no puede abrir otro en el mismo run; usar el patrón de
  guardar la intención en `st.session_state` y abrir `dialog_cobrar` en el run siguiente
  (buscar cómo lo hace `monitor_mesas.py` al cobrar, y replicar).
- No modificar `dialog_cobrar` en este paquete (la simplificación visual del checkout es
  fase posterior, fuera de este plan).

**Verificación:** compile + import; consulta SQL de mesas con saldo probada con un script
dirigido si hay BD disponible.

### P3 · Meseros pasa a Administración (solo admin)

**Archivos:** `auth.py`, `panel.py`, `views/meseros.py` (sin cambios internos).

- `auth.ROLE_VIEWS`: quitar `"meseros"` de `ADMIN` y de `CAJA` (deja de ser vista de
  navegación). El cajero queda con `["monitor", "menu", "mesas", "nuevo", "caja"]`.
- `panel.py`: en `_render_admin()` añadir pestaña "👥 Personal" que llama
  `meseros.render()`. Quitar `meseros` del dict de etiquetas de navegación y del dispatch
  de vista propia. Añadir saneo de vista activa: si una sesión traía `"meseros"` como vista
  activa, reapuntarla (admin → `"admin"`, caja → su vista de aterrizaje) — seguir el patrón
  ya existente para `"resumen"` en `panel.py`.
- Ojo: `views/meseros.py` tiene ramas para caja (`puede_gestionar`, generación de PIN de
  turno); al quedar solo-admin no hace falta tocarlas, quedan siempre habilitadas.

**Verificación:** compile de `auth.py` y `panel.py`; login admin → pestaña Personal; login
caja → sin vista Meseros y sin crash si su sesión traía esa vista activa.

### P4 · Activar meseros al abrir la caja

**Archivos:** `views/caja.py`; reusa `empleados.py`.

- Convertir la apertura en un diálogo `@st.dialog("🟢 Abrir caja")` (ambos roles): paso 1
  base de apertura; paso 2 (tras `abrir_caja` OK) lista de meseros con perfil bloqueados
  (`listar_empleados()` filtrando `rol='mesero'` y acceso bloqueado — revisar el campo que
  usa `reactivar_acceso`/`bloquear_acceso` para saber el flag exacto) con botón "Activar"
  por fila y "Activar todos". Cada activación llama `empleados.reactivar_acceso(emp_id)` y
  `audit.registrar("mesero_reactivado", "empleado", emp_id, {"via": "apertura_caja"})`.
  Botón final "Listo, empezar turno" → `st.rerun()`.
- Si no hay meseros bloqueados, el paso 2 se salta.
- La reactivación manual sigue disponible en Administración → Personal (P3).

**Verificación:** compile; con BD: cerrar caja (bloquea meseros) → abrir caja → aparecen
para activar → PIN de mesero vuelve a funcionar.

### P5 · Corrección de cobro con autorización del admin

**Archivos:** `views/pedidos.py` (lógica + UI), `views/caja.py` (punto de entrada).

- **Lógica** — `anular_ultimo_pago(pedido_ids, admin_pin) -> tuple[bool, str]`:
  1. Validar `empleados.admin_pin_valido(admin_pin)`; si además existe contraseña de rol
     admin (`auth.password_for(auth.ADMIN)`), aceptarla también. Sin autorización → (False,
     "PIN de administrador inválido").
  2. En UNA transacción (`engine.begin()` + `FOR UPDATE` del pedido): tomar el ÚLTIMO
     asiento de `pagos` del pedido (mayor `fecha`/id), **borrarlo** (o insertar contra-
     asiento negativo — elegir borrado: el libro `pagos` no tiene columna de anulación y un
     monto negativo rompería los `SUM` de arqueo/resumen… en realidad un negativo suma bien;
     PERO borrarlo mantiene el libro limpio para facturación. Decisión: **borrar la fila** y
     conservar el detalle completo en la auditoría).
  3. Recalcular el pedido: `total_pagado -= monto_anulado`, `pagado = (saldo == 0 → puede
     volver a FALSE)`. Si el pago anulado era el que completó la cuenta, `pagado=FALSE`.
     `cobro_iniciado` NO se revierte (anti-skimming: la cuenta ya tocó caja).
  4. `audit.registrar("pago_anulado", "pedido", pid, {"monto": ..., "metodo": ...,
     "submetodo": ..., "comprobante": ..., "fecha_pago_original": ..., "autorizo":
     "admin_pin", "cajero": audit.actor()[0]})` — el detalle borrado queda íntegro aquí.
- **UI** — en `dialog_cobrar`, bajo la lista de "pagos ya registrados" (si existe; si no,
  añadir un caption con los abonos previos del pedido leyendo `pagos`): enlace discreto
  "⚠️ Corregir último cobro" → pide el PIN de admin (`st.text_input(type="password")`) +
  botón confirmar. Tras anular, `st.rerun()` (el modal reabre con el saldo corregido y el
  cajero re-registra el cobro bien).
- Alcance deliberadamente mínimo: solo el ÚLTIMO abono del pedido, uno a la vez. Nada de
  edición libre de pagos.

**Verificación:** compile + script dirigido que ejercite `anular_ultimo_pago` sobre un
pedido de prueba (crear → cobrar → anular → re-cobrar; el arqueo debe cuadrar).

### P6 · Reimprimir recibo

**Archivos:** `views/pedidos.py` o `views/caja.py` (donde se listan cuentas cobradas),
reusa `utils/print_jobs.py`.

- En la cuenta ya saldada (rama `total <= 0` de `dialog_cobrar`, y/o tarjeta de pedido
  pagado en el tablero): botón "🖨️ Reimprimir recibo" → leer de `pagos` los abonos del
  pedido, llamar `enqueue_recibo(ids, titulo, total, abono_total, metodo_principal,
  imprimir=True)` con `abrir_cajon=False` — revisar la firma real: si `enqueue_recibo`
  decide `abrir_cajon` por método, añadir parámetro opcional `abrir_cajon=None` para
  forzarlo a False en reimpresiones (una copia no debe abrir el cajón).
- Marcar el payload como copia (ej. `"copia": True` en el payload, y que el `print_agent`
  imprima "COPIA" si lo soporta; si no lo soporta aún, dejar el campo y anotar tarea en
  Notion). Auditar: `audit.registrar("recibo_reimpreso", "pedido", pid, {...})`.

**Verificación:** compile; encolar y ver la fila en `print_jobs` con `abrir_cajon=False`.

### P7 · Abrir cajón sin venta

**Archivos:** `views/caja.py`, `utils/print_jobs.py`.

- Helper `enqueue_abrir_cajon(motivo: str)` en `print_jobs.py`: encola un job (tipo
  `"recibo"` con payload mínimo `{"abrir_cajon": True, "sin_papel": True}` o un tipo nuevo
  `"abrir_cajon"` si el `print_agent` lo soporta — revisar `print_agent` antes; si no,
  reutilizar el mecanismo de `enqueue_recibo(..., imprimir=False)` que ya abre cajón sin
  papel, visto en el cobro masivo de bases).
- UI: en el home simple del cajero, un botón secundario pequeño bajo la cuadrícula:
  "🗄️ Abrir cajón (cambio)" → mini-diálogo con motivo opcional y confirmar. Solo con turno
  abierto y `auth.can("manage_caja")`. Auditar SIEMPRE:
  `audit.registrar("cajon_abierto", "caja", cierre_id, {"motivo": ...})`.
- No genera movimiento de dinero (no toca `movimientos_caja`).

**Verificación:** compile; job encolado correcto.

## Orden y commits

```
feature/caja-simple  (desde staging)
  P1 panel: caja simple para el cajero con botones grandes (admin conserva la vista completa)
  P2 panel: boton Cobrar mesa directo en la caja simple
  P3 panel: gestion de personal pasa al entorno de Administracion (solo admin)
  P4 panel: activar meseros del turno al abrir la caja
  P5 panel: corregir ultimo cobro con PIN de admin (auditado)
  P6 impresion: reimprimir recibo sin abrir cajon
  P7 impresion: abrir cajon sin venta, auditado
```

Merge a `staging` al final; probar; luego `staging` → `main`. Borrar la rama tras el merge.

## Fuera de alcance (NO hacer en esta rama)

- Rediseño visual del checkout (`dialog_cobrar`) — fase posterior.
- Facturación electrónica (solo se protege el libro `pagos` para habilitarla).
- Propina y método datáfono — decisiones de negocio pendientes en Notion.
- Cambios de esquema de BD (los paquetes anteriores no requieren migraciones; si algún
  flag de empleados no existe como se asume en P4, adaptarse al esquema real, no migrarlo).

## Checklist final de verificación

- [ ] `python -m py_compile` de todos los módulos tocados + import de `panel`.
- [ ] Rol caja: navegación sin Meseros; caja sin pestañas; 4 botones grandes; nunca ve
      montos esperados ni totales de venta.
- [ ] Rol admin: vista de caja idéntica a la actual + pestaña Personal en Administración.
- [ ] Cobro dividido intacto: N abonos con métodos distintos sobre una cuenta siguen
      registrándose como N filas en `pagos`.
- [ ] Anular último pago exige PIN de admin, cuadra el arqueo y queda auditado.
- [ ] Reimpresión no abre cajón; apertura de cajón sin venta queda auditada.
