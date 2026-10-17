"""Inspeccion y purga de contenido (articles/clusters) de news.db.

Uso:
  python3 scripts/news_reset.py --inspect
  python3 scripts/news_reset.py --purge            # borra articulos y clusters
  python3 scripts/news_reset.py --purge --keep-runs  # no toca runs/deliveries
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/news.db")


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r[0] for r in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in table_names(conn)}


def fk_targets(conn: sqlite3.Connection) -> list[str]:
    out = []
    for tbl in table_names(conn):
        for row in conn.execute(f'PRAGMA foreign_key_list("{tbl}")'):
            if row[2] in {"articles", "clusters"}:
                out.append(f"{tbl}.{row[3]} -> {row[2]}.{row[4]}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="solo mostrar el estado")
    parser.add_argument("--purge", action="store_true", help="borrar articulos y clusters")
    parser.add_argument("--db", default=str(DB_PATH), help="ruta a news.db")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"No existe {db}")

    # news.db lo escriben los contenedores como root: el usuario host solo
    # puede abrirlo en modo lectura. La purga se ejecuta dentro de un contenedor.
    if args.purge:
        conn = sqlite3.connect(db)
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.execute("PRAGMA foreign_keys=ON")

    print(f"== {db} ==")
    for name, n in counts(conn).items():
        print(f"  {name:28} {n}")
    print("\n== FKs hacia articles/clusters ==")
    targets = fk_targets(conn)
    print("\n".join(f"  {t}" for t in targets) if targets else "  (ninguna)")

    if args.inspect or not args.purge:
        conn.close()
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db.with_suffix(db.suffix + f".pre-purge-{stamp}")
    shutil.copy2(db, backup)
    print(f"\nBackup: {backup}")

    conn.execute("BEGIN")
    # child -> parent: borrar primero las tablas que apuntan a articles/clusters
    for target in {t.split(" -> ")[0].split(".")[0] for t in targets}:
        conn.execute(f'DELETE FROM "{target}"')
        print(f"  DELETE FROM {target}")
    conn.execute("DELETE FROM clusters")
    print("  DELETE FROM clusters")
    conn.execute("DELETE FROM articles")
    print("  DELETE FROM articles")
    conn.execute("COMMIT")

    print("\n== estado tras la purga ==")
    for name, n in counts(conn).items():
        print(f"  {name:28} {n}")
    print("\nintegrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
