# -*- coding: utf-8 -*-
"""Aprovisiona la base de datos de un restaurante NUEVO a partir de un JSON de
configuración (ver restaurante.example.json).

    python scripts/provision_restaurante.py --config mi_restaurante.json \
        --database-url postgresql://usuario:clave@host:5432/bd

Qué hace, en orden:
  1. Aplica db/schema.sql (las 17 tablas + seeds guardados) vía psycopg2 — no
     necesitas psql instalado.
  2. Escribe la configuración del restaurante en 'ajustes' (identidad, saludo del
     bot, precios, recargo, métodos de pago) — UPSERT: re-correrlo actualiza.
  3. Reemplaza los grupos del Plato del Día (plato_dia_grupos) con los del JSON y
     siembra los componentes iniciales por grupo (solo si la tabla está vacía: no
     resucita opciones que el restaurante borró después).
  4. Crea las mesas (solo si no hay ninguna).
  5. Marca los seeds one-time del bot (seed_menu / seed_componentes /
     seed_pd_grupos) para que el arranque del bot NO inserte datos de ejemplo.
  6. Imprime el bloque de variables de entorno para los 3 servicios de Railway y
     el config.json del Agente de Impresión Local, listos para copiar/pegar.

Es idempotente y seguro de re-correr (las mesas y componentes solo se siembran en
vacío; el resto es UPSERT). La checklist completa está en docs/REPLICAR.md.
"""
import argparse
import json
import os
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("[FATAL] Falta psycopg2. Instala: pip install psycopg2-binary")

# La consola de Windows (cmd/PowerShell) suele usar cp1252, que no sabe encodear los
# ✔/↷ que este script imprime. Sin esto, el print truena a mitad de la transacción con
# la BD ya aprovisionada pero SIN ROLLBACK visible en pantalla (el commit del 'with conn'
# nunca llega a correr) — forzamos UTF-8 en stdout para que corra igual en cualquier shell.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_SQL = os.path.join(RAIZ, "db", "schema.sql")

SALUDO_DEFAULT = (
    "¡Hola! 👋 Bienvenido a *{nombre}*.\n\n"
    "📲 Haz tu pedido a domicilio o para llevar desde nuestra carta digital:\n"
    "{link}\n\n"
    "Elige cómo lo quieres, arma tu pedido y nosotros nos encargamos. ¡Gracias!"
)


def cargar_config(ruta: str) -> dict:
    with open(ruta, encoding="utf-8") as fh:
        cfg = json.load(fh)
    nombre = ((cfg.get("restaurante") or {}).get("nombre") or "").strip()
    if not nombre:
        sys.exit("[FATAL] El JSON debe traer restaurante.nombre")
    return cfg


def aplicar_schema(cur) -> None:
    if not os.path.exists(SCHEMA_SQL):
        sys.exit(f"[FATAL] No se encontró {SCHEMA_SQL}")
    with open(SCHEMA_SQL, encoding="utf-8") as fh:
        cur.execute(fh.read())
    print("  ✔ db/schema.sql aplicado (tablas + seeds guardados)")


def upsert_ajuste(cur, clave: str, valor) -> None:
    cur.execute(
        "INSERT INTO ajustes (clave, valor) VALUES (%s, %s) "
        "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
        (clave, str(valor)),
    )


def marcar_seed(cur, clave: str) -> None:
    """Marca un seed one-time del bot como YA hecho (ON CONFLICT DO NOTHING)."""
    cur.execute(
        "INSERT INTO ajustes (clave, valor) VALUES (%s, '1') "
        "ON CONFLICT (clave) DO NOTHING", (clave,),
    )


def escribir_ajustes(cur, cfg: dict) -> None:
    r = cfg.get("restaurante") or {}
    precios = cfg.get("precios") or {}
    upsert_ajuste(cur, "restaurante_nombre", (r.get("nombre") or "").strip())
    upsert_ajuste(cur, "restaurante_direccion", (r.get("direccion") or "").strip())
    upsert_ajuste(cur, "restaurante_telefono", (r.get("telefono") or "").strip())
    upsert_ajuste(cur, "bot_saludo", (cfg.get("bot_saludo") or SALUDO_DEFAULT))
    upsert_ajuste(cur, "moneda_simbolo", (cfg.get("moneda_simbolo") or "$"))
    upsert_ajuste(cur, "plato_dia_precio", int(precios.get("plato_dia", 18000)))
    upsert_ajuste(cur, "especiales_precio", int(precios.get("especiales", 25000)))
    upsert_ajuste(cur, "fee_entrega", int(precios.get("fee_entrega", 0)))
    mp = cfg.get("metodos_pago")
    if isinstance(mp, dict):
        upsert_ajuste(cur, "metodos_pago", json.dumps(mp, ensure_ascii=False))
    print("  ✔ ajustes escritos (identidad, saludo, precios, métodos de pago)")


def escribir_grupos(cur, cfg: dict) -> list:
    """UPSERT de los grupos del JSON. Devuelve las claves válidas (para componentes)."""
    grupos = cfg.get("grupos") or []
    claves = []
    for i, g in enumerate(grupos):
        clave = str(g.get("clave") or "").strip().lower()[:20]
        etiqueta = str(g.get("etiqueta") or clave).strip()
        if not clave:
            continue
        mn = max(0, int(g.get("min", 1)))
        mx = max(1, int(g.get("max", 1)), mn)
        cur.execute(
            "INSERT INTO plato_dia_grupos "
            "  (clave, etiqueta, orden, activo, min_sel, max_sel, permite_repetir) "
            "VALUES (%s, %s, %s, TRUE, %s, %s, %s) "
            "ON CONFLICT (clave) DO UPDATE SET etiqueta = EXCLUDED.etiqueta, "
            "  orden = EXCLUDED.orden, activo = TRUE, min_sel = EXCLUDED.min_sel, "
            "  max_sel = EXCLUDED.max_sel, permite_repetir = EXCLUDED.permite_repetir",
            (clave, etiqueta, i + 1, mn, mx, bool(g.get("repetir", False))),
        )
        claves.append(clave)
    if claves:
        # 'acompanamientos_n' legado alineado con el grupo (por si algo viejo lo lee).
        for g in grupos:
            if str(g.get("clave")) == "acompanamiento":
                upsert_ajuste(cur, "acompanamientos_n", max(1, int(g.get("max", 3))))
        print(f"  ✔ {len(claves)} grupo(s) del Plato del Día: {', '.join(claves)}")
    marcar_seed(cur, "seed_pd_grupos")
    return claves


def sembrar_componentes(cur, cfg: dict, claves: list) -> None:
    """Siembra los componentes propios del JSON. Se guía por el marcador 'seed_componentes'
    (no por 'la tabla está vacía'): schema.sql ya inserta los 12 componentes clásicos como
    parte de aplicar_schema() cuando la tabla está vacía, así que en un restaurante NUEVO
    la tabla nunca llega vacía a este punto — mirar el conteo pisaría siempre el seed
    genérico y el JSON quedaría ignorado. El marcador solo se escribe UNA vez (aquí abajo),
    así que en re-corridas posteriores no se resucita nada que el restaurante haya borrado."""
    comps = cfg.get("componentes") or {}
    cur.execute("SELECT 1 FROM ajustes WHERE clave = 'seed_componentes'")
    if cur.fetchone() is not None:
        print("  ↷ menu_componentes ya fue sembrado antes: no se toca (no se resucita nada)")
    else:
        # Primera vez real: descarta el seed clásico de schema.sql y siembra lo del JSON
        # (o deja vacío si el JSON no trae nada — el restaurante lo carga desde el panel).
        cur.execute("DELETE FROM menu_componentes")
        n = 0
        for clave, nombres in comps.items():
            clave = str(clave).strip().lower()[:20]
            if claves and clave not in claves:
                print(f"  ⚠ componentes de '{clave}' ignorados: no existe ese grupo")
                continue
            for orden, nombre in enumerate(nombres or [], start=1):
                nombre = str(nombre).strip()
                if nombre:
                    cur.execute(
                        "INSERT INTO menu_componentes (grupo, nombre, orden) "
                        "VALUES (%s, %s, %s)", (clave, nombre, orden))
                    n += 1
        if n:
            print(f"  ✔ {n} componente(s) del Plato del Día sembrados (desde el JSON)")
        else:
            print("  ↷ el JSON no trae componentes propios: quedan vacíos (cárgalos desde el panel)")
    # Con o sin componentes propios: el bot no debe insertar los de ejemplo.
    marcar_seed(cur, "seed_componentes")
    marcar_seed(cur, "seed_menu")   # tampoco los 3 platos de ejemplo


def crear_mesas(cur, cfg: dict) -> None:
    mesas = cfg.get("mesas")
    if not mesas:
        return
    cur.execute("SELECT COUNT(*) FROM mesas")
    if cur.fetchone()[0] > 0:
        print("  ↷ ya hay mesas: no se crean nuevas")
        return
    nombres = ([f"Mesa {i}" for i in range(1, int(mesas) + 1)]
               if isinstance(mesas, int) else [str(m).strip() for m in mesas if str(m).strip()])
    for nom in nombres:
        cur.execute("INSERT INTO mesas (nombre) VALUES (%s)", (nom,))
    print(f"  ✔ {len(nombres)} mesa(s) creadas")


def imprimir_deploy(cfg: dict, database_url: str) -> None:
    d = cfg.get("deploy") or {}
    rid = int(d.get("restaurante_id", 1))
    app_url = d.get("app_cliente_url") or "https://TU-APP-CLIENTE.up.railway.app"
    print("\n" + "=" * 72)
    print("VARIABLES DE ENTORNO — copia/pega en cada servicio de Railway")
    print("=" * 72)
    print("\n[whatsapp_bot]")
    print(f"DATABASE_URL={database_url}")
    print(f"APP_CLIENTE_URL={app_url}")
    print("TWILIO_ACCOUNT_SID=...   # consola de Twilio")
    print("TWILIO_AUTH_TOKEN=...")
    print("TWILIO_WHATSAPP_NUMBER=+57...")
    print("\n[dashboard_admin]")
    print(f"DATABASE_URL={database_url}")
    print(f"RESTAURANTE_ID={rid}")
    print("PANEL_PASSWORD_ADMIN=...   # elige contraseñas fuertes y distintas")
    print("PANEL_PASSWORD_CAJA=...")
    print("\n[app_cliente]")
    print(f"DATABASE_URL={database_url}")
    print(f"RESTAURANTE_ID={rid}")
    print("\n" + "=" * 72)
    print("print_agent/config.json — en el PC del restaurante (junto a la impresora)")
    print("=" * 72)
    print(json.dumps({
        "DATABASE_URL": database_url,
        "RESTAURANTE_ID": rid,
        "POLL_SECONDS": 2,
        "PRINTER_CONNECTION": {"type": "windows", "printer_name": "EPSON TM-T20III"},
    }, indent=2, ensure_ascii=False))
    print("\nChecklist completa: docs/REPLICAR.md")


def main() -> None:
    ap = argparse.ArgumentParser(description="Aprovisiona la BD de un restaurante nuevo.")
    ap.add_argument("--config", required=True, help="JSON del restaurante (ver restaurante.example.json)")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="postgresql://... (o variable de entorno DATABASE_URL)")
    ap.add_argument("--solo-env", action="store_true",
                    help="No toca la BD: solo imprime los bloques de env vars/config")
    args = ap.parse_args()

    cfg = cargar_config(args.config)
    if not args.database_url:
        sys.exit("[FATAL] Falta --database-url (o la variable DATABASE_URL)")
    url = args.database_url
    if url.startswith("postgres://"):   # Railway entrega el esquema viejo
        url = url.replace("postgres://", "postgresql://", 1)

    if not args.solo_env:
        print(f"Aprovisionando '{cfg['restaurante']['nombre']}' …")
        conn = psycopg2.connect(url)
        try:
            with conn:
                with conn.cursor() as cur:
                    aplicar_schema(cur)
                    escribir_ajustes(cur, cfg)
                    claves = escribir_grupos(cur, cfg)
                    sembrar_componentes(cur, cfg, claves)
                    crear_mesas(cur, cfg)
        finally:
            conn.close()
        print("Listo: base de datos aprovisionada.")

    imprimir_deploy(cfg, url)


if __name__ == "__main__":
    main()
