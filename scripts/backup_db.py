# -*- coding: utf-8 -*-
"""Respaldo LÓGICO de la base de datos de un restaurante — puro Python (solo psycopg2).

    python scripts/backup_db.py --database-url postgresql://... [--nombre "Doña Marta"] \
        [--out-dir backups] [--retention-days 14] [--keep 7]

Por qué puro Python y no pg_dump:
  - El servidor de Railway corre Postgres 18. Un pg_dump de una versión MENOR se niega a
    respaldar un servidor de versión mayor, así que exigiría instalar exactamente pg_dump 18
    en cada PC — fricción que no queremos en un piloto.
  - psycopg2 ya está instalado en todos los servicios (bot, panel, agente), así que este
    script corre en cualquier lado sin instalar nada.

Qué produce:
  Un archivo comprimido  backup_[<nombre>_]<bd>_<AAAAMMDD_HHMMSS>.sql.gz  con:
    - cabecera (fecha, versión del server, nº de tablas),
    - UN SOLO 'TRUNCATE' que cubre todas las tablas (Postgres exige truncar en una sola
      sentencia las tablas relacionadas por FK; un TRUNCATE por tabla revienta apenas hay
      una referenciada, p. ej. cierres_caja <- movimientos_caja),
    - los datos de TODAS las tablas base de 'public' en formato COPY (texto de Postgres),
    - los setval de cada secuencia para que los SERIAL sigan donde iban.
  El formato es restaurable con scripts/restore_db.py (y también es legible con psql).

Todos los restaurantes usan el mismo nombre de base en Railway ("railway"), así que sin
--nombre los archivos de 5 restaurantes serían indistinguibles si algún día se centralizan en
una sola carpeta. Con --nombre, el archivo lleva ese nombre y la poda por retención agrupa
por él (no borra de más los backups de un restaurante por culpa de otro más activo).

Es SOLO LECTURA sobre la base (COPY ... TO STDOUT, transacción REPEATABLE READ: todas las
tablas se leen del mismo instante congelado, no una por una en momentos distintos) — seguro
de correr en producción en cualquier momento. Al terminar poda los respaldos viejos del
directorio según --retention-days (conservando siempre al menos --keep por grupo).
"""
import argparse
import gzip
import os
import re
import sys
from datetime import datetime, timedelta

try:
    import psycopg2
except ImportError:
    sys.exit("[FATAL] Falta psycopg2. Instala: pip install psycopg2-binary")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# 'grupo' es todo lo que va entre 'backup_' y el timestamp: "<bd>" o "<nombre>_<bd>" si se
# usó --nombre. La poda agrupa por esto, así que no hace falta separar nombre de bd.
NOMBRE_RE = re.compile(r"^backup_(?P<grupo>.+)_(?P<ts>\d{8}_\d{6})\.sql\.gz$")


def _conectar(url: str):
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def _tablas_base(cur) -> list:
    """Tablas base de 'public' (excluye vistas). Orden alfabético estable."""
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    return [r[0] for r in cur.fetchall()]


def _columnas(cur, tabla: str) -> list:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position", (tabla,))
    return [r[0] for r in cur.fetchall()]


def _slug(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", texto) or "x"


def _nombre_bd(url: str) -> str:
    """Nombre de la BD para el archivo (última parte de la ruta, sin query)."""
    base = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return _slug(base)


def respaldar(url: str, out_dir: str, nombre: str | None = None) -> tuple:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bd = _nombre_bd(url)
    grupo = f"{_slug(nombre)}_{bd}" if nombre else bd
    destino = os.path.join(out_dir, f"backup_{grupo}_{ts}.sql.gz")

    conn = _conectar(url)
    # REPEATABLE READ: todas las tablas se leen del mismo instante congelado. Sin esto (solo
    # readonly, sin fijar el nivel de aislamiento) cada tabla se lee en un momento distinto —
    # si el backup corre con el restaurante operando, podría capturar un pago cuyo pedido
    # llegó DESPUÉS de copiar 'pedidos': un respaldo internamente inconsistente.
    conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
    total_filas = 0
    resumen = []
    try:
        with conn.cursor() as cur:
            cur.execute("SET client_encoding = 'UTF8'")
            cur.execute("SHOW server_version")
            server_version = cur.fetchone()[0]
            tablas = _tablas_base(cur)
            tablas_cols = [(t, _columnas(cur, t)) for t in tablas]
            tablas_cols = [(t, c) for t, c in tablas_cols if c]

            with gzip.open(destino, "wb") as gz:
                def w(texto: str):
                    gz.write(texto.encode("utf-8"))

                w(f"-- olo backup lógico\n-- generado: {datetime.now().isoformat()}\n")
                w(f"-- server_version: {server_version}\n-- tablas: {len(tablas_cols)}\n\n")
                # Durante la restauración: ignora FKs/triggers para no pelear con el orden.
                w("SET session_replication_role = replica;\n\n")

                # UNA sola sentencia para todas las tablas (ver docstring del módulo).
                objetivos = ", ".join(f'public."{t}"' for t, _ in tablas_cols)
                w(f"TRUNCATE {objetivos};\n\n")

                for tabla, cols in tablas_cols:
                    cur.execute(f'SELECT COUNT(*) FROM public."{tabla}"')
                    n = cur.fetchone()[0]
                    total_filas += n
                    resumen.append((tabla, n))
                    cols_sql = ", ".join(f'"{c}"' for c in cols)
                    w(f'COPY public."{tabla}" ({cols_sql}) FROM stdin;\n')
                    gz.flush()
                    cur.copy_expert(
                        f'COPY public."{tabla}" ({cols_sql}) TO STDOUT WITH (FORMAT text)', gz)
                    w("\\.\n\n")

                # setval de cada secuencia para que los SERIAL no choquen tras recargar.
                cur.execute(
                    "SELECT schemaname, sequencename, last_value "
                    "FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename")
                for esquema, seq, last in cur.fetchall():
                    ref = f"{esquema}.{seq}"
                    if last is None:
                        w(f"SELECT pg_catalog.setval('{ref}', 1, false);\n")
                    else:
                        w(f"SELECT pg_catalog.setval('{ref}', {last}, true);\n")

                w("\nSET session_replication_role = DEFAULT;\n")
    finally:
        conn.close()

    return destino, server_version, total_filas, resumen


def podar(out_dir: str, retention_days: int, keep: int) -> list:
    """Borra respaldos con más de retention_days, agrupando por 'grupo' (nombre+bd) y
    conservando siempre los 'keep' más nuevos DE CADA GRUPO — así el respaldo activo de un
    restaurante no hace que se pode de más el de otro."""
    if not os.path.isdir(out_dir):
        return []
    grupos: dict = {}
    for f in os.listdir(out_dir):
        m = NOMBRE_RE.match(f)
        if m:
            grupos.setdefault(m.group("grupo"), []).append((f, m.group("ts")))

    limite = datetime.now() - timedelta(days=retention_days)
    borrados = []
    for archivos in grupos.values():
        archivos.sort(key=lambda par: par[1], reverse=True)  # timestamp: más nuevos primero
        for i, (f, ts) in enumerate(archivos):
            if i < keep:
                continue  # nunca toques los 'keep' más recientes de este grupo
            cuando = datetime.strptime(ts, "%Y%m%d_%H%M%S")
            if cuando < limite:
                os.remove(os.path.join(out_dir, f))
                borrados.append(f)
    return borrados


def main() -> None:
    ap = argparse.ArgumentParser(description="Respaldo lógico (puro Python) de la BD.")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="postgresql://... (o variable DATABASE_URL)")
    ap.add_argument("--nombre", help="Etiqueta del restaurante (va en el nombre del archivo)")
    ap.add_argument("--out-dir", default="backups", help="Carpeta destino (def: backups)")
    ap.add_argument("--retention-days", type=int, default=14,
                    help="Borra respaldos más viejos que N días (def: 14)")
    ap.add_argument("--keep", type=int, default=7,
                    help="Conserva siempre al menos N respaldos por restaurante (def: 7)")
    args = ap.parse_args()

    if not args.database_url:
        sys.exit("[FATAL] Falta --database-url (o la variable DATABASE_URL)")

    destino, ver, total, resumen = respaldar(args.database_url, args.out_dir, args.nombre)
    tam = os.path.getsize(destino)
    print(f"✔ Respaldo creado: {destino}")
    print(f"  Postgres {ver} · {len(resumen)} tablas · {total} filas · {tam/1024:.1f} KB")
    for tabla, n in resumen:
        if n:
            print(f"    {tabla:<22} {n}")

    borrados = podar(args.out_dir, args.retention_days, args.keep)
    if borrados:
        print(f"  Podados {len(borrados)} respaldo(s) viejos (>{args.retention_days}d, "
              f"conservando {args.keep} por restaurante).")


if __name__ == "__main__":
    main()
