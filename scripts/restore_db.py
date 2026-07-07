# -*- coding: utf-8 -*-
"""Restaura un respaldo creado por scripts/backup_db.py — puro Python (solo psycopg2).

    # Ensayo sin tocar la base (parsea y reporta qué haría):
    python scripts/restore_db.py --file backups/backup_railway_20260707_120000.sql.gz --dry-run

    # Restauración real (DESTRUCTIVA: reemplaza los datos de la base destino):
    python scripts/restore_db.py --file backups/....sql.gz --database-url postgresql://... --yes

Cómo funciona:
  1. (Por defecto) aplica db/schema.sql para garantizar las tablas — así también restaura
     sobre una base VACÍA. Es idempotente; sáltalo con --no-schema.
  2. Reproduce el respaldo: cada 'TRUNCATE' y 'COPY ... FROM stdin' se ejecuta en una sola
     sesión con session_replication_role=replica (ignora FKs → sin problemas de orden), y
     los 'SELECT setval(...)' dejan las secuencias donde iban.

⚠️ DESTRUCTIVO: vacía y recarga cada tabla del respaldo. Exige --yes para correr de verdad
(sin --yes solo hace el ensayo). Verifica DOS VECES la --database-url: nunca la de producción
de un restaurante en marcha salvo que sea justo lo que quieres.
"""
import argparse
import gzip
import io
import os
import re
import sys

try:
    import psycopg2
except ImportError:
    sys.exit("[FATAL] Falta psycopg2. Instala: pip install psycopg2-binary")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_SQL = os.path.join(RAIZ, "db", "schema.sql")
COPY_INI_RE = re.compile(r'^COPY\s+(?P<obj>.+?)\s+FROM stdin;\s*$', re.IGNORECASE)


def _conectar(url: str):
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)


def _abrir(path: str):
    return gzip.open(path, "rt", encoding="utf-8")


def analizar(path: str) -> tuple:
    """Recorre el respaldo sin tocar ninguna base. Devuelve (tablas, filas, sentencias)."""
    tablas, filas, sentencias = [], 0, 0
    with _abrir(path) as fh:
        en_copy = False
        for linea in fh:
            if en_copy:
                if linea.rstrip("\n") == "\\.":
                    en_copy = False
                else:
                    filas += 1
                continue
            m = COPY_INI_RE.match(linea)
            if m:
                en_copy = True
                tablas.append(m.group("obj").split("(")[0].strip())
            elif linea.strip() and not linea.startswith("--"):
                sentencias += 1
    return tablas, filas, sentencias


def restaurar(path: str, url: str, aplicar_schema: bool) -> tuple:
    conn = _conectar(url)
    conn.autocommit = False
    tablas_cargadas, filas = [], 0
    try:
        with conn.cursor() as cur:
            if aplicar_schema:
                if not os.path.exists(SCHEMA_SQL):
                    sys.exit(f"[FATAL] No se encontró {SCHEMA_SQL} (usa --no-schema si es a propósito)")
                with open(SCHEMA_SQL, encoding="utf-8") as fh:
                    cur.execute(fh.read())

            with _abrir(path) as fh:
                buffer, copy_sql, tabla_actual = [], None, None
                for linea in fh:
                    if copy_sql is not None:
                        if linea.rstrip("\n") == "\\.":
                            datos = io.StringIO("".join(buffer))
                            cur.copy_expert(copy_sql, datos)
                            filas += len(buffer)
                            tablas_cargadas.append(tabla_actual)
                            buffer, copy_sql, tabla_actual = [], None, None
                        else:
                            buffer.append(linea)
                        continue
                    m = COPY_INI_RE.match(linea)
                    if m:
                        obj = m.group("obj")
                        copy_sql = f"COPY {obj} FROM STDIN WITH (FORMAT text)"
                        tabla_actual = obj.split("(")[0].strip()
                    elif linea.strip() and not linea.startswith("--"):
                        cur.execute(linea)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return tablas_cargadas, filas


def main() -> None:
    ap = argparse.ArgumentParser(description="Restaura un respaldo de backup_db.py.")
    ap.add_argument("--file", required=True, help="Ruta al backup_*.sql.gz")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="postgresql://... destino (o variable DATABASE_URL)")
    ap.add_argument("--no-schema", action="store_true",
                    help="No aplica db/schema.sql antes (asume que las tablas ya existen)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo analiza el archivo y reporta; no toca ninguna base")
    ap.add_argument("--yes", action="store_true",
                    help="Confirma la restauración DESTRUCTIVA (sin esto, solo ensaya)")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"[FATAL] No existe el archivo {args.file}")

    tablas, filas, sentencias = analizar(args.file)
    print(f"Respaldo: {args.file}")
    print(f"  {len(tablas)} bloque(s) COPY · {filas} fila(s) de datos · "
          f"{sentencias} sentencia(s) SQL (TRUNCATE/setval)")

    if args.dry_run or not args.yes:
        if not args.dry_run:
            print("\n⚠️  Ensayo (sin --yes no se toca la base). Para restaurar de verdad:")
            print(f"    python scripts/restore_db.py --file {args.file} "
                  f"--database-url <URL> --yes")
        return

    if not args.database_url:
        sys.exit("[FATAL] Falta --database-url para restaurar")

    print(f"\nRestaurando en: {args.database_url.split('@')[-1]}  (DESTRUCTIVO)")
    cargadas, n = restaurar(args.file, args.database_url, aplicar_schema=not args.no_schema)
    print(f"✔ Restauración completa: {len(cargadas)} tabla(s), {n} fila(s) recargadas.")


if __name__ == "__main__":
    main()
