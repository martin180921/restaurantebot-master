from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import hashlib
import hmac
import json
import os
import time
import urllib.parse

import proveedor

load_dotenv()

# ── Zona horaria del negocio: Bogotá (UTC−5). El bot ESCRIBE 'fecha' y asigna el
# número de pedido del día (CURRENT_DATE). Debe coincidir con el panel y la app del
# cliente: si cada servicio guardara en una zona distinta, "pedidos de hoy" y el corte
# de caja se descuadrarían. Fijamos la zona del proceso y, abajo, la de la conexión.
os.environ.setdefault("TZ", "America/Bogota")
if hasattr(time, "tzset"):
    time.tzset()

app = FastAPI()

APP_CLIENTE_URL = os.getenv(
    "APP_CLIENTE_URL", "https://app-client-production-3486.up.railway.app"
).rstrip("/")
WA_APP_SECRET  = os.getenv("WA_APP_SECRET", "")
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
# Desactivable en local, donde no hay WA_APP_SECRET real para firmar el payload.
WA_VALIDATE = os.getenv("WA_VALIDATE", "true").lower() != "false"


# ── Config de base de datos (C7) ────────────────────────────────────────────────
def _normalizar_db_url(url):
    """Valida/normaliza DATABASE_URL: 'postgres://' → 'postgresql://'.

    Railway entrega el esquema 'postgres://' que SQLAlchemy 2.0 ya no acepta. Si
    falta, fallamos con un mensaje claro en vez de create_engine(None).
    """
    if not url:
        raise RuntimeError(
            "DATABASE_URL no está configurada. Define la variable de entorno con "
            "la cadena de conexión de PostgreSQL antes de arrancar el bot."
        )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = _normalizar_db_url(os.getenv("DATABASE_URL"))
# C5: pre_ping descarta conexiones muertas y recycle las renueva antes del
# timeout del servidor (Railway corta las conexiones inactivas).
# P-POOL: techo EXPLÍCITO de conexiones por proceso (igual que el panel y app_cliente).
# Las tres patas comparten una sola Postgres pequeña; sin tope, cada proceso abriría
# hasta 15 conexiones y una avalancha agotaría el límite del plan. 5+5=10/proceso.
engine = create_engine(
    DATABASE_URL,
    # Zona horaria de la sesión de la BD: NOW()/CURRENT_DATE en hora de Bogotá.
    connect_args={"options": "-c timezone=America/Bogota"},
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=5,
    max_overflow=5,
    pool_timeout=10,
)

# ── Inicializar tablas ─────────────────────────────────────────────────────────
# El bot es el dueño del esquema: crea/actualiza las tablas en cada arranque.
def init_db():
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sesiones (
                numero      VARCHAR(50) PRIMARY KEY,
                estado      VARCHAR(30) NOT NULL DEFAULT 'inicio',
                carrito     TEXT        NOT NULL DEFAULT '[]',
                actualizado TIMESTAMP   NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS menu (
                id            SERIAL PRIMARY KEY,
                nombre        VARCHAR(100) NOT NULL,
                precio        INTEGER NOT NULL,
                activo        BOOLEAN NOT NULL DEFAULT TRUE,
                orden         INTEGER NOT NULL DEFAULT 0,
                agotado_hasta DATE
            )
        """))
        # F6: "86 / agotado hoy" — disponible de nuevo automáticamente al día siguiente
        conn.execute(text(
            "ALTER TABLE menu ADD COLUMN IF NOT EXISTS agotado_hasta DATE"
        ))
        # ── Marca persistente de "ya sembrado" (one-time seed) ─────────────────
        # Antes el menú de ejemplo se reinsertaba CADA vez que la tabla quedaba
        # vacía (COUNT==0) en un reinicio. Eso "resucitaba" los platos de ejemplo
        # cada vez que el restaurante borraba toda una sección y el bot se reiniciaba
        # (de ahí que los borrados "reaparecieran solos"). Ahora la semilla corre
        # SOLO la primerísima vez: dejamos una marca en 'ajustes' y nunca volvemos a
        # sembrar, aunque el restaurante deje una sección vacía a propósito.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ajustes (
                clave VARCHAR(50) PRIMARY KEY,
                valor TEXT        NOT NULL
            )
        """))

        def _ya_sembrado(clave: str) -> bool:
            return conn.execute(
                text("SELECT 1 FROM ajustes WHERE clave = :k"), {"k": clave}
            ).scalar() is not None

        def _marcar_sembrado(clave: str):
            conn.execute(text(
                "INSERT INTO ajustes (clave, valor) VALUES (:k, '1') "
                "ON CONFLICT (clave) DO NOTHING"
            ), {"k": clave})

        if not _ya_sembrado('seed_menu'):
            if conn.execute(text("SELECT COUNT(*) FROM menu")).scalar() == 0:
                conn.execute(text("""
                    INSERT INTO menu (nombre, precio, activo, orden) VALUES
                    ('Hamburguesa', 25000, TRUE, 1),
                    ('Pizza',       35000, TRUE, 2),
                    ('Ensalada',    18000, TRUE, 3)
                """))
            _marcar_sembrado('seed_menu')

        # ── Mesas: gestión dinámica de mesas del restaurante ───────────────────
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mesas (
                id      SERIAL PRIMARY KEY,
                nombre  VARCHAR(50)  NOT NULL,
                activa  BOOLEAN      NOT NULL DEFAULT TRUE,
                creada  TIMESTAMP    NOT NULL DEFAULT NOW()
            )
        """))

        # ── Pedidos: creado explícitamente aquí (antes no lo creaba ningún código)
        conn.execute(text("""
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
                total_pagado        INTEGER      NOT NULL DEFAULT 0
            )
        """))
        # Actualiza tablas 'pedidos' preexistentes que aún no tienen estas columnas
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS mesa_id INTEGER REFERENCES mesas(id)"
        ))
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS motivo_cancelacion TEXT"
        ))
        # cancelled_at: hora de la cancelación, para el historial del panel
        # (Caja → Cancelaciones, agrupado por día). Los cancelados previos quedan NULL.
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP"
        ))
        # pagado: dimensión de cobro independiente del estado de cocina (el monitor
        # de mesas marca 'pagado' sin tocar el flujo pendiente→…→entregado).
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pagado BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        # total_pagado: monto abonado hasta ahora (libro acumulado para pagos
        # parciales / cuentas divididas). saldo = total − total_pagado. Cuando cubre
        # el total, el cobro marca además pagado=TRUE. Los pedidos ya pagados antes
        # de esta columna tienen total_pagado=0 pero pagado=TRUE → el panel los trata
        # como cobrados por completo (ver db.saldo_pedido/cobrado_pedido).
        conn.execute(text(
            "ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS total_pagado INTEGER NOT NULL DEFAULT 0"
        ))

        # ── Pagos: libro de abonos (uno por pedido tocado en cada cobro) ────────
        # Detalle del cobro que 'pedidos.total_pagado' resume: método (efectivo /
        # transferencia) y hora REAL del pago. Fuente para el desglose de caja por
        # método; total_pagado se mantiene denormalizado para el saldo en cada render.
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
        # (n.º de transacción). NULL en efectivo. Las llena el panel al cobrar.
        conn.execute(text("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS submetodo VARCHAR(20)"))
        conn.execute(text("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS comprobante VARCHAR(60)"))

        # ── Turnos de caja: arqueo (apertura con fondo, cierre con conteo) ──────
        # Esperado = fondo_inicial + efectivo cobrado entre abierto y cerrado (de
        # 'pagos'); diferencia = efectivo_contado − esperado. Las transferencias no
        # entran a la caja física.
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

        # ════════════════════════════════════════════════════════════════════
        # OVERHAUL DEL MENÚ (aditivo): secciones (Plato del Día / Especiales /
        # A la carta / Bebidas), opciones del Plato del Día, ajustes de precios y
        # recargo de entrega, base de clientes y metadatos de entrega en pedidos.
        # NADA se elimina: el menú y los pedidos existentes siguen funcionando
        # (categoria default 'a_la_carta', tipo_entrega NULL = pedido de mesa).
        # ════════════════════════════════════════════════════════════════════

        # Componentes del Plato del Día. Cada fila es una opción toggleable mapeada
        # a un grupo (entrada/principio/proteina/acompanamiento). Las sopas son
        # filas 'entrada' que el restaurante activa/desactiva por día y se listan
        # como pares de Fruta/Huevo. Reusa el patrón "86" (agotado_hasta).
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
        # Semilla SOLO la primerísima vez (marca persistente en 'ajustes'): nunca se
        # vuelve a sembrar, aunque el restaurante borre todas las opciones a propósito.
        # Antes corría con COUNT==0 y "resucitaba" las opciones borradas en cada reinicio.
        if not _ya_sembrado('seed_componentes'):
            if conn.execute(text("SELECT COUNT(*) FROM menu_componentes")).scalar() == 0:
                conn.execute(text("""
                    INSERT INTO menu_componentes (grupo, nombre, orden) VALUES
                    ('entrada', 'Fruta', 1), ('entrada', 'Huevo', 2), ('entrada', 'Sopa del día', 3),
                    ('principio', 'Frijol', 1), ('principio', 'Lenteja', 2),
                    ('proteina', 'Res', 1), ('proteina', 'Cerdo', 2), ('proteina', 'Pechuga', 3),
                    ('acompanamiento', 'Arroz', 1), ('acompanamiento', 'Maduro', 2),
                    ('acompanamiento', 'Papa', 3), ('acompanamiento', 'Ensalada', 4),
                    ('bebida', 'Limonada', 1), ('bebida', 'Jugo del día', 2)
                """))
            _marcar_sembrado('seed_componentes')

        # Categoría + descripción del catálogo 'menu': especiales (con resumen de
        # lo que incluyen), a la carta y bebidas. Las filas existentes quedan como
        # 'a_la_carta' sin tocar su precio ni su estado.
        conn.execute(text(
            "ALTER TABLE menu ADD COLUMN IF NOT EXISTS categoria VARCHAR(20) NOT NULL DEFAULT 'a_la_carta'"
        ))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS descripcion TEXT"))

        # Inventario diario (aditivo): 'stock' por componente del Plato del Día y por
        # plato/bebida a la carta. NULL = sin control (ilimitado). El panel lo descuenta
        # al crear el pedido y lo reintegra si se cancela antes de 'listo'; el admin fija
        # las cantidades cada mañana en 🍔 Menú → 📦 Inventario.
        conn.execute(text("ALTER TABLE menu_componentes ADD COLUMN IF NOT EXISTS stock INTEGER"))
        conn.execute(text("ALTER TABLE menu ADD COLUMN IF NOT EXISTS stock INTEGER"))

        # Ajustes clave/valor: precios planos (Plato del Día y Especiales), recargo
        # de entrega (Domicilio/Para Llevar) y nº de acompañamientos a elegir.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ajustes (
                clave VARCHAR(50) PRIMARY KEY,
                valor TEXT        NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO ajustes (clave, valor) VALUES
            ('plato_dia_precio',  '18000'),
            ('especiales_precio', '25000'),
            ('fee_entrega',       '4000'),
            ('acompanamientos_n', '3')
            ON CONFLICT (clave) DO NOTHING
        """))

        # Identidad/branding del restaurante y métodos de pago (replicabilidad):
        # los defaults son los valores que antes estaban quemados en el código.
        # Editables desde el panel (🍔 Menú → ⚙️ Ajustes). 'bot_saludo' es una
        # plantilla con {nombre} y {link}; 'metodos_pago' es JSON con el switch de
        # efectivo y el mapa clave→etiqueta de las transferencias aceptadas.
        for _k, _v in {
            "restaurante_nombre":    "RestauranteBOT",
            "restaurante_direccion": "",
            "restaurante_telefono":  "",
            "bot_saludo": (
                "¡Hola! 👋 Bienvenido a *{nombre}*.\n\n"
                "📲 Haz tu pedido a domicilio o para llevar desde nuestra carta digital:\n"
                "{link}\n\n"
                "Elige cómo lo quieres, arma tu pedido y nosotros nos encargamos. "
                "¡Gracias!"
            ),
            "metodos_pago": (
                '{"efectivo": true, "transferencia": '
                '{"nequi": "Nequi", "daviplata": "Daviplata", "breb": "Bre-B"}}'
            ),
            "moneda_simbolo": "$",
        }.items():
            conn.execute(text(
                "INSERT INTO ajustes (clave, valor) VALUES (:k, :v) "
                "ON CONFLICT (clave) DO NOTHING"
            ), {"k": _k, "v": _v})

        # ── Grupos del Plato del Día como DATOS (replicabilidad) ───────────────
        # Antes los grupos (entrada/principio/proteina/acompanamiento/bebida)
        # estaban quemados en el código; ahora cada restaurante define los suyos.
        # 'clave' es el mismo valor de menu_componentes.grupo (sin FK dura: los
        # componentes existentes siguen mapeando solos). min_sel=0 → grupo
        # opcional; max_sel>1 → multi-selección; permite_repetir → se puede pedir
        # 2x la misma opción (acompañamientos).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS plato_dia_grupos (
                id              SERIAL  PRIMARY KEY,
                clave           TEXT    UNIQUE NOT NULL,
                etiqueta        TEXT    NOT NULL,
                orden           INTEGER NOT NULL DEFAULT 0,
                activo          BOOLEAN NOT NULL DEFAULT TRUE,
                min_sel         INTEGER NOT NULL DEFAULT 1,
                max_sel         INTEGER NOT NULL DEFAULT 1,
                permite_repetir BOOLEAN NOT NULL DEFAULT FALSE
            )
        """))
        # Seed one-time con la semántica EXACTA del código anterior: radio único
        # en entrada/principio/proteína, bebida opcional, y N acompañamientos
        # tomados del ajuste 'acompanamientos_n' VIGENTE — así la migración
        # respeta lo que cada restaurante ya tenía configurado.
        if not _ya_sembrado('seed_pd_grupos'):
            _n_acomp = conn.execute(text(
                "SELECT valor FROM ajustes WHERE clave = 'acompanamientos_n'"
            )).scalar()
            try:
                _n_acomp = max(1, int(_n_acomp))
            except (TypeError, ValueError):
                _n_acomp = 3
            conn.execute(text("""
                INSERT INTO plato_dia_grupos
                    (clave, etiqueta, orden, min_sel, max_sel, permite_repetir)
                VALUES
                    ('entrada',        'Entrada',           1, 1,  1,  FALSE),
                    ('principio',      'Principio',         2, 1,  1,  FALSE),
                    ('proteina',       'Carnes o Proteína', 3, 1,  1,  FALSE),
                    ('acompanamiento', 'Acompañamientos',   4, :n, :n, TRUE),
                    ('bebida',         'Bebida',            5, 0,  1,  FALSE)
                ON CONFLICT (clave) DO NOTHING
            """), {"n": _n_acomp})
            _marcar_sembrado('seed_pd_grupos')

        # ── Categorías del catálogo como DATOS (replicabilidad) ────────────────
        # Antes las categorías (especial/a_la_carta/adicional/bebida) estaban quemadas
        # en el código; ahora cada restaurante puede agregar las suyas (Desayunos,
        # Postres…) desde el panel. 'clave' es el mismo valor de menu.categoria (sin FK
        # dura). disponible_desde/hasta acotan una categoría a un horario, solo aplicado
        # en la carta digital del cliente (el panel/POS siempre ven todo lo activo).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS categorias (
                id                SERIAL      PRIMARY KEY,
                clave             VARCHAR(20) UNIQUE NOT NULL,
                etiqueta          TEXT        NOT NULL,
                emoji             VARCHAR(8)  NOT NULL DEFAULT '',
                orden             INTEGER     NOT NULL DEFAULT 0,
                activo            BOOLEAN     NOT NULL DEFAULT TRUE,
                disponible_desde  TIME,
                disponible_hasta  TIME,
                extras_grupos     TEXT        NOT NULL DEFAULT ''
            )
        """))
        # El horario va además en ALTER aparte: una base que ya tenga la tabla de una build
        # anterior NO re-ejecuta el CREATE y se quedaría sin estas columnas.
        conn.execute(text("ALTER TABLE categorias ADD COLUMN IF NOT EXISTS disponible_desde TIME"))
        conn.execute(text("ALTER TABLE categorias ADD COLUMN IF NOT EXISTS disponible_hasta TIME"))
        # extras_grupos: claves de grupo del Plato del Día (coma-separadas) que la
        # categoría ofrece INCLUIDAS sin costo (p. ej. 'entrada,bebida'). '' = catálogo
        # simple; default vacío a propósito, ningún restaurante nuevo nace con extras
        # encendidos. Mismo motivo de ALTER aparte que las columnas de horario.
        conn.execute(text(
            "ALTER TABLE categorias ADD COLUMN IF NOT EXISTS extras_grupos TEXT NOT NULL DEFAULT ''"))
        # Seed one-time con las 4 categorías clásicas del código anterior.
        if not _ya_sembrado('seed_categorias'):
            conn.execute(text("""
                INSERT INTO categorias (clave, etiqueta, emoji, orden) VALUES
                    ('especial',   'Especiales',  '⭐', 1),
                    ('a_la_carta', 'A la carta',  '📋', 2),
                    ('adicional',  'Adicionales', '🍟', 3),
                    ('bebida',     'Bebidas',     '🥤', 4)
                ON CONFLICT (clave) DO NOTHING
            """))
            _marcar_sembrado('seed_categorias')

        # Base de clientes: la alimenta la app pública (tel como identidad).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clientes (
                telefono    VARCHAR(40) PRIMARY KEY,
                nombre      VARCHAR(120),
                direccion   TEXT,
                creado      TIMESTAMP NOT NULL DEFAULT NOW(),
                actualizado TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))

        # Metadatos de entrega en 'pedidos' (web: domicilio/para_llevar; los de
        # mesa dejan tipo_entrega NULL). 'fee' es el recargo plano de entrega;
        # 'paga_con' el efectivo con el que paga el cliente (para el cambio).
        for _col, _ddl in [
            ("tipo_entrega",     "VARCHAR(15)"),
            ("cliente_nombre",   "VARCHAR(120)"),
            ("cliente_telefono", "VARCHAR(40)"),
            ("direccion",        "TEXT"),
            ("metodo_pago",      "VARCHAR(20)"),
            ("paga_con",         "INTEGER"),
            ("fee",              "INTEGER NOT NULL DEFAULT 0"),
            ("nota_general",     "TEXT"),
            ("mesero",           "VARCHAR(120)"),  # quién tomó el pedido (NULL en pedidos del cliente)
            ("idem_key",         "VARCHAR(40)"),   # H3: clave de idempotencia anti-duplicado
        ]:
            conn.execute(text(f"ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS {_col} {_ddl}"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pedidos_tipo_entrega ON pedidos (tipo_entrega)"
        ))
        # H3: árbitro de ON CONFLICT (idem_key) para crear pedidos de forma idempotente.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_pedidos_idem ON pedidos (idem_key)"
        ))

        # ════════════════════════════════════════════════════════════════════
        # CLOUD API DE META (aditivo): eventos ya procesados (anti-duplicado),
        # estado de pausa por contacto y bitácora cruda de webhooks.
        # ════════════════════════════════════════════════════════════════════
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS wa_eventos (
                wamid   VARCHAR(120) PRIMARY KEY,
                tipo    VARCHAR(30)  NOT NULL,
                visto   TIMESTAMP    NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_wa_eventos_visto ON wa_eventos (visto)"
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS wa_contactos (
                telefono       VARCHAR(40) PRIMARY KEY,
                pausado_hasta  TIMESTAMP,
                pausa_motivo   VARCHAR(30),
                ultimo_saludo  TIMESTAMP,
                actualizado    TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS wa_log (
                id      SERIAL PRIMARY KEY,
                campo   VARCHAR(40),
                payload TEXT,
                creado  TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_wa_log_creado ON wa_log (creado)"
        ))

        for _k, _v in {
            "bot_activo":               "1",
            "bot_pausa_min":            "45",
            "bot_saludo_cooldown_min":  "180",
        }.items():
            conn.execute(text(
                "INSERT INTO ajustes (clave, valor) VALUES (:k, :v) "
                "ON CONFLICT (clave) DO NOTHING"
            ), {"k": _k, "v": _v})

        # Poda: sin esto wa_eventos y wa_log crecen sin límite. Corre en cada
        # arranque; con eso basta, no hace falta un cron aparte.
        conn.execute(text(
            "DELETE FROM wa_eventos WHERE visto < NOW() - INTERVAL '30 days'"
        ))
        conn.execute(text(
            "DELETE FROM wa_log WHERE creado < NOW() - INTERVAL '30 days'"
        ))

        conn.commit()

init_db()


# ── Webhook ────────────────────────────────────────────────────────────────────
# El bot ya no procesa pedidos por texto. A cualquier mensaje responde con el
# enlace a la carta digital (app_cliente), donde el cliente elige su mesa, arma
# el carrito y lo envía directo a la cocina.

@app.get("/webhook")
async def verificar_webhook(request: Request):
    """Handshake de alta de Meta. El challenge se devuelve como texto plano,
    sin comillas: si vuelve como JSON, Meta rechaza la URL sin decir por qué.
    """
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == WA_VERIFY_TOKEN
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("", status_code=403)


@app.post("/webhook")
async def recibir_webhook(request: Request, background_tasks: BackgroundTasks):
    # Cuerpo crudo ANTES de parsear nada: la firma se calcula sobre estos bytes
    # tal cual llegaron. Si se parsea el JSON y se vuelve a serializar para
    # firmar, la firma nunca cuadra.
    cuerpo_crudo = await request.body()

    if WA_VALIDATE:
        firma = request.headers.get("X-Hub-Signature-256", "")
        esperada = "sha256=" + hmac.new(
            WA_APP_SECRET.encode(), cuerpo_crudo, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(firma, esperada):
            return PlainTextResponse("", status_code=403)

    try:
        payload = json.loads(cuerpo_crudo)
    except ValueError:
        # JSON malformado: no es nuestro problema reintentarlo, así que 200.
        return {"status": "ok"}

    # El webhook nunca debe fallar ni tardar: todo el trabajo real va en
    # segundo plano. Si Meta recibe error o timeout, reintenta, y los
    # reintentos generan saludos duplicados.
    background_tasks.add_task(_procesar, payload)
    return {"status": "ok"}


def _guardar_log(payload):
    """Bitácora cruda del webhook completo. Tolerante a fallos: un error de
    log jamás debe cortar el procesamiento de los eventos.
    """
    try:
        campos = []
        for entrada in payload.get("entry") or []:
            for change in entrada.get("changes") or []:
                campo = change.get("field")
                if campo and campo not in campos:
                    campos.append(campo)
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO wa_log (campo, payload) VALUES (:campo, :payload)"
            ), {"campo": ", ".join(campos) or None, "payload": json.dumps(payload)})
            conn.commit()
    except Exception as e:
        print(f"[WARN] No se pudo guardar en wa_log: {e}")


def _reclamar_evento(ev: proveedor.Evento) -> bool:
    """Reclama el wamid en wa_eventos. True si es la primera vez que se ve
    (queda reclamado); False si ya se procesó (reintento de Meta). Los
    eventos sin wamid propio ('contacto', 'cuenta') no tienen con qué
    deduplicar y se tratan siempre como nuevos.
    """
    if not ev.wamid:
        return True
    with engine.connect() as conn:
        fila = conn.execute(text(
            "INSERT INTO wa_eventos (wamid, tipo) VALUES (:wamid, :tipo) "
            "ON CONFLICT (wamid) DO NOTHING RETURNING wamid"
        ), {"wamid": ev.wamid, "tipo": ev.tipo}).fetchone()
        conn.commit()
    return fila is not None


def _procesar(payload):
    """Corre en BackgroundTasks, fuera del ciclo de request/response."""
    _guardar_log(payload)

    for ev in proveedor.parsear_webhook(payload):
        if not _reclamar_evento(ev):
            continue
        if ev.tipo == "entrante":
            _atender_entrante(ev)
        elif ev.tipo == "echo":
            _pausar_por_humano(ev)
        elif ev.tipo == "contacto":
            _upsert_contacto(ev)
        elif ev.tipo == "cuenta":
            _alerta_cuenta(ev)
        # 'estado': nada más que hacer, ya quedó en wa_log.


def _atender_entrante(ev: proveedor.Evento):
    """El bot no conversa: a cualquier mensaje responde el saludo + enlace a la
    carta digital, salvo que el interruptor esté apagado, alguien del local ya
    esté atendiendo a mano, o ya se haya saludado hace poco.
    """
    if ajuste_int("bot_activo", 1) != 1:
        return

    cooldown_min = ajuste_int("bot_saludo_cooldown_min", 180)
    with engine.connect() as conn:
        fila = conn.execute(text("""
            SELECT pausado_hasta > NOW() AS pausado,
                   ultimo_saludo > NOW() - (:cooldown || ' minutes')::interval AS en_cooldown
            FROM wa_contactos WHERE telefono = :telefono
        """), {"telefono": ev.telefono, "cooldown": cooldown_min}).fetchone()

    # Sin fila (contacto nuevo) o columnas NULL (nunca pausado / nunca saludado):
    # las comparaciones anteriores devuelven NULL, que Python trata como falsy.
    if fila and (fila[0] or fila[1]):
        return

    if not proveedor.enviar_texto(ev.telefono, mensaje_bienvenida(ev.telefono)):
        # No se marca ultimo_saludo: el próximo mensaje del cliente reintenta solo.
        return

    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO wa_contactos (telefono, ultimo_saludo, actualizado)
            VALUES (:telefono, NOW(), NOW())
            ON CONFLICT (telefono) DO UPDATE SET
                ultimo_saludo = NOW(), actualizado = NOW()
        """), {"telefono": ev.telefono})
        conn.commit()


def _pausar_por_humano(ev: proveedor.Evento):
    """Evento 'echo': alguien del local escribió desde su celular o WhatsApp
    Web. El bot se aparta por un rato; no responde nada, no notifica nada.
    """
    minutos = ajuste_int("bot_pausa_min", 45)
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO wa_contactos (telefono, pausado_hasta, pausa_motivo, actualizado)
            VALUES (:telefono, NOW() + (:minutos || ' minutes')::interval, 'humano', NOW())
            ON CONFLICT (telefono) DO UPDATE SET
                pausado_hasta = NOW() + (:minutos || ' minutes')::interval,
                pausa_motivo = 'humano',
                actualizado = NOW()
        """), {"telefono": ev.telefono, "minutos": minutos})
        conn.commit()


def _upsert_contacto(ev: proveedor.Evento):
    print(f"[TODO] _upsert_contacto: {ev.telefono}")


def _alerta_cuenta(ev: proveedor.Evento):
    print(f"[TODO] _alerta_cuenta: {ev.crudo}")


# ── Ajustes enteros con cache (bot_activo, bot_pausa_min, ...) ──────────────────
_ajustes_int_cache = {}  # clave -> (valor: int, ts: float)


def ajuste_int(clave: str, default: int) -> int:
    """Entero desde 'ajustes', con cache de 60s por clave y fallback a
    'default' si falla la lectura o el valor guardado no es un entero válido.
    El bot nunca debe dejar de funcionar por un ajuste mal puesto.
    """
    cache = _ajustes_int_cache.get(clave)
    if cache and time.time() - cache[1] <= 60:
        return cache[0]

    valor = default
    try:
        with engine.connect() as conn:
            fila = conn.execute(text(
                "SELECT valor FROM ajustes WHERE clave = :clave"
            ), {"clave": clave}).fetchone()
        if fila and fila[0] is not None:
            valor = int(fila[0])
    except Exception as e:
        print(f"[WARN] No se pudo leer el ajuste '{clave}': {e}")
        valor = default

    _ajustes_int_cache[clave] = (valor, time.time())
    return valor


# ── Branding configurable (nombre y saludo viven en 'ajustes') ─────────────────
# El webhook NUNCA debe dejar de responder por culpa de la BD: si la lectura
# falla se usa el texto por defecto (el que antes estaba quemado). Cache en
# memoria de 60s para no golpear la BD en cada mensaje.
_NOMBRE_DEFAULT = "RestauranteBOT"
_SALUDO_DEFAULT = (
    "¡Hola! 👋 Bienvenido a *{nombre}*.\n\n"
    "📲 Haz tu pedido a domicilio o para llevar desde nuestra carta digital:\n"
    "{link}\n\n"
    "Elige cómo lo quieres, arma tu pedido y nosotros nos encargamos. "
    "¡Gracias!"
)
_branding_cache = {"ts": 0.0, "nombre": _NOMBRE_DEFAULT, "saludo": _SALUDO_DEFAULT}


def _branding():
    """(nombre, saludo) desde 'ajustes', con cache de 60s y fallback quemado."""
    if time.time() - _branding_cache["ts"] > 60:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT clave, valor FROM ajustes "
                    "WHERE clave IN ('restaurante_nombre', 'bot_saludo')"
                )).fetchall()
            d = {r[0]: r[1] for r in rows}
            nombre = (d.get("restaurante_nombre") or "").strip()
            _branding_cache["nombre"] = nombre or _NOMBRE_DEFAULT
            _branding_cache["saludo"] = d.get("bot_saludo") or _SALUDO_DEFAULT
        except Exception as e:
            print(f"[WARN] No se pudo leer el branding de 'ajustes': {e}")
        # ts se actualiza también si falló: reintenta en 60s, no en cada mensaje.
        _branding_cache["ts"] = time.time()
    return _branding_cache["nombre"], _branding_cache["saludo"]


def mensaje_bienvenida(numero: str) -> str:
    tel  = numero.replace("whatsapp:", "").strip()
    link = f"{APP_CLIENTE_URL}/?tel={urllib.parse.quote(tel)}"
    nombre, saludo = _branding()
    try:
        return saludo.format(nombre=nombre, link=link)
    except (KeyError, IndexError, ValueError):
        # Plantilla malformada guardada desde el panel (llaves sueltas, campos
        # desconocidos): degradar al saludo por defecto antes que no responder.
        return _SALUDO_DEFAULT.format(nombre=nombre, link=link)
