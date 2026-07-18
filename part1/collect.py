import os
import sys


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    api_key = os.environ.get("YOUTUBE_API_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not api_key or not database_url:
        raise SystemExit("YOUTUBE_API_KEY / DATABASE_URL 환경변수가 필요합니다.")

    # TODO: channels.list -> playlistItems.list -> videos.list(카테고리 필터)
    #       -> commentThreads.list 순서로 수집 후 PostgreSQL(Neon)에 적재
    print("collect.py placeholder: 수집 로직 아직 미구현")


if __name__ == "__main__":
    main()
