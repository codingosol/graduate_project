import json
from pathlib import Path

KEYWORDS_FILE = Path(__file__).parent / "keywords.json"


INCLUDE_CATEGORIES = [
    "parties",
    "institutions_terms",
    "hanja_abbreviations",
    "nicknames",
    "politician_names",
]
EXCLUDE_CATEGORY = "exclude_foreign_leaders"


def load_political_keywords():
    """keywords.json을 읽어서 (포함 키워드 목록, 제외 키워드 목록)을 반환한다.
    포함 키워드는 정당명/기구·용어/한자 축약/별칭/정치인 이름 카테고리를 하나로 합친 것.
    카테고리 구분은 keywords.json 파일 자체에서만 유지(사람이 보기 편하게)하고,
    실제 매칭 로직은 구분 없이 "하나라도 포함되면 정치"로 취급한다.
    `_`로 시작하는 키(예: _comment)는 메타데이터라 무시한다."""
    data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))

    include_keywords = []
    for category in INCLUDE_CATEGORIES:
        include_keywords.extend(data[category])
    exclude_keywords = data[EXCLUDE_CATEGORY]

    return include_keywords, exclude_keywords
