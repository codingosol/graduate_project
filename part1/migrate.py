import argparse
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
        # migrate.py를 직접 실행하면 DATABASE_URL(=로컬 분석 DB)이, --staging이면
        # STAGING_DATABASE_URL(=Neon 스테이징)이 대상이 된다.
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
    p = argparse.ArgumentParser(description="미적용 마이그레이션을 순서대로 적용")
    p.add_argument("--staging", action="store_true",
                   help="로컬 분석 DB(DATABASE_URL) 대신 스테이징 DB(STAGING_DATABASE_URL)에 적용")
    args = p.parse_args()

    # 스키마는 로컬·스테이징이 같아야 sync.py가 그대로 옮길 수 있다 → 새 마이그레이션은 둘 다에 적용할 것.
    load_dotenv()
    env_name = "STAGING_DATABASE_URL" if args.staging else "DATABASE_URL"
    database_url = os.environ.get(env_name)
    if not database_url:
        raise SystemExit(f"{env_name}이 .env에 없습니다.")
    print(f"대상 환경변수: {env_name}")
    apply_migrations(database_url)


if __name__ == "__main__":
    main()
