-- ============================================================
-- RESET TOTAL DE DATOS — Restaurantebot
-- Borra todos los datos transaccionales y reinicia los IDs.
-- Conserva: menu, menu_componentes, mesas, empleados,
--            clientes, ajustes, agentes_estado.
--
-- CÓMO EJECUTAR:
--   En el panel de Railway → tu servicio PostgreSQL
--   → pestaña "Query" → pega este script y ejecuta.
-- ============================================================

BEGIN;

-- Orden respeta FK: primero las tablas hijas, luego las padres. para bash
TRUNCATE TABLE
    pago_lineas,
    pagos,
    auditoria,
    sesiones_empleado,
    movimientos_caja,
    print_jobs,
    pedidos,
    cierres_caja,
    turnos_caja,
    claves_mesero,
    sesiones
RESTART IDENTITY CASCADE;

COMMIT;

-- para psql
psql $DATABASE_URL -c "TRUNCATE TABLE pago_lineas, pagos, auditoria, sesiones_empleado, movimientos_caja, print_jobs, pedidos, cierres_caja, turnos_caja, claves_mesero, sesiones RESTART IDENTITY CASCADE;"