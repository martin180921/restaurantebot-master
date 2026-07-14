# Arquitectura

Cómo se conectan las piezas de OLO dentro de **un** restaurante (recuerda: un deploy por restaurante, nada se comparte entre restaurantes).

```
  Comensal                    Restaurante (local)                Tú (flota)
  ────────                    ───────────────────                ──────────
  WhatsApp ──► whatsapp_bot ─┐                                   scripts/
  Navegador ─► app_cliente ──┤                                    ├ provision_restaurante.py
                             ▼                                    ├ backup_db.py / restore_db.py
                     PostgreSQL (Railway) ◄── dashboard_admin     └ monitor_salud.py
                             ▲                (POS, cocina, caja)
                             │
                     print_agent (PC del local) ──► impresora térmica
```

## La base de datos es el punto de encuentro

No hay APIs entre servicios: **todos leen y escriben la misma base PostgreSQL** (esquema en `db/schema.sql`, ~23 tablas). Las centrales:

| Tabla | Quién escribe | Quién lee |
|---|---|---|
| `pedidos` (+ items en JSON) | bot, app_cliente, panel (nuevo_pedido) | panel (cocina/monitor), caja |
| `menu`, `menu_componentes`, `plato_dia_grupos`, `categorias` | panel (🍔 Menú → ⚙️ Ajustes → 🏷️ Categorías) | bot, app_cliente, panel |
| `ajustes` (identidad, saludo, precios, pagos) | provision + panel (⚙️ Ajustes) | todos |
| `print_jobs` (cola de impresión) | panel y app_cliente (al confirmar pedido) | print_agent |
| `agentes_estado` (latido del agente) | print_agent | monitor_salud.py |
| `pagos`, `pago_lineas`, `turnos_caja`, `cierres_caja`, `movimientos_caja` | panel (Caja) | panel (resumen ventas) |
| `empleados`, `claves_mesero`, `sesiones_*`, `auditoria`, `login_intentos` | panel (auth/RBAC) | panel |

## Flujo de un pedido

1. **Entra** por uno de tres caminos: chat de WhatsApp (bot vía Twilio), carta digital (`app_cliente`, pedido web con QR de mesa o domicilio), o el POS del panel (mesero con PIN de turno).
2. Queda en `pedidos`; el monitor de cocina del panel lo ve al instante.
3. Al confirmarse se encola una comanda en `print_jobs`.
4. El `print_agent` (PC del local, instalado con `instalar_agente.bat`) sondea `print_jobs`, imprime en la térmica y marca el estado; deja su latido en `agentes_estado`.
5. El cobro se registra en Caja (por plato o total) → `pagos`/`pago_lineas`, y el cierre de turno hace el arqueo.

## Identidad y roles

- `RESTAURANTE_ID` identifica el restaurante en su propia base (siempre 1 en este modelo).
- Panel: `admin` y `caja` entran con contraseña de entorno (`PANEL_PASSWORD_ADMIN`/`_CAJA`); los **meseros** usan PIN de turno efímero generado en Caja (sin contraseña propia). Capacidades por rol en `dashboard_admin/auth.py`.

## Operación de la flota

Los scripts de `scripts/` corren desde tu PC contra la base de cada restaurante: aprovisionar uno nuevo ([REPLICAR.md](REPLICAR.md)), respaldo diario con poda ([BACKUPS.md](BACKUPS.md)) y monitoreo con alertas a Telegram ([MONITOREO.md](MONITOREO.md)) — su señal principal es la que el cliente notaría primero: **que dejó de imprimir**.

## Mantenimiento (`scripts/sql/`)

- `limpiar_menu_duplicados.sql` — depura platos duplicados del menú.
- `reset_datos.sql` — ⚠️ **destructivo**: borra los datos operativos para reiniciar un restaurante de prueba. Jamás contra producción sin respaldo previo.
