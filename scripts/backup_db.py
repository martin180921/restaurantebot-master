# -*- coding: utf-8 -*-
"""Respaldo LÓGICO de la base de datos de un restaurante — puro Python (solo psycopg2).

    python scripts/backup_db.py --database-url postgresql://... [--out-dir backups] \
        [--retention-days 14] [--keep 7]

Por qué puro Python y no pg_dump:
  - El servidor de Railway corre Postgres 18. Un pg_dump de una versión MENOR se niega a
    respaldar un servidor de versión mayor, así que exigiría instalar exactamente pg_dump 18
    en cada PC — fricción que no queremos en un piloto.
  - psycopg2 ya está instalado en todos los servicios (bot, panel, agente), así que este
    script corre en cualquier lado sin instalar nada.

Qué produce:
  Un archivo comprimido  backup_<bd>_<AAAAMMDD_HHMMSS>.sql.gz  con:
    - cabecera (fecha, versión del server, nº de tablas),
    - los datos de TODAS las tablas base de 'public' en formato COPY (texto de Postgres),
    - los setval de cada secuencia para que los SERIAL sigan donde iban.
  El formato es restaurable con scripts/restore_db.py (y también es legible con psql).

Es SOLO LECTURA sobre la base (COPY ... TO STDOUT): seguro de correr en producción.
Al terminar poda los respaldos viejos del directorio según --retention-days (conservando
siempre al menos --keep, por si un día no se generaron backups nuevos).
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

NOMBRE_RE = re.compile(r"^backup_(?P<bd>.+)_(?P<ts>\d{8}_\d{6})\.sql\.gz$")


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


def _nombre_bd(url: str) -> str:
    """Nombre de la BD para el archivo (última parte de la ruta, sin query)."""
    base = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base) or "db"


def respaldar(url: str, out_dir: str) -> tuple:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bd = _nombre_bd(url)
    destino = os.path.join(out_dir, f"backup_{bd}_{ts}.sql.gz")

    conn = _conectar(url)
    conn.set_session(readonly=True)
    total_filas = 0
    resumen = []
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            server_version = cur.fetchone()[0]
            tablas = _tablas_base(cur)

            with gzip.open(destino, "wb") as gz:
                def w(texto: str):
                    gz.write(texto.encode("utf-8"))

                w(f"-- olo backup lógico\n-- generado: {datetime.now().isoformat()}\n")
                w(f"-- server_version: {server_version}\n-- tablas: {len(tablas)}\n\n")
                # Durante la restauración: ignora FKs/triggers para no pelear con el orden.
                w("SET session_replication_role = replica;\n\n")

                for tabla in tablas:
                    cols = _columnas(cur, tabla)
                    if not cols:
                        continue
                    cur.execute(f'SELECT COUNT(*) FROM public."{tabla}"')
                    n = cur.fetchone()[0]
                    total_filas += n
                    resumen.append((tabla, n))
                    cols_sql = ", ".join(f'"{c}"' for c in cols)
                    # Reemplazo total en la restauración: vacía antes de recargar.
                    w(f'TRUNCATE public."{tabla}";\n')
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
    """Borra respaldos con más de retention_days, conservando siempre los 'keep' más nuevos."""
    if not os.path.isdir(out_dir):
        return []
    archivos = sorted(
        (f for f in os.listdir(out_dir) if NOMBRE_RE.match(f)),
        reverse=True)  # más nuevos primero (el timestamp ordena lexicográficamente)
    limite = datetime.now() - timedelta(days=retention_days)
    borrados = []
    for i, f in enumerate(archivos):
        if i < keep:
            continue  # nunca toques los 'keep' más recientes
        m = NOMBRE_RE.match(f)
        cuando = datetime.strptime(m.group("ts"), "%Y%m%d_%H%M%S")
        if cuando < limite:
            os.remove(os.path.join(out_dir, f))
            borrados.append(f)
    return borrados


def main() -> None:
    ap = argparse.ArgumentParser(description="Respaldo lógico (puro Python) de la BD.")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="postgresql://... (o variable DATABASE_URL)")
    ap.add_argument("--out-dir", default="backups", help="Carpeta destino (def: backups)")
    ap.add_argument("--retention-days", type=int, default=14,
                    help="Borra respaldos más viejos que N días (def: 14)")
    ap.add_argument("--keep", type=int, default=7,
                    help="Conserva siempre al menos N respaldos, sin importar su edad (def: 7)")
    args = ap.parse_args()

    if not args.database_url:
        sys.exit("[FATAL] Falta --database-url (o la variable DATABASE_URL)")

    destino, ver, total, resumen = respaldar(args.database_url, args.out_dir)
    tam = os.path.getsize(destino)
    print(f"✔ Respaldo creado: {destino}")
    print(f"  Postgres {ver} · {len(resumen)} tablas · {total} filas · {tam/1024:.1f} KB")
    for tabla, n in resumen:
        if n:
            print(f"    {tabla:<22} {n}")

    borrados = podar(args.out_dir, args.retention_days, args.keep)
    if borrados:
        print(f"  Podados {len(borrados)} respaldo(s) viejos (>{args.retention_days}d, "
              f"conservando {args.keep}).")


if __name__ == "__main__":
    main()
