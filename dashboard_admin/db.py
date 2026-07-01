"""Conexión compartida a la base de datos y lecturas comunes entre vistas."""
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from datetime import date, datetime
from zoneinfo import ZoneInfo
import streamlit as st
import pandas as pd
import os

from utils.items import parse_items

load_dotenv()

# ── Zona horaria del negocio: Bogotá, Colombia (UTC−5, sin horario de verano) ────
# Todo el negocio razona por DÍA: corte de caja, número de pedido del día, "pedidos
# de hoy" del tablero, y la base del repartidor. Sin fijar la zona, Railway corre en
# UTC y "hoy" cambia a las 7 p.m. hora de Bogotá → el selector de la base arrastraba
# los pedidos del día anterior y el número de pedido del día se reiniciaba a media
# tarde. La CONEXIÓN (NOW()/CURRENT_DATE/fecha::date) queda fija vía connect_args más
# abajo (Postgres trae su propio catálogo de zonas, así que eso SIEMPRE funciona).
#
# El PROCESO (datetime.now()/date.today()) es otro cantar: TZ + time.tzset() de abajo
# es best-effort y NO basta por sí solo — Railway corre sobre Nixpacks, cuya imagen no
# siempre trae el tzdata del sistema (/usr/share/zoneinfo), así que tzset() cae en UTC
# en silencio (glibc: zona no encontrada → UTC) aunque TZ esté fijada. El síntoma es
# exactamente 5h (300 min) de más en cualquier "hace N minutos" calculado en Python
# (Monitor, etc.), porque el proceso queda en UTC mientras 'fecha' en la BD ya está en
# hora de Bogotá. Por eso TODO el código de negocio debe usar ahora_bogota()/
# hoy_bogota() de aquí abajo en vez de datetime.now()/date.today() directo: usan
# zoneinfo + el paquete 'tzdata' (dato IANA embebido en Python), que no depende del
# tzdata del SO ni de time.tzset() y por tanto funciona igual en Railway, Windows o
# donde sea.
import time as _time
os.environ.setdefault("TZ", "America/Bogota")
if hasattr(_time, "tzset"):
    _time.tzset()

_BOGOTA = ZoneInfo("America/Bogota")


def ahora_bogota() -> datetime:
    """'Ahora' naive en hora de Bogotá. Úsalo en vez de datetime.now() (ver nota de
    arriba) en cualquier cálculo que se compare contra 'fecha'/timestamps de la BD.
    Naive a propósito: esas columnas se guardan sin tz (wall-clock de Bogotá vía la
    zona de sesión del engine), así restan/comparan directo sin conversiones."""
    return datetime.now(_BOGOTA).replace(tzinfo=None)


def hoy_bogota() -> date:
    """'Hoy' en Bogotá. Úsalo en vez de date.today() (ver ahora_bogota)."""
    return ahora_bogota().date()


# ── Config de conexión (C7) ─────────────────────────────────────────────────────
def _normalizar_db_url(url):
    """Valida y normaliza DATABASE_URL.

    Railway/Heroku entregan el esquema 'postgres://', que SQLAlchemy 2.0 ya no
    acepta; lo reescribimos a 'postgresql://'. Si falta, fallamos con un mensaje
    claro en vez de un error opaco de create_engine(None).
    """
    if not url:
        raise RuntimeError(
            "DATABASE_URL no está configurada. Define la variable de entorno con "
            "la cadena de conexión de PostgreSQL antes de arrancar el panel."
        )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = _normalizar_db_url(os.getenv("DATABASE_URL"))

# Identidad del inquilino (SaaS multi-tenant). Aún no hay tabla de restaurantes ni
# login por tenant, así que de momento la tomamos del entorno (default 1 = single
# tenant). Cuando exista auth por restaurante, esto saldrá de la sesión. El agente
# local filtra por este mismo id en su config.json.
RESTAURANTE_ID = int(os.getenv("RESTAURANTE_ID", "1"))
# C5: pre_ping descarta conexiones muertas (Railway corta las inactivas) y
# pool_recycle las renueva antes del timeout del servidor → sin 500s aleatorios.
# P-POOL: techo de conexiones EXPLÍCITO por proceso. El pool por defecto de
# SQLAlchemy (pool_size=5 + max_overflow=10) abre hasta 15 conexiones por proceso;
# con tres servicios (panel, app_cliente, bot) sobre una sola Postgres pequeña eso
# llega a 45 y agota el límite del plan → 'FATAL: too many connections' (cae todo).
# Lo acotamos a 5+5=10/proceso y, con pool_timeout=10, una espera por contención
# falla rápido con error claro en vez de congelar la UI de Streamlit indefinidamente.
engine = create_engine(
    DATABASE_URL,
    # Zona horaria de la SESIÓN de la BD: que NOW()/CURRENT_DATE/fecha::date sean hora
    # de Bogotá (no UTC). Va de la mano con la zona del proceso fijada arriba.
    connect_args={"options": "-c timezone=America/Bogota"},
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=5,
    max_overflow=5,
    pool_timeout=10,
)


# ── Esquema defensivo (F1/F6) ───────────────────────────────────────────────────
# El bot es el dueño del esquema, pero el panel puede arrancar antes. Garantizamos
# las columnas que necesitan sus flujos (motivo de cancelación, "agotado hoy") una
# sola vez al importar este módulo (proceso del panel). Idempotente.
def _ensure_schema():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS motivo_cancelacion TEXT"))
        # mesero: quién tomó el pedido (nombre del actor en sesión al crearlo). NULL en los
        # pedidos del cliente (app pública / QR), que no los arma personal. Lo muestra el Monitor.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mesero VARCHAR(120)"))
        # cancelled_at: marca de tiempo de la cancelación, para el historial del
        # administrador (agrupa los cancelados por día). Se rellena al cancelar; los
        # cancelados previos a esta columna quedan NULL y caen bajo su 'fecha' de creación.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS agotado_hasta DATE"))
        # pagado: cobro independiente del estado de cocina (lo usa el monitor de mesas).
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pagado BOOLEAN NOT NULL DEFAULT FALSE"))
        # total_pagado: libro acumulado de abonos para pagos parciales / cuentas
        # divididas (saldo = total − total_pagado). Canónico en el bot; defensivo aquí.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS total_pagado INTEGER NOT NULL DEFAULT 0"))
        # pagos: libro de abonos (método + hora real de pago). Canónico en el bot;
        # defensivo aquí para que el panel cobre y desglose por método pre-redeploy.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pagos (
                id         SERIAL PRIMARY KEY,
                pedido_id  INTEGER     NOT NULL REFERENCES pedidos(id),
                monto      INTEGER     NOT NULL,
                metodo     VARCHAR(20) NOT NULL DEFAULT 'efectivo',
                fecha      TIMESTAMP   NOT NULL DEFAULT NOW()
            )
        """))
        # Transferencias detalladas: submetodo (nequi/daviplata/breb) y comprobante
        # (n.º de transacción). NULL en efectivo. Canónico en el bot; defensivo aquí.
        conn.execute(text("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS submetodo VARCHAR(20)"))
        conn.execute(text("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS comprobante VARCHAR(60)"))
        # pago_lineas: libro auxiliar de "pagar por plato" — cuántas unidades de cada
        # línea (índice en pedidos.items) ya están pagadas. 'total_pagado' sigue siendo
        # la autoridad del saldo; esto solo recuerda QUÉ unidades se cobraron para el
        # checklist. Invariante en modo por-plato: total_pagado == Σ(cantidad_pagada×precio).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pago_lineas (
                pedido_id       INTEGER NOT NULL REFERENCES pedidos(id),
                linea_idx       INTEGER NOT NULL,
                cantidad_pagada INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pedido_id, linea_idx)
            )
        """))
        # claves_mesero: PINs de turno efímeros del mesero (acceso solo-PIN, sin
        # contraseña fija). Se generan en caja, se revocan a mano o al cerrar la caja.
        # Guardamos solo el hash; el PIN se muestra una sola vez al generarlo.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS claves_mesero (
                id         SERIAL PRIMARY KEY,
                etiqueta   VARCHAR(120),
                clave_hash VARCHAR(64) NOT NULL,
                activa     BOOLEAN     NOT NULL DEFAULT TRUE,
                creada     TIMESTAMP   NOT NULL DEFAULT NOW(),
                revocada   TIMESTAMP,
                creada_por VARCHAR(20)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_claves_mesero_activa ON claves_mesero (activa)"
        ))
        # turnos_caja: arqueo de caja v1 (apertura con fondo, cierre con conteo).
        # Reemplazada por cierres_caja (abajo); la dejamos definida de forma defensiva
        # para no perder datos de turnos ya cerrados en despliegues previos.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS turnos_caja (
                id               SERIAL PRIMARY KEY,
                abierto          TIMESTAMP NOT NULL DEFAULT NOW(),
                cerrado          TIMESTAMP,
                fondo_inicial    INTEGER   NOT NULL DEFAULT 0,
                efectivo_contado INTEGER,
                nota             TEXT
            )
        """))
        # cierres_caja: arqueo de turno v2 (cierre de caja). Apertura con base en
        # efectivo, y al cerrar se congela lo esperado vs. lo contado (efectivo y
        # transferencias verificadas en banco) más la diferencia de caja. Como mucho
        # un turno con estado='abierto' a la vez. Canónico en el bot; defensivo aquí.
        conn.execute(text("""
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
            )
        """))
        # movimientos_caja: flujo de efectivo del cajón fuera de las ventas (gastos de
        # caja con su devolución de cambio, y base de cambio del repartidor con el float
        # devuelto al volver). 'estado'='abierto' = dinero aún afuera; 'cerrado' = ya
        # conciliado. 'ref_id' enlaza el retorno con su salida (reingreso_gasto→gasto,
        # retorno_base→base_repartidor). 'pedidos_ref' (JSON de ids) lista los pedidos de
        # domicilio que el repartidor lleva, para cobrarlos al volver (esos cobros entran
        # por el libro 'pagos', NO por aquí → sin doble conteo). Va DESPUÉS de cierres_caja.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS movimientos_caja (
                id           SERIAL       PRIMARY KEY,
                cierre_id    INTEGER      REFERENCES cierres_caja(id),
                tipo         VARCHAR(20)  NOT NULL,
                monto        INTEGER      NOT NULL,
                motivo       TEXT,
                actor_rol    VARCHAR(20),
                actor_nombre VARCHAR(120),
                ref_id       INTEGER,
                pedidos_ref  TEXT,
                estado       VARCHAR(15)  NOT NULL DEFAULT 'abierto',
                creado_at    TIMESTAMP    NOT NULL DEFAULT NOW()
            )
        """))
        # Defensivo si la tabla se creó antes de existir esta columna.
        conn.execute(text("ALTER TABLE movimientos_caja ADD COLUMN IF NOT EXISTS pedidos_ref TEXT"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_movimientos_caja_cierre "
            "ON movimientos_caja (cierre_id, tipo, estado)"
        ))
        # base_id (H1): enlace RELACIONAL pedido→base de repartidor. Reemplaza a
        # 'pedidos_ref' (JSON meramente informativo) como fuente de verdad de qué pedidos
        # lleva un repartidor. Al entregar una base se fija base_id con un UPDATE CONDICIONAL
        # (WHERE base_id IS NULL …), de modo que un mismo pedido no puede quedar en dos bases;
        # y la base no se cierra hasta que el saldo de sus pedidos esté en 0 (ver caja.py).
        # Sin FK dura aquí (defensivo sobre una BD viva); la versión canónica (schema.sql) sí
        # la declara. NULL = el pedido no va en ninguna base de repartidor.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS base_id INTEGER"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pedidos_base ON pedidos (base_id) "
            "WHERE base_id IS NOT NULL"
        ))
        # H3: clave de idempotencia. Un reintento (doble clic / corte de red al confirmar) que
        # reusa la misma clave NO crea un pedido duplicado: el INSERT usa ON CONFLICT (idem_key)
        # DO NOTHING y este índice único es el árbitro. NULLs son distintos en Postgres → los
        # pedidos legados sin clave no chocan entre sí.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS idem_key VARCHAR(40)"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pedidos_idem ON pedidos (idem_key)"
        ))
        # print_jobs: cola de impresión multi-tenant. El panel (cloud/Railway) encola
        # filas 'pendiente'; el Agente de Impresión Local de cada restaurante hace
        # polling por (restaurante_id, estado) y las imprime en su Epson 80mm. 'payload'
        # es JSONB con el ticket ya armado (ítems, totales, abrir_cajon, etc.).
        # 'error_msg' (fuera de la spec original) guarda el último fallo del agente.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS print_jobs (
                id             SERIAL      PRIMARY KEY,
                restaurante_id INTEGER     NOT NULL,
                tipo           VARCHAR(20) NOT NULL,
                payload        JSONB       NOT NULL,
                estado         VARCHAR(15) NOT NULL DEFAULT 'pendiente',
                intentos       INTEGER     NOT NULL DEFAULT 0,
                error_msg      TEXT,
                creado_at      TIMESTAMP   DEFAULT CURRENT_TIMESTAMP,
                impreso_at     TIMESTAMP
            )
        """))
        # Índice de polling del agente: WHERE restaurante_id = ? AND estado = 'pendiente'.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_print_jobs_tenant_estado "
            "ON print_jobs (restaurante_id, estado)"
        ))
        # reclamado_at: hora en que el agente tomó el job (estado→'imprimiendo'). El janitor
        # del agente la usa para detectar trabajos atascados sin re-encolar los que se están
        # imprimiendo ahora (no sirve creado_at). Defensiva: el agente también la garantiza.
        conn.execute(text("ALTER TABLE print_jobs ADD COLUMN IF NOT EXISTS reclamado_at TIMESTAMP"))

        # ── OVERHAUL DEL MENÚ (aditivo, defensivo) ───────────────────────────
        # El bot es el dueño canónico del esquema y siembra los datos; aquí solo
        # garantizamos que las tablas/columnas EXISTAN para que el panel lea sin
        # romperse si arranca antes del redeploy del bot. NO sembramos componentes
        # (los siembra el bot) para no recrear opciones que el restaurante borró.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS menu_componentes (
                id            SERIAL PRIMARY KEY,
                grupo         VARCHAR(20)  NOT NULL,
                nombre        VARCHAR(100) NOT NULL,
                activo        BOOLEAN      NOT NULL DEFAULT TRUE,
                orden         INTEGER      NOT NULL DEFAULT 0,
                agotado_hasta DATE
            )
        """))
        conn.execute(text(
            "ALTER TABLE menu ADD COLUMN IF NOT EXISTS categoria VARCHAR(20) NOT NULL DEFAULT 'a_la_carta'"
        ))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS descripcion TEXT"))
        # ── INVENTARIO DIARIO (stock por componente y por plato) ─────────────
        # 'stock' = porciones/unidades disponibles HOY. Modelo de dos niveles:
        #   menu_componentes.stock → cada micro-opción del Plato del Día (cada sopa,
        #     cada proteína, cada acompañamiento) lleva su propio contador.
        #   menu.stock             → cada plato a la carta / especial / bebida como
        #     unidad completa (1 a 1).
        # NULL = SIN control (ilimitado): el ítem se comporta como hasta ahora (no se
        # oculta ni se bloquea). Un número activa el control: al crear un pedido se
        # descuenta y al cancelarlo (antes de 'listo') se reintegra. El administrador
        # fija las cantidades cada mañana en 🍔 Menú → 📦 Inventario.
        conn.execute(text("ALTER TABLE menu_componentes ADD COLUMN IF NOT EXISTS stock INTEGER"))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS stock INTEGER"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ajustes (
                clave VARCHAR(50) PRIMARY KEY,
                valor TEXT        NOT NULL
            )
        """))
        # Ajustes: sí sembramos defaults (ON CONFLICT DO NOTHING, sin resurrección)
        # para que precios/recargo tengan valor aunque el panel arranque primero.
        conn.execute(text("""
            INSERT INTO ajustes (clave, valor) VALUES
            ('plato_dia_precio',  '18000'),
            ('especiales_precio', '25000'),
            ('fee_entrega',       '4000'),
            ('acompanamientos_n', '3')
            ON CONFLICT (clave) DO NOTHING
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clientes (
                telefono    VARCHAR(40) PRIMARY KEY,
                nombre      VARCHAR(120),
                direccion   TEXT,
                creado      TIMESTAMP NOT NULL DEFAULT NOW(),
                actualizado TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        for _col, _ddl in [
            ("tipo_entrega",     "VARCHAR(15)"),
            ("cliente_nombre",   "VARCHAR(120)"),
            ("cliente_telefono", "VARCHAR(40)"),
            ("direccion",        "TEXT"),
            ("metodo_pago",      "VARCHAR(20)"),
            ("paga_con",         "INTEGER"),
            ("fee",              "INTEGER NOT NULL DEFAULT 0"),
            ("nota_general",     "TEXT"),
        ]:
            conn.execute(text(f"ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS {_col} {_ddl}"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pedidos_tipo_entrega ON pedidos (tipo_entrega)"
        ))

        # ── FASE 1: control anti-fraude, auditoría y personal ────────────────
        # Bloqueo anti-skimming + descuento autorizado en pedidos.
        #   cobro_iniciado     → TRUE en cuanto la cuenta toca caja (se abre el cobro o
        #                        se aplica un descuento). Una vez TRUE, cancelar queda
        #                        bloqueado (igual que con un abono) → evita anular ventas
        #                        ya cobradas para quedarse el efectivo.
        #   descuento_valor    → rebaja acumulada (gross = total + descuento_valor).
        #   tipo/motivo/autoriza→ tipo de rebaja, justificación y admin que la autorizó.
        for _col, _ddl in [
            ("cobro_iniciado",     "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("descuento_valor",    "INTEGER NOT NULL DEFAULT 0"),
            ("tipo_descuento",     "VARCHAR(20)"),
            ("motivo_descuento",   "TEXT"),
            ("descuento_autoriza", "VARCHAR(120)"),
        ]:
            conn.execute(text(f"ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS {_col} {_ddl}"))

        # num_dia: contador diario del pedido (1, 2, 3… se reinicia cada día). Lo asigna
        # siguiente_num_dia() de forma ATÓMICA vía la tabla contador_dia (no con MAX+1,
        # que bajo concurrencia daba números duplicados). NULL en pedidos previos a esta
        # columna → se muestra el id global.
        conn.execute(text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS num_dia INTEGER"))
        # contador_dia: una fila por fecha con el último num_dia entregado. El upsert
        # ON CONFLICT toma el lock de la fila del día → serializa la numeración sin
        # carreras. La primera vez del día se siembra desde el MAX real para no colisionar
        # con pedidos ya creados hoy (ver siguiente_num_dia).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS contador_dia (
                fecha DATE    PRIMARY KEY,
                n     INTEGER NOT NULL DEFAULT 0
            )
        """))

        # Índices del tablero en vivo: el SELECT activo filtra por estado, saldo y fecha
        # de hoy; sin estos cae en seq-scan de toda la tabla bajo carga (ver pedidos.py).
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pedidos_estado ON pedidos (estado)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_pedidos_fecha ON pedidos (fecha DESC)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pedidos_no_pagado ON pedidos (pagado) "
            "WHERE pagado = FALSE"
        ))

        # empleados: perfiles PERSISTENTES de personal (mesero/caja/admin) con PIN propio
        # (hash). Distinto de claves_mesero (PIN efímero de turno, legado): un empleado no
        # se revoca al cerrar caja. Es la fuente de identidad del actor en la auditoría.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS empleados (
                id         SERIAL      PRIMARY KEY,
                nombre     VARCHAR(120) NOT NULL,
                rol        VARCHAR(20)  NOT NULL DEFAULT 'mesero',
                pin_hash   VARCHAR(64)  NOT NULL,
                activo     BOOLEAN      NOT NULL DEFAULT TRUE,
                creado     TIMESTAMP    NOT NULL DEFAULT NOW(),
                creado_por VARCHAR(120)
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_empleados_pin ON empleados (pin_hash)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_empleados_activo ON empleados (activo)"
        ))
        # bloqueado: acceso cerrado por el cajero al terminar el turno (el PIN no entra y
        # su sesión se mata) hasta que se reactiva. Distinto de activo=FALSE (baja
        # permanente): un empleado bloqueado sigue en la nómina, solo sin acceso ahora.
        conn.execute(text(
            "ALTER TABLE empleados ADD COLUMN IF NOT EXISTS bloqueado BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        # token: secreto aleatorio para PERSISTIR la sesión del mesero en móvil (la URL
        # lleva ?mt=token). Al reconectar (bloqueo de pantalla / refresco) se restaura su
        # sesión sin volver a pedir el PIN, mientras siga activo y no bloqueado. Se rota al
        # cerrar su acceso (⏹ Salida), así una URL vieja deja de servir. Solo para mesero.
        conn.execute(text(
            "ALTER TABLE empleados ADD COLUMN IF NOT EXISTS token VARCHAR(32)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_empleados_token ON empleados (token)"
        ))

        # sesiones_empleado: marcaje de entrada/salida (clock-in/out). login_at al entrar,
        # logout_at al salir; 'activa' = en turno ahora. Snapshot de nombre/rol para que el
        # histórico sobreviva a cambios de perfil. Como mucho una sesión activa por empleado.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sesiones_empleado (
                id               SERIAL    PRIMARY KEY,
                empleado_id      INTEGER   REFERENCES empleados(id),
                nombre           VARCHAR(120),
                rol              VARCHAR(20),
                login_at         TIMESTAMP NOT NULL DEFAULT NOW(),
                logout_at        TIMESTAMP,
                ultima_actividad TIMESTAMP NOT NULL DEFAULT NOW(),
                activa           BOOLEAN   NOT NULL DEFAULT TRUE
            )
        """))
        # ultima_actividad: latido de presencia. El panel lo refresca cada ~60 s mientras
        # la pestaña sigue abierta; una sesión sin latido reciente se considera FUERA de
        # turno (el empleado cerró la pestaña sin pulsar "Salir"). Defensiva si la tabla se
        # creó en un deploy anterior sin esta columna.
        conn.execute(text(
            "ALTER TABLE sesiones_empleado ADD COLUMN IF NOT EXISTS ultima_actividad TIMESTAMP"
        ))
        conn.execute(text(
            "UPDATE sesiones_empleado SET ultima_actividad = COALESCE(ultima_actividad, login_at) "
            "WHERE ultima_actividad IS NULL"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_sesiones_emp_activa ON sesiones_empleado (activa)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_sesiones_emp_login ON sesiones_empleado (login_at DESC)"
        ))

        # auditoria: LIBRO MAYOR central de eventos críticos. Una fila por evento con
        # actor (nombre+rol), acción, entidad afectada y un JSONB 'detalle' con el diff /
        # metadatos. Lo escriben las acciones de dominio (cobrar, cancelar, descuento,
        # clock-in/out, alta/baja de empleado, movimientos de caja). Append-only.
        conn.execute(text("""
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
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_auditoria_ts ON auditoria (ts DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_auditoria_accion ON auditoria (accion)"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_auditoria_actor ON auditoria (actor_nombre)"
        ))

        # agentes_estado: latido (heartbeat) del Agente de Impresión Local. El agente hace
        # upsert de su estado en cada ciclo de polling (visto_at + profundidad de cola); el
        # panel lo lee para pintar un badge "🟢 en línea / 🔴 sin conexión". Una fila por
        # restaurante (PK = restaurante_id). Lo crea también el agente de forma defensiva.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS agentes_estado (
                restaurante_id INTEGER   PRIMARY KEY,
                hostname       VARCHAR(120),
                visto_at       TIMESTAMP NOT NULL DEFAULT NOW(),
                cola_pendiente INTEGER   NOT NULL DEFAULT 0
            )
        """))

try:
    _ensure_schema()
except Exception:
    pass  # tablas aún sin crear (deploy nuevo): el bot las creará con las columnas


def _ensure_constraints():
    """H6: invariantes de dinero como CHECK ... NOT VALID (no validan filas legadas, sí las
    nuevas inserciones/updates). En su PROPIA transacción y best-effort, para que un fallo
    aquí JAMÁS bloquee la creación de columnas esenciales de _ensure_schema(). Guardadas por
    nombre porque Postgres no soporta ADD CONSTRAINT IF NOT EXISTS."""
    with engine.begin() as conn:
        conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_pedidos_total_no_neg') THEN
                    ALTER TABLE pedidos ADD CONSTRAINT chk_pedidos_total_no_neg
                        CHECK (total >= 0) NOT VALID;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_pedidos_pagado_rango') THEN
                    ALTER TABLE pedidos ADD CONSTRAINT chk_pedidos_pagado_rango
                        CHECK (total_pagado >= 0
                               AND total_pagado <= total + COALESCE(descuento_valor, 0)) NOT VALID;
                END IF;
            END $$;
        """))


try:
    _ensure_constraints()
except Exception:
    pass  # los CHECK son una red de seguridad; si no se pueden crear, no rompemos el arranque


# ── Toasts no bloqueantes (compartidos por todas las vistas) ────────────────────
# st.toast NO sobrevive a un st.rerun() (el rerun descarta los deltas del run en
# curso) y casi toda acción del panel termina en st.rerun(). Por eso encolamos el
# toast en session_state y lo emitimos al inicio del run siguiente con
# drain_toasts() — lo llama panel.py antes de pintar la vista, y nuevo_pedido en
# su fragment (que se relanza con scope="fragment", sin re-ejecutar panel.py).
# Así el aviso flota sin desplazar el layout hacia abajo.
def flash(mensaje: str, icono: str = "✅") -> None:
    st.session_state.setdefault("_toasts", []).append((mensaje, icono))

def drain_toasts() -> None:
    for mensaje, icono in st.session_state.pop("_toasts", []):
        st.toast(mensaje, icon=icono)


# ── Formato de moneda LATAM $XX.XXX (C6) ────────────────────────────────────────
def fmt_money(valor) -> str:
    """Formatea un monto entero con punto de miles: 35000 → '35.000'.

    Devuelve solo el número; el símbolo '$' se antepone en cada vista.
    """
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


# ── Saldo / cobrado de un pedido (pagos parciales) ──────────────────────────────
# 'total_pagado' (libro acumulado de abonos) permite cuentas divididas sin destruir
# 'total'. saldo = total − total_pagado; cobrado = total_pagado (acotado a [0,total]).
# CLAVE de migración: los pedidos cobrados ANTES de existir total_pagado tienen
# total_pagado=0 pero pagado=TRUE, así que un pedido pagado se considera cobrado por
# completo SIEMPRE (saldo 0), sin importar total_pagado. Aceptan filas pandas o dict.
def _a_entero(valor) -> int:
    try:
        return int(round(float(valor)))
    except (TypeError, ValueError):
        return 0


def _es_pagado(valor) -> bool:
    """bool seguro para 'pagado': None/NaN/ausente → False (NaN es 'truthy' en Python)."""
    if valor is None:
        return False
    try:
        if pd.isna(valor):
            return False
    except (TypeError, ValueError):
        pass
    return bool(valor)


def saldo_pedido(row) -> int:
    """Saldo pendiente de un pedido = total − total_pagado, nunca negativo.
    Un pedido con pagado=TRUE siempre tiene saldo 0 (incl. los pagados pre-migración)."""
    if _es_pagado(row.get("pagado", False)):
        return 0
    return max(0, _a_entero(row.get("total", 0)) - _a_entero(row.get("total_pagado", 0)))


def cobrado_pedido(row) -> int:
    """Dinero realmente cobrado de un pedido. pagado=TRUE → total completo (cubre los
    pagados antes de total_pagado); si no, total_pagado acotado a [0, total]."""
    total = _a_entero(row.get("total", 0))
    if _es_pagado(row.get("pagado", False)):
        return total
    return max(0, min(_a_entero(row.get("total_pagado", 0)), total))


# ── Fechas en español, sin depender del locale ni de %-d (U4) ───────────────────
# %-d y %B/%b dependen de glibc (revientan en Windows) y del locale (devuelven
# meses en inglés). Formateamos a mano para que sea portable y en español.
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_MESES_ABBR = ["ene", "feb", "mar", "abr", "may", "jun", "jul",
               "ago", "sep", "oct", "nov", "dic"]


def fecha_larga(dt) -> str:
    """'13 de junio, 2026'."""
    return f"{dt.day} de {_MESES[dt.month - 1]}, {dt.year}"


def fecha_corta(dt) -> str:
    """'13 jun · 14:30'."""
    return f"{dt.day} {_MESES_ABBR[dt.month - 1]} · {dt.hour:02d}:{dt.minute:02d}"


# ── Títulos de sección con icono de línea (sustituyen a los emoji) ────────────────
# Cada encabezado .section-title puede empezar por un emoji; titulo_seccion() lo
# detecta, lo elimina del texto y lo reemplaza por el icono de trazo fino equivalente
# (mismo lenguaje monocromático que la navegación lateral, en índigo de marca). Si el
# texto no empieza por un emoji conocido, se devuelve el título tal cual, sin icono.
_SEC_ICONS = {
    "cash":    "<rect x='2.5' y='6' width='19' height='12' rx='2'/><circle cx='12' cy='12' r='2.4'/><path d='M5.5 9.5h.01'/><path d='M18.5 14.5h.01'/>",
    "bag":     "<path d='M9 4.5h6'/><path d='M9 4.5l1.6 2.6M15 4.5l-1.6 2.6'/><path d='M10.6 7.1c-3 1.5-5.1 4.8-5.1 8.1 0 3 2.4 4.5 6.5 4.5s6.5-1.5 6.5-4.5c0-3.3-2.1-6.6-5.1-8.1z'/><path d='M12 11v5.6'/><path d='M13.8 12.2a2.1 2.1 0 0 0-1.9-1c-1 0-1.8.5-1.8 1.4 0 2 3.7 1 3.7 3 0 .9-.9 1.5-2 1.5a2.1 2.1 0 0 1-1.9-1.1'/>",
    "ban":     "<circle cx='12' cy='12' r='8.5'/><path d='M6 6l12 12'/>",
    "bowl":    "<path d='M4 11h16'/><path d='M5 11a7 7 0 0 0 14 0'/><path d='M10 4.6c0 1-1 1.5-1 2.6M14 4.6c0 1-1 1.5-1 2.6'/>",
    "cutlery": "<path d='M7.5 3.5v17'/><path d='M5.3 3.5v3.5a2.2 2.2 0 0 0 4.4 0V3.5'/><path d='M16 3.5c-1.4 .6-2.2 2.6-2.2 4.8 0 1.8 .9 3 2.2 3.3v8.9'/>",
    "burger":  "<path d='M4 9.8c1.6-4.2 14.4-4.2 16 0'/><circle cx='9.4' cy='7.4' r='.5' fill='#6c5ce0' stroke='none'/><circle cx='13.6' cy='7' r='.5' fill='#6c5ce0' stroke='none'/><path d='M3.6 12.6h16.8'/><path d='M4 15.4c.6 2.3 2.4 3.1 4.4 3.1h7.2c2 0 3.8-.8 4.4-3.1'/>",
    "user":    "<circle cx='12' cy='8' r='3.4'/><path d='M5.5 19.5a6.5 6.5 0 0 1 13 0'/>",
    "users":   "<circle cx='9' cy='8.5' r='3'/><path d='M3.5 19a5.5 5.5 0 0 1 11 0'/><path d='M15.5 6.2a3 3 0 0 1 0 5.6'/><path d='M16.5 13.4a5.5 5.5 0 0 1 4 5.3'/>",
    "monitor": "<rect x='3' y='4' width='18' height='13' rx='1.7'/><path d='M9 20.5h6'/><path d='M12 17v3.5'/>",
    "moped":   "<circle cx='6.5' cy='16.5' r='2.5'/><circle cx='17.5' cy='16.5' r='2.5'/><path d='M9 16.5h6'/><path d='M15 16.5l-2-6.5h-2.5'/><path d='M13 10h4l2 4'/><path d='M4.8 12.3h3.4l1.5 2.7'/>",
    "chair":   "<path d='M7.5 12.5V6c0-1.1.9-2 2-2h5c1.1 0 2 .9 2 2v6.5'/><path d='M5.5 12.5h13'/><path d='M7.2 12.5V19.5'/><path d='M16.8 12.5V19.5'/>",
    "book":    "<path d='M6 4.5h11a1 1 0 0 1 1 1v13H7.5A1.5 1.5 0 0 0 6 20z'/><path d='M6 4.5v15.5'/><path d='M9.5 9h5M9.5 12h5'/>",
    "chart":   "<path d='M4 4v16h16'/><rect x='7' y='12' width='2.8' height='5' rx='.4'/><rect x='11.6' y='8' width='2.8' height='9' rx='.4'/><rect x='16.2' y='14' width='2.8' height='3' rx='.4'/>",
    "star":    "<path d='M12 4.2l2.3 4.7 5.2.8-3.8 3.7.9 5.2-4.6-2.5-4.6 2.5.9-5.2-3.8-3.7 5.2-.8z'/>",
    "list":    "<rect x='5' y='4.5' width='14' height='16' rx='2'/><path d='M9 3.5h6v3H9z'/><path d='M9 11h6M9 15h4'/>",
    "cup":     "<path d='M7 7.5h10l-.9 11.4a1.5 1.5 0 0 1-1.5 1.4H9.4a1.5 1.5 0 0 1-1.5-1.4z'/><path d='M5.8 7.5h12.4'/><path d='M14.5 4l-1.6 3.5'/>",
}
_EMOJI_ICON = {
    "💸": "cash", "💰": "bag", "🚫": "ban", "🍛": "bowl", "🍽": "cutlery",
    "🍔": "burger", "👤": "user", "👥": "users", "🖥": "monitor", "🛵": "moped",
    "🪑": "chair", "📒": "book", "📊": "chart", "⭐": "star", "📋": "list", "🥤": "cup",
}


def _svg_seccion(name: str) -> str:
    p = _SEC_ICONS.get(name, "")
    return (f"<svg width='17' height='17' viewBox='0 0 24 24' fill='none' "
            f"stroke='#6c5ce0' stroke-width='1.6' stroke-linecap='round' "
            f"stroke-linejoin='round' style='vertical-align:-3px;margin-right:9px'>{p}</svg>")


def titulo_seccion(texto, style: str = "") -> str:
    """HTML de un encabezado .section-title. Si el texto empieza por un emoji conocido,
    lo cambia por el icono de línea índigo equivalente; si no, lo deja sin icono."""
    s = str(texto)
    icono = ""
    if s and s[0] in _EMOJI_ICON:
        icono = _svg_seccion(_EMOJI_ICON[s[0]])
        s = s[1:].lstrip("️‍ ")  # quita selector de variación, ZWJ y espacios
    style_attr = f' style="{style}"' if style else ""
    return f'<div class="section-title"{style_attr}>{icono}{s}</div>'


# ── Menú (lectura compartida por views/menu.py y views/nuevo_pedido.py) ────────
# P1: el menú cambia poco; lo cacheamos para no consultar la BD en cada rerun
# (cada tap de +/- dispara un rerun). menu.py llama cargar_menu.clear() tras
# cada escritura para reflejar los cambios al instante.
@st.cache_data(ttl=60)
def cargar_menu():
    with engine.connect() as conn:
        resultado = conn.execute(text(
            "SELECT id, nombre, precio, activo, orden, agotado_hasta FROM menu ORDER BY orden, id"
        ))
        return pd.DataFrame(resultado.fetchall(), columns=resultado.keys())


# ── Mesas activas (para selectores; F5) ─────────────────────────────────────────
@st.cache_data(ttl=30)
def cargar_mesas_activas():
    """[{id, nombre}] de mesas activas. TTL corto; sin invalidación explícita."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, nombre FROM mesas WHERE activa = TRUE ORDER BY id"
        )).mappings().all()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════════
# OVERHAUL DEL MENÚ — lecturas compartidas (componentes, catálogo, ajustes, clientes)
# Las usan views/menu.py (admin), views/nuevo_pedido.py (POS) y views/monitor_mesas.
# La app pública (app_cliente) tiene su propia conexión y replica lo que necesita.
# ════════════════════════════════════════════════════════════════════════════════

# Grupos de opciones del Plato del Día, en el orden de los pasos de selección.
GRUPOS_COMPONENTE = ["entrada", "principio", "proteina", "acompanamiento", "bebida"]
GRUPO_LABEL = {
    "entrada":        "Entrada",
    "principio":      "Principio",
    "proteina":       "Carnes o Proteína",
    "acompanamiento": "Acompañamientos",
    "bebida":         "Bebida",
}


@st.cache_data(ttl=60)
def cargar_componentes():
    """DataFrame de TODOS los componentes del Plato del Día (admin). menu.py llama
    cargar_componentes.clear() tras cada escritura para reflejar los cambios al vuelo."""
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT id, grupo, nombre, activo, orden, agotado_hasta, stock "
            "FROM menu_componentes ORDER BY grupo, orden, id"
        ))
        return pd.DataFrame(res.fetchall(), columns=res.keys())


def disponibles(df):
    """Filtra un DataFrame de menú/componentes a lo ofrecible HOY: activo = TRUE y
    no agotado hoy (agotado_hasta NULL o < hoy). Misma regla en menú, componentes y
    carta del cliente (centraliza el filtro que vivía suelto en nuevo_pedido)."""
    if df is None or df.empty:
        return df
    hoy = pd.Timestamp(hoy_bogota())
    ag = pd.to_datetime(df["agotado_hasta"], errors="coerce")
    return df[(df["activo"] == True) & (ag.isna() | (ag < hoy))]


# ════════════════════════════════════════════════════════════════════════════════
# INVENTARIO DIARIO — control de existencias por componente y por plato
# ════════════════════════════════════════════════════════════════════════════════
# Dos modelos coexisten (ver el comentario del esquema en _ensure_schema):
#   · Plato del Día → cada micro-componente (sopa, proteína, acompañamiento…) tiene su
#     propio contador en menu_componentes.stock.
#   · A la carta / especiales / bebidas → cada plato es UNA unidad en menu.stock.
# stock NULL = SIN control (ilimitado): no se descuenta, no se oculta ni se bloquea.
# Umbral de "quedan pocos" para la alerta de cocina del mesero.
STOCK_BAJO = 3


def stock_int(valor):
    """int del stock, o None si no lleva control (NULL/NaN/no numérico). El None es
    significativo: distingue 'ilimitado' de '0 (agotado)'."""
    if valor is None:
        return None
    try:
        if pd.isna(valor):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def agotado_por_stock(valor) -> bool:
    """True solo si la opción lleva control y se quedó en 0 (o menos). Las de stock
    NULL (ilimitadas) nunca están 'agotadas por stock'."""
    s = stock_int(valor)
    return s is not None and s <= 0


def inventario_de_items(items):
    """Pura: cuenta las unidades a mover por un pedido. Devuelve (comp_qty, menu_qty):
      comp_qty → {(grupo, nombre_en_minúsculas): unidades}  (componentes del Plato del Día)
      menu_qty → {menu_id: unidades}                         (a la carta / especiales / bebidas)
    El Plato del Día descuenta su entrada/principio/proteína (si no son 'Ninguno') y cada
    acompañamiento elegido (con repetición). Los demás ítems descuentan su menu_id; además,
    un especial puede traer entrada/bebida del Plato del Día INCLUIDAS en su config, que
    también descuentan su componente. La cantidad del ítem multiplica las unidades.
    Tolerante a items legados / basura."""
    comp_qty, menu_qty = {}, {}

    def _acumular_config(cfg, cant):
        # entrada/principio/proteína/bebida descuentan UNA porción de su componente; los
        # acompañamientos, una por cada uno elegido (con repetición). Un especial solo
        # trae entrada/bebida → los demás grupos faltan en su cfg y se omiten solos.
        # Principio 'mitad y mitad' (mixto): config['principio_mixto'] = [compA, compB]
        # lleva los nombres REALES de los dos principios combinados; descuenta UNA porción
        # de CADA uno (el campo 'principio' es solo la etiqueta '½ A · ½ B' para mostrar e
        # imprimir). Sin esa lista, el principio descuenta como un componente normal.
        mixto = cfg.get("principio_mixto")
        for g in ("entrada", "principio", "proteina", "bebida"):
            if g == "principio" and mixto:
                for nom in mixto:
                    if nom:
                        k = ("principio", str(nom).strip().lower())
                        comp_qty[k] = comp_qty.get(k, 0) + cant
                continue
            v = cfg.get(g)
            if v:
                k = (g, str(v).strip().lower())
                comp_qty[k] = comp_qty.get(k, 0) + cant
        for a in (cfg.get("acompanamientos") or []):
            if a:
                k = ("acompanamiento", str(a).strip().lower())
                comp_qty[k] = comp_qty.get(k, 0) + cant

    for it in parse_items(items):
        cant = int(it.get("cantidad", 1) or 1)
        if cant <= 0:
            continue
        tipo = str(it.get("tipo") or "item").lower()
        cfg = it.get("config") or {}
        if tipo == "plato_dia":
            _acumular_config(cfg, cant)
        else:
            mid = it.get("id")
            try:
                mid = int(mid)
            except (TypeError, ValueError):
                continue
            menu_qty[mid] = menu_qty.get(mid, 0) + cant
            # Entrada/bebida del Plato del Día incluidas en un especial (u otro ítem).
            if cfg:
                _acumular_config(cfg, cant)
    return comp_qty, menu_qty


class SinStock(Exception):
    """Se intentó vender más de lo disponible de uno o más ítems CON control de stock.
    La lanza aplicar_inventario(signo=-1) cuando una opción rastreada no alcanza; al subir
    por la transacción del INSERT provoca el ROLLBACK del pedido completo (no se vende lo
    que no hay). 'faltantes' = etiquetas legibles para avisar al mesero/cliente."""
    def __init__(self, faltantes):
        self.faltantes = list(faltantes)
        super().__init__(", ".join(self.faltantes))


def aplicar_inventario(conn, items, signo: int) -> None:
    """Aplica el efecto de un pedido sobre el inventario DENTRO de una transacción dada
    ('conn' debe ser la misma del INSERT/cancelación → atómico). Solo toca filas con stock
    NO NULL (rastreadas).

    signo=+1 (reintegro al cancelar): suma sin tope; nunca falla.
    signo=-1 (descuento al crear): descuento CONDICIONAL y atómico. Cada UPDATE solo aplica
      si 'stock >= n' (WHERE … AND stock >= :n); bajo READ COMMITTED un UPDATE en espera
      re-evalúa el WHERE sobre la versión ya commiteada de la fila, así dos pedidos por la
      última porción NO pueden sobrevender. Si una opción rastreada no alcanza, acumula su
      etiqueta y, al terminar, lanza SinStock → revienta el txn (rollback del INSERT). Las
      filas se recorren ORDENADAS para fijar un orden de bloqueo determinista (sin deadlocks
      entre pedidos concurrentes)."""
    comp_qty, menu_qty = inventario_de_items(items)

    if int(signo) > 0:  # reintegro: solo suma, sin tope superior
        # sorted() también aquí → mismo orden de bloqueo que el descuento, así una
        # cancelación nunca hace deadlock con una creación/otra cancelación concurrente.
        for (grupo, nombre_l), n in sorted(comp_qty.items()):
            conn.execute(text(
                "UPDATE menu_componentes SET stock = stock + :n "
                "WHERE grupo = :g AND LOWER(nombre) = :nom AND stock IS NOT NULL"
            ), {"n": int(n), "g": grupo, "nom": nombre_l})
        for mid, n in sorted(menu_qty.items()):
            conn.execute(text(
                "UPDATE menu SET stock = stock + :n WHERE id = :id AND stock IS NOT NULL"
            ), {"n": int(n), "id": int(mid)})
        return

    # Descuento (-1): guarda atómica con detección de faltantes.
    faltan = []
    for (grupo, nombre_l), n in sorted(comp_qty.items()):
        ok = conn.execute(text(
            "UPDATE menu_componentes SET stock = stock - :n "
            "WHERE grupo = :g AND LOWER(nombre) = :nom AND stock IS NOT NULL AND stock >= :n "
            "RETURNING id"
        ), {"n": int(n), "g": grupo, "nom": nombre_l}).first()
        # ok=None puede ser 'no rastreada' (ilimitada → se ignora) o 'no alcanza'. Solo
        # es faltante si EXISTE una fila rastreada con ese nombre.
        if ok is None and conn.execute(text(
            "SELECT 1 FROM menu_componentes "
            "WHERE grupo = :g AND LOWER(nombre) = :nom AND stock IS NOT NULL"
        ), {"g": grupo, "nom": nombre_l}).first():
            faltan.append(f"{GRUPO_LABEL.get(grupo, grupo)}: {nombre_l}")

    for mid, n in sorted(menu_qty.items()):
        ok = conn.execute(text(
            "UPDATE menu SET stock = stock - :n "
            "WHERE id = :id AND stock IS NOT NULL AND stock >= :n RETURNING nombre"
        ), {"n": int(n), "id": int(mid)}).first()
        if ok is None:
            nom = conn.execute(text(
                "SELECT nombre FROM menu WHERE id = :id AND stock IS NOT NULL"
            ), {"id": int(mid)}).scalar()
            if nom is not None:
                faltan.append(str(nom))

    if faltan:
        raise SinStock(faltan)


def siguiente_num_dia(conn) -> int:
    """Número de pedido del día (1, 2, 3…), atómico y libre de carreras. Debe llamarse
    DENTRO de la transacción del INSERT del pedido.

    Usa contador_dia (una fila por fecha): el siguiente número es 1 + el MÁXIMO entre el
    contador y el MAX(num_dia) real de hoy. El GREATEST hace el contador auto-reparable: si
    algún writer fuera de esta vía (INSERT manual, script) dejó un num_dia por encima del
    contador, la siguiente asignación salta por encima y nunca colisiona. El lock de fila del
    ON CONFLICT serializa la numeración y, al tomarse al inicio del txn, fija un punto de
    serialización común para todas las creaciones de pedido (evita deadlocks de inventario)."""
    return int(conn.execute(text("""
        INSERT INTO contador_dia (fecha, n)
        VALUES (CURRENT_DATE,
                COALESCE((SELECT MAX(num_dia) FROM pedidos
                          WHERE fecha::date = CURRENT_DATE), 0) + 1)
        ON CONFLICT (fecha) DO UPDATE SET n = GREATEST(
            contador_dia.n,
            COALESCE((SELECT MAX(num_dia) FROM pedidos
                      WHERE fecha::date = CURRENT_DATE), 0)
        ) + 1
        RETURNING n
    """)).scalar_one())


def resumen_disponibilidad_componentes() -> dict:
    """Para la alerta de cocina del Plato del Día (mesero). Sobre los componentes
    ofrecibles hoy y CON control de stock, separa los agotados (0) de los que quedan
    pocos (1..STOCK_BAJO). {'agotados': [{grupo,nombre}], 'bajos': [{grupo,nombre,stock}]}
    en el orden de los grupos (entrada → principio → proteína → acompañamiento)."""
    agotados, bajos = [], []
    df = cargar_componentes()
    if df.empty:
        return {"agotados": agotados, "bajos": bajos}
    disp = disponibles(df)
    orden = {g: i for i, g in enumerate(GRUPOS_COMPONENTE)}
    filas = sorted(disp.to_dict("records"),
                   key=lambda r: (orden.get(r.get("grupo"), 99), r.get("orden", 0)))
    for r in filas:
        s = stock_int(r.get("stock"))
        if s is None:
            continue
        if s <= 0:
            agotados.append({"grupo": r["grupo"], "nombre": str(r["nombre"])})
        elif s <= STOCK_BAJO:
            bajos.append({"grupo": r["grupo"], "nombre": str(r["nombre"]), "stock": s})
    return {"agotados": agotados, "bajos": bajos}


def guardar_inventario(comp_stock: dict, menu_stock: dict) -> None:
    """Inicializa/sobrescribe los contadores de stock del día (lo llama 🍔 Menú →
    📦 Inventario al guardar el formulario). Las claves son ids; el valor es el stock
    (int) o None para dejar el ítem SIN control (ilimitado). Atómico; invalida cachés."""
    with engine.begin() as conn:
        for cid, val in (comp_stock or {}).items():
            conn.execute(text("UPDATE menu_componentes SET stock = :s WHERE id = :id"),
                         {"s": (None if val is None else int(val)), "id": int(cid)})
        for mid, val in (menu_stock or {}).items():
            conn.execute(text("UPDATE menu SET stock = :s WHERE id = :id"),
                         {"s": (None if val is None else int(val)), "id": int(mid)})
    cargar_componentes.clear()
    cargar_catalogo.clear()
    cargar_menu.clear()
    flash("Inventario del día guardado", "📦")


def componentes_activos_por_grupo() -> dict:
    """{grupo: [{id, nombre, stock}]} con SOLO los componentes ofrecibles hoy, en orden.
    'stock' = porciones que quedan (int) o None si la opción no lleva control. NO se
    ocultan las opciones agotadas (stock 0): el configurador las muestra deshabilitadas
    para no esconder nunca el Plato del Día. Lo consume el configurador (POS y cliente)."""
    out = {g: [] for g in GRUPOS_COMPONENTE}
    df = cargar_componentes()
    if df.empty:
        return out
    for _, r in disponibles(df).iterrows():
        out.setdefault(r["grupo"], []).append({
            "id": int(r["id"]), "nombre": str(r["nombre"]),
            "stock": stock_int(r.get("stock")),
        })
    return out


@st.cache_data(ttl=60)
def cargar_catalogo():
    """Catálogo 'menu' con categoría y descripción (especiales / a la carta /
    bebidas). Incluye todas las filas; el filtrado por categoría y disponibilidad lo
    hace cada vista. menu.py llama cargar_catalogo.clear() tras cada escritura."""
    with engine.connect() as conn:
        res = conn.execute(text(
            "SELECT id, nombre, precio, activo, orden, agotado_hasta, categoria, descripcion, stock "
            "FROM menu ORDER BY categoria, orden, id"
        ))
        return pd.DataFrame(res.fetchall(), columns=res.keys())


# ── Ajustes: precios planos y recargo de entrega ────────────────────────────────
@st.cache_data(ttl=60)
def cargar_ajustes() -> dict:
    """{clave: valor} de la tabla 'ajustes'. menu.py llama cargar_ajustes.clear()
    tras guardar precios/recargo."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT clave, valor FROM ajustes")).mappings().all()
    return {r["clave"]: r["valor"] for r in rows}


def ajuste_int(clave: str, default: int = 0) -> int:
    """Lee un ajuste como entero (tolerante a vacío/None/no numérico)."""
    try:
        return int(float(cargar_ajustes().get(clave, default)))
    except (TypeError, ValueError):
        return default


def precio_plato_dia() -> int:
    return ajuste_int("plato_dia_precio", 0)


def precio_especiales() -> int:
    return ajuste_int("especiales_precio", 0)


def fee_entrega() -> int:
    return ajuste_int("fee_entrega", 0)


def num_acompanamientos() -> int:
    return max(1, ajuste_int("acompanamientos_n", 3))


# ── Base de clientes (la alimenta la app pública) ───────────────────────────────
def buscar_cliente(telefono: str):
    """{telefono, nombre, direccion} del cliente, o None. Para pre-rellenar el
    formulario de identidad en pedidos repetidos."""
    tel = (telefono or "").strip()[:40]
    if not tel:
        return None
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT telefono, nombre, direccion FROM clientes WHERE telefono = :t"
        ), {"t": tel}).mappings().first()
    return dict(row) if row else None


def upsert_cliente(telefono: str, nombre: str = None, direccion: str = None) -> None:
    """Crea/actualiza el cliente por teléfono. Conserva nombre/dirección previos si
    llegan vacíos (COALESCE con EXCLUDED). Tolerante: nunca rompe el flujo del pedido."""
    tel = (telefono or "").strip()[:40]
    if not tel:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO clientes (telefono, nombre, direccion)
                VALUES (:t, :n, :d)
                ON CONFLICT (telefono) DO UPDATE SET
                    nombre      = COALESCE(EXCLUDED.nombre, clientes.nombre),
                    direccion   = COALESCE(EXCLUDED.direccion, clientes.direccion),
                    actualizado = NOW()
            """), {"t": tel, "n": (nombre or None), "d": (direccion or None)})
    except Exception:
        pass
