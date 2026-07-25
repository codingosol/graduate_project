import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).parent / "DB_migrations"


def apply_migrations(database_url: str) -> None:
    conn = psycopg2.connect(database_url)
    try:
        # 어느 DB에 적용하는지 반드시 눈에 보이게 한다.
        # 이 함수는 db.ensure_test_database()가 테스트 DB에 대해서도 호출하지만,
        # migrate.py를 직접 실행하면 DATABASE_URL(=운영 DB)이 대상이 된다.
        # 실제로 테스트 DB에 적용할 의도로 `python migrate.py`를 돌려 운영 DB에
        # 마이그레이션이 적용된 적이 있다(2026-07-21, 당시 운영 DB가 비어 있어 피해는 없었음).
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            print(f"대상 DB: {cur.fetchone()[0]}")

        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            conn.commit()
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        pending = [f for f in migration_files if f.name not in applied]

        if not pending:
            print("적용할 마이그레이션이 없습니다 (이미 최신 상태).")
            return

        for f in pending:
            print(f"적용 중: {f.name}")
            sql = f.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (f.name,)
                )
            conn.commit()
            print(f"  완료: {f.name}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL이 .env에 없습니다.")
    apply_migrations(database_url)


if __name__ == "__main__":
    main()
