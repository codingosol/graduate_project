"""공용 UI 유틸 — 화면 중앙 원형 로딩 스피너.

Streamlit 기본 실행 인디케이터(우상단 아이콘)는 app.py CSS에서 숨겼고,
데이터 로딩 구간을 이 컨텍스트로 감싸면 화면 한가운데에 원형 스피너가 뜬다.
(캐시 히트 시에는 즉시 끝나 보이지 않는다.)
"""

from contextlib import contextmanager

import streamlit as st


@contextmanager
def center_spinner():
    ph = st.empty()
    ph.markdown('<div class="loader-overlay"><div class="loader"></div></div>',
                unsafe_allow_html=True)
    try:
        yield
    finally:
        ph.empty()
