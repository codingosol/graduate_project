from urllib.parse import urlsplit, urlunsplit

import psycopg2

from migrate import apply_migrations

TEST_DB_NAME = "collect_test"


def to_database_url(database_url: str, db_name: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + db_name, parts.query, parts.fragment))


def ensure_test_database(database_url: str, db_name: str = TEST_DB_NAME) -> str:
    """운영 DATABASE_URL과 같은 Neon 프로젝트 안에 테스트 전용 DB(db_name)를 만들고,
    거기에 스키마 마이그레이션까지 적용한 뒤 그 DB의 연결 문자열을 반환한다.
    운영 DB의 테이블은 전혀 건드리지 않는다."""
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f'CREATE DATABASE "{db_name}"')
                print(f"테스트 DB 생성: {db_name}")
            else:
                print(f"테스트 DB 이미 존재: {db_name}")
    finally:
        conn.close()

    test_url = to_database_url(database_url, db_name)
    apply_migrations(test_url)
    return test_url
