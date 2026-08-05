"""대시보드 공용 상수 — Sentry 팔레트(DESIGN.md §1)와 채널 통념."""

# Sentry 다크 팔레트
COLORS = {
    "canvas": "#1D1127", "surface": "#2B1D38",
    "ink": "#E7E1EC", "ink_muted": "#9386A0", "border": "#776589",
    "accent": "#6C5FC7", "accent_soft": "#A396DA",
    # 데이터 — 성향 diverging (진보 파랑 ↔ 보수 빨강, 중립 회색)
    "left": "#3D74DB", "neutral": "#9386A0", "right": "#F55459",
    "good": "#33BF9E", "warn": "#FFC227", "event": "#FF7738",
}

# 언론 성향 통념 (infer.py와 동일). 진영 톤 색 배정에도 쓴다.
CONVENTION = {
    "오마이TV": "진보", "MBC 뉴스": "진보", "JTBC News": "진보",
    "TV조선": "보수", "채널A News": "보수", "MBN News": "보수",
    "KBS News": "중도/공영", "YTN": "중도/공영", "SBS 뉴스": "중도/공영",
    "연합뉴스TV": "중도/공영",
}


# 채널별 식별 색 — 로고(브랜드) 시그널 컬러를 참고하되, 겹치는 파랑 계열은
# 색조를 벌려 구분이 쉽도록 조정했다. (비교 페이지에서 채널 구분용 — 성향 색과는 별개)
CHANNEL_COLORS = {
    "오마이TV": "#F03E3E",     # 빨강
    "MBC 뉴스": "#1C6FD4",     # 파랑
    "JTBC News": "#12B5A5",    # 민트
    "TV조선": "#8E9BEE",       # 연보라파랑
    "채널A News": "#B06FD9",   # 보라
    "MBN News": "#F08B2C",     # 주황
    "YTN": "#E85D75",          # 핑크레드
    "SBS 뉴스": "#E6C020",     # 금노랑
    "KBS News": "#3FB96B",     # 초록
    "연합뉴스TV": "#56C2E6",   # 하늘
}


def conv_color(outlet):
    """통념 진영 → 대표 톤(시계열 채널 선 등 entity 고정색용)."""
    c = CONVENTION.get(outlet, "중도/공영")
    return {"진보": COLORS["left"], "보수": COLORS["right"]}.get(c, COLORS["neutral"])
