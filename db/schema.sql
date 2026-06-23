-- ============================================================================
-- schema.sql — bootstrap idempotente de la base de datos de RestauranteBOT.
--
-- ESTO ES OPCIONAL. El esquema canónico lo crea y migra automáticamente el bot
-- (whatsapp_bot/main.py::init_db) en cada arranque, y el panel/app cliente lo
-- garantizan de forma defensiva. Usa este archivo solo si quieres inicializar la
-- base de datos a mano (p. ej. con psql) sin arrancar antes ningún servicio:
--
--     psql "$DATABASE_URL" -f db/schema.sql
--
-- Es idempotente: seguro de correr en una base nueva o ya existente (todo es
-- CREATE/ALTER ... IF NOT EXISTS y los seeds están guardados). Crea exactamente
-- las mismas 15 tablas, columnas e índices que el código, y siembra los 12
-- componentes del Plato del Día y los 4 ajustes de precios/recargo.
--
-- NOTA: a diferencia del bot, NO inserta los 3 platos de ejemplo del menú; arranca
-- con la carta vacía para que cargues tus platos reales en 🍔 Menú.
-- ============================================================================

-- Sesiones del bot de WhatsApp.
CREATE TABLE IF NOT EXISTS sesiones (
    numero      VARCHAR(50) PRIMARY KEY,
    estado      VARCHAR(30) NOT NULL DEFAULT 'inicio',
    carrito     TEXT        NOT NULL DEFAULT '[]',
    actualizado TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- Mesas del salón.
CREATE TABLE IF NOT EXISTS mesas (
    id     SERIAL PRIMARY KEY,
    nombre VARCHAR(50) NOT NULL,
    activa BOOLEAN     NOT NULL DEFAULT TRUE,
    creada TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- Catálogo: Especiales (categoria='especial'), A la carta ('a_la_carta') y
-- Bebidas ('bebida'). El Plato del Día NO vive aquí (se arma con componentes).
CREATE TABLE IF NOT EXISTS menu (
    id            SERIAL PRIMARY KEY,
    nombre        VARCHAR(100) NOT NULL,
    precio        INTEGER      NOT NULL,
    activo        BOOLEAN      NOT NULL DEFAULT TRUE,
    orden         INTEGER      NOT NULL DEFAULT 0,
    agotado_hasta DATE,
    categoria     VARCHAR(20)  NOT NULL DEFAULT 'a_la_carta',
    descripcion   TEXT,
    -- Inventario diario: unidades disponibles hoy. NULL = sin control (ilimitado).
    stock         INTEGER
);

-- Componentes del Plato del Día (grupos: entrada / principio / proteina /
-- acompanamiento). Cada opción es toggleable y soporta "86" (agotado_hasta).
CREATE TABLE IF NOT EXISTS menu_componentes (
    id            SERIAL PRIMARY KEY,
    grupo         VARCHAR(20)  NOT NULL,
    nombre        VARCHAR(100) NOT NULL,
    activo        BOOLEAN      NOT NULL DEFAULT TRUE,
    orden         INTEGER      NOT NULL DEFAULT 0,
    agotado_hasta DATE,
    -- Inventario diario: porciones disponibles hoy de esta opción. NULL = ilimitado.
    stock         INTEGER
);

-- Ajustes clave/valor: precios planos, recargo de entrega y nº de acompañamientos.
CREATE TABLE IF NOT EXISTS ajustes (
    clave VARCHAR(50) PRIMARY KEY,
    valor TEXT        NOT NULL
);

-- Base de clientes (la alimenta la app pública por teléfono).
CREATE TABLE IF NOT EXISTS clientes (
    telefono    VARCHAR(40) PRIMARY KEY,
    nombre      VARCHAR(120),
    direccion   TEXT,
    creado      TIMESTAMP NOT NULL DEFAULT NOW(),
    actualizado TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Pedidos (mesa: tipo_entrega='mesa'/NULL; web: 'domicilio'/'para_llevar').
CREATE TABLE IF NOT EXISTS pedidos (
    id                  SERIAL PRIMARY KEY,
    numero_cliente      VARCHAR(50)  NOT NULL,
    items               TEXT         NOT NULL,
    total               INTEGER      NOT NULL,
    estado              VARCHAR(30)  NOT NULL DEFAULT 'pendiente',
    fecha               TIMESTAMP    NOT NULL DEFAULT NOW(),
    mesa_id             INTEGER      REFERENCES mesas(id),
    motivo_cancelacion  TEXT,
    cancelled_at        TIMESTAMP,
    pagado              BOOLEAN      NOT NULL DEFAULT FALSE,
    total_pagado        INTEGER      NOT NULL DEFAULT 0,
    tipo_entrega        VARCHAR(15),
    cliente_nombre      VARCHAR(120),
    cliente_telefono    VARCHAR(40),
    direccion           TEXT,
    metodo_pago         VARCHAR(20),
    paga_con            INTEGER,
    fee                 INTEGER      NOT NULL DEFAULT 0,
    nota_general        TEXT,
    cobro_iniciado      BOOLEAN      NOT NULL DEFAULT FALSE,  -- anti-skimming: bloquea cancelar
    descuento_valor     INTEGER      NOT NULL DEFAULT 0,      -- rebaja acumulada (gross=total+esto)
    tipo_descuento      VARCHAR(20),                          -- monto | porcentaje | cortesia
    motivo_descuento    TEXT,                                 -- justificación obligatoria
    descuento_autoriza  VARCHAR(120)                          -- admin que autorizó la rebaja
);
CREATE INDEX IF NOT EXISTS idx_pedidos_tipo_entrega ON pedidos (tipo_entrega);
-- Índices del tablero en vivo (lectura acotada por estado / saldo / fecha de hoy).
CREATE INDEX IF NOT EXISTS idx_pedidos_estado    ON pedidos (estado);
CREATE INDEX IF NOT EXISTS idx_pedidos_fecha     ON pedidos (fecha DESC);
CREATE INDEX IF NOT EXISTS idx_pedidos_no_pagado ON pedidos (pagado) WHERE pagado = FALSE;

-- Libro de abonos (método + hora real de pago).
CREATE TABLE IF NOT EXISTS pagos (
    id          SERIAL PRIMARY KEY,
    pedido_id   INTEGER     NOT NULL REFERENCES pedidos(id),
    monto       INTEGER     NOT NULL,
    metodo      VARCHAR(20) NOT NULL DEFAULT 'efectivo',
    submetodo   VARCHAR(20),                      -- nequi | daviplata | breb (NULL en efectivo)
    comprobante VARCHAR(60),                      -- n.º de transacción de la transferencia
    fecha       TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- "Pagar por plato": unidades pagadas por línea (índice en pedidos.items). 'total_pagado'
-- es la autoridad del saldo; esto recuerda QUÉ unidades se cobraron para el checklist.
CREATE TABLE IF NOT EXISTS pago_lineas (
    pedido_id       INTEGER NOT NULL REFERENCES pedidos(id),
    linea_idx       INTEGER NOT NULL,
    cantidad_pagada INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pedido_id, linea_idx)
);

-- PINs de turno efímeros del mesero (acceso solo-PIN, sin contraseña fija). Se generan
-- en caja y se revocan a mano o al cerrar la caja. Solo se guarda el hash del PIN.
CREATE TABLE IF NOT EXISTS claves_mesero (
    id         SERIAL PRIMARY KEY,
    etiqueta   VARCHAR(120),
    clave_hash VARCHAR(64) NOT NULL,
    activa     BOOLEAN     NOT NULL DEFAULT TRUE,
    creada     TIMESTAMP   NOT NULL DEFAULT NOW(),
    revocada   TIMESTAMP,
    creada_por VARCHAR(20)
);
CREATE INDEX IF NOT EXISTS idx_claves_mesero_activa ON claves_mesero (activa);

-- Arqueo de caja v1 (heredado; coexiste con cierres_caja).
CREATE TABLE IF NOT EXISTS turnos_caja (
    id               SERIAL PRIMARY KEY,
    abierto          TIMESTAMP NOT NULL DEFAULT NOW(),
    cerrado          TIMESTAMP,
    fondo_inicial    INTEGER   NOT NULL DEFAULT 0,
    efectivo_contado INTEGER,
    nota             TEXT
);

-- Cierre de caja v2 (apertura con base + congelado esperado vs. contado).
CREATE TABLE IF NOT EXISTS cierres_caja (
    id                     SERIAL      PRIMARY KEY,
    fecha_apertura         TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    fecha_cierre           TIMESTAMP,
    monto_apertura         INTEGER     NOT NULL,
    efectivo_esperado      INTEGER     NOT NULL DEFAULT 0,
    transferencia_esperada INTEGER     NOT NULL DEFAULT 0,
    efectivo_real          INTEGER,
    transferencia_real     INTEGER,
    diferencia             INTEGER     DEFAULT 0,
    estado                 VARCHAR(10) NOT NULL DEFAULT 'abierto'
);

-- Flujo de efectivo del cajón fuera de las ventas: gastos de caja (con su devolución
-- de cambio) y base de cambio del repartidor (con el float devuelto al volver).
-- 'estado'='abierto' = el dinero aún está afuera; 'cerrado' = ya conciliado. 'ref_id'
-- enlaza el retorno con su salida (reingreso_gasto→gasto, retorno_base→base_repartidor).
-- 'pedidos_ref' (JSON de ids) lista los pedidos de domicilio que lleva el repartidor;
-- esos se cobran al volver por el libro 'pagos' (no por aquí → sin doble conteo).
CREATE TABLE IF NOT EXISTS movimientos_caja (
    id           SERIAL       PRIMARY KEY,
    cierre_id    INTEGER      REFERENCES cierres_caja(id),
    tipo         VARCHAR(20)  NOT NULL,   -- gasto | reingreso_gasto | base_repartidor | retorno_base
    monto        INTEGER      NOT NULL,
    motivo       TEXT,
    actor_rol    VARCHAR(20),
    actor_nombre VARCHAR(120),
    ref_id       INTEGER,
    pedidos_ref  TEXT,
    estado       VARCHAR(15)  NOT NULL DEFAULT 'abierto',
    creado_at    TIMESTAMP    NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_movimientos_caja_cierre ON movimientos_caja (cierre_id, tipo, estado);

-- Cola de impresión multi-tenant (el agente local hace polling por tenant+estado).
CREATE TABLE IF NOT EXISTS print_jobs (
    id             SERIAL      PRIMARY KEY,
    restaurante_id INTEGER     NOT NULL,
    tipo           VARCHAR(20) NOT NULL,
    payload        JSONB       NOT NULL,
    estado         VARCHAR(15) NOT NULL DEFAULT 'pendiente',
    intentos       INTEGER     NOT NULL DEFAULT 0,
    error_msg      TEXT,
    creado_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
    reclamado_at   TIMESTAMP,                 -- hora del claim (estado→imprimiendo); janitor
    impreso_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_print_jobs_tenant_estado ON print_jobs (restaurante_id, estado);

-- ── FASE 1: auditoría, personal y heartbeat del agente ──────────────────────
-- Perfiles PERSISTENTES de personal (mesero/caja/admin) con PIN propio (hash). Fuente de
-- identidad del actor en la auditoría. Distinto de claves_mesero (PIN efímero de turno).
CREATE TABLE IF NOT EXISTS empleados (
    id         SERIAL       PRIMARY KEY,
    nombre     VARCHAR(120) NOT NULL,
    rol        VARCHAR(20)  NOT NULL DEFAULT 'mesero',
    pin_hash   VARCHAR(64)  NOT NULL,
    activo     BOOLEAN      NOT NULL DEFAULT TRUE,
    bloqueado  BOOLEAN      NOT NULL DEFAULT FALSE,  -- acceso cerrado por caja hasta reactivar
    creado     TIMESTAMP    NOT NULL DEFAULT NOW(),
    creado_por VARCHAR(120)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_empleados_pin ON empleados (pin_hash);
CREATE INDEX IF NOT EXISTS idx_empleados_activo ON empleados (activo);
ALTER TABLE empleados ADD COLUMN IF NOT EXISTS bloqueado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE empleados ADD COLUMN IF NOT EXISTS token VARCHAR(32);  -- persistencia móvil del mesero (?mt)
CREATE INDEX IF NOT EXISTS idx_empleados_token ON empleados (token);

-- Marcaje entrada/salida (clock-in/out). Una sesión activa como mucho por empleado.
CREATE TABLE IF NOT EXISTS sesiones_empleado (
    id               SERIAL    PRIMARY KEY,
    empleado_id      INTEGER   REFERENCES empleados(id),
    nombre           VARCHAR(120),
    rol              VARCHAR(20),
    login_at         TIMESTAMP NOT NULL DEFAULT NOW(),
    logout_at        TIMESTAMP,
    ultima_actividad TIMESTAMP NOT NULL DEFAULT NOW(),  -- latido de presencia (en turno)
    activa           BOOLEAN   NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_sesiones_emp_activa ON sesiones_empleado (activa);
CREATE INDEX IF NOT EXISTS idx_sesiones_emp_login  ON sesiones_empleado (login_at DESC);

-- Libro mayor central de eventos críticos (append-only): quién, qué, cuándo, sobre qué.
CREATE TABLE IF NOT EXISTS auditoria (
    id             SERIAL      PRIMARY KEY,
    ts             TIMESTAMP   NOT NULL DEFAULT NOW(),
    actor_nombre   VARCHAR(120),
    actor_rol      VARCHAR(20),
    accion         VARCHAR(40) NOT NULL,
    entidad        VARCHAR(40),
    entidad_id     INTEGER,
    detalle        JSONB,
    restaurante_id INTEGER     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_auditoria_ts     ON auditoria (ts DESC);
CREATE INDEX IF NOT EXISTS idx_auditoria_accion ON auditoria (accion);
CREATE INDEX IF NOT EXISTS idx_auditoria_actor  ON auditoria (actor_nombre);

-- Latido (heartbeat) del Agente de Impresión Local: una fila por restaurante.
CREATE TABLE IF NOT EXISTS agentes_estado (
    restaurante_id INTEGER   PRIMARY KEY,
    hostname       VARCHAR(120),
    visto_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    cola_pendiente INTEGER   NOT NULL DEFAULT 0
);

-- ── Upgrade de tablas preexistentes (idempotente) ───────────────────────────
-- Si menu/pedidos YA existían (base de datos en uso), los CREATE de arriba no los
-- tocan, así que estos ALTER garantizan las columnas nuevas sin perder datos —
-- el mismo efecto que init_db()/_ensure_schema() al arrancar los servicios.
ALTER TABLE menu    ADD COLUMN IF NOT EXISTS agotado_hasta DATE;
ALTER TABLE menu    ADD COLUMN IF NOT EXISTS categoria VARCHAR(20) NOT NULL DEFAULT 'a_la_carta';
ALTER TABLE menu    ADD COLUMN IF NOT EXISTS descripcion TEXT;
ALTER TABLE menu             ADD COLUMN IF NOT EXISTS stock INTEGER;  -- inventario diario (NULL = ilimitado)
ALTER TABLE menu_componentes ADD COLUMN IF NOT EXISTS stock INTEGER;  -- inventario diario por componente
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mesa_id INTEGER REFERENCES mesas(id);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS motivo_cancelacion TEXT;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pagado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS total_pagado INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tipo_entrega VARCHAR(15);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cliente_nombre VARCHAR(120);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cliente_telefono VARCHAR(40);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS direccion TEXT;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS metodo_pago VARCHAR(20);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS paga_con INTEGER;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS fee INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS nota_general TEXT;
ALTER TABLE pagos   ADD COLUMN IF NOT EXISTS submetodo VARCHAR(20);
ALTER TABLE pagos   ADD COLUMN IF NOT EXISTS comprobante VARCHAR(60);
-- FASE 1: anti-skimming + descuentos en pedidos, y reclamado_at en la cola de impresión.
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cobro_iniciado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS descuento_valor INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS tipo_descuento VARCHAR(20);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS motivo_descuento TEXT;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS descuento_autoriza VARCHAR(120);
ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS reclamado_at TIMESTAMP;
ALTER TABLE sesiones_empleado ADD COLUMN IF NOT EXISTS ultima_actividad TIMESTAMP;

-- ── Seeds (idempotentes) ────────────────────────────────────────────────────
INSERT INTO ajustes (clave, valor) VALUES
    ('plato_dia_precio',  '18000'),
    ('especiales_precio', '25000'),
    ('fee_entrega',       '4000'),
    ('acompanamientos_n', '3')
ON CONFLICT (clave) DO NOTHING;

-- Solo siembra si la tabla está vacía (no resucita opciones borradas en re-runs).
INSERT INTO menu_componentes (grupo, nombre, orden)
SELECT v.grupo, v.nombre, v.orden FROM (VALUES
    ('entrada',        'Fruta',        1),
    ('entrada',        'Huevo',        2),
    ('entrada',        'Sopa del día', 3),
    ('principio',      'Frijol',       1),
    ('principio',      'Lenteja',      2),
    ('proteina',       'Res',          1),
    ('proteina',       'Cerdo',        2),
    ('proteina',       'Pechuga',      3),
    ('acompanamiento', 'Arroz',        1),
    ('acompanamiento', 'Maduro',       2),
    ('acompanamiento', 'Papa',         3),
    ('acompanamiento', 'Ensalada',     4)
) AS v(grupo, nombre, orden)
WHERE NOT EXISTS (SELECT 1 FROM menu_componentes);
