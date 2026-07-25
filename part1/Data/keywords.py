import json
import unicodedata
from pathlib import Path

KEYWORDS_FILE = Path(__file__).parent / "keywords.json"


# 국내 정치를 **고유하게** 가리키는 카테고리.
# 이 중 하나라도 걸리면 해외 정상 이름이 같이 나와도 국내 정치로 본다
# (예: "트럼프 대통령에 국방비 먼저 꺼낸 이재명 대통령" — 이재명이 있으므로 국내 정치).
DOMESTIC_CATEGORIES = [
    "parties",
    "hanja_abbreviations",
    "nicknames",
    "politician_names",
]

# 국내 정치에 자주 쓰이지만 해외 뉴스에도 그대로 등장하는 일반 용어.
# 이것만 걸린 상태에서 해외 정상 이름이 있으면 해외 뉴스로 본다
# (예: "트럼프 부정선거 관련 대국민 연설" — '대통령'·'부정선거'만 걸림).
GENERIC_CATEGORIES = ["institutions_terms"]

INCLUDE_CATEGORIES = DOMESTIC_CATEGORIES + GENERIC_CATEGORIES
EXCLUDE_CATEGORY = "exclude_foreign_leaders"


def normalize(text):
    """제목·키워드를 비교 전에 유니코드 정규화(NFC)한다.

    왜 필요한가 — 실제로 새던 구멍:
      한국 언론 헤드라인의 `李`가 표준 한자(U+674E)가 아니라 **CJK 호환용 한자**
      (U+F9E1)로 들어오는 경우가 있다. 사람 눈에는 완전히 같은 글자지만 코드포인트가
      달라 `'李' in title`이 False가 된다. 실측 413개 제목에 호환용 한자가 있었고,
      그중 7건은 `李`가 유일한 정치 신호라 통째로 비정치로 분류돼 있었다
      (예: `李 "해괴하다"는 2심 판결문 보니...`, `'李 피습' 6개월 수사했지만...`).
      NFC가 호환용 한자를 표준 한자로 정규화한다(U+F9E1 → U+674E).
    """
    return unicodedata.normalize("NFC", text)


def load_political_keywords():
    """(포함 키워드, 제외 키워드) 2-튜플. 기존 호출부 호환용.

    국내 고유/일반 용어 구분이 필요한 쪽은 `load_keyword_groups()`를 쓴다.
    """
    domestic, generic, exclude = load_keyword_groups()
    return domestic + generic, exclude


def load_keyword_groups():
    """(국내 고유 키워드, 일반 용어, 제외 키워드) 세 갈래로 반환한다.

    제외 규칙을 '무조건 제외'에서 '일반 용어만 걸렸을 때만 제외'로 바꾸려면 갈래가 필요하다.
    키워드도 제목과 같은 방식으로 정규화해 둔다(한쪽만 정규화하면 여전히 안 맞는다).
    `_`로 시작하는 키(_comment 등)는 메타데이터라 카테고리 목록에 없으므로 자연히 무시된다.
    """
    data = json.loads(KEYWORDS_FILE.read_text(encoding="utf-8"))

    def collect(categories):
        out = []
        for category in categories:
            out.extend(normalize(k) for k in data[category])
        return out

    return (
        collect(DOMESTIC_CATEGORIES),
        collect(GENERIC_CATEGORIES),
        [normalize(k) for k in data[EXCLUDE_CATEGORY]],
    )
