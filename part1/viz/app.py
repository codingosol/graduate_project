"""viz 진입점 — st.navigation 멀티페이지 + 공용 폰트/배경."""

import streamlit as st

from theme import COLORS

st.set_page_config(page_title="뉴스 채널 성향", page_icon="📊",
                   layout="wide", initial_sidebar_state="auto")

# Pretendard 웹폰트 + 배경. @import 는 <style> 최상단이어야 유효.
# ⚠️ font-family를 전역(*)으로 강제하면 Streamlit의 material 아이콘 폰트까지 덮어써
#    아이콘이 'dashboard' 같은 글자로 새어 나온다 → 아이콘 폰트는 명시적으로 복원한다.
st.markdown(
    f"""<style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@1.3.9/dist/web/static/pretendard.min.css');
    html, body, [class*="css"], .stApp, .stApp * {{
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        font-variant-numeric: tabular-nums;
    }}
    span[data-testid="stIconMaterial"], .material-symbols-outlined, .material-symbols-rounded,
    .material-icons, [class*="material-symbols"] {{
        font-family: 'Material Symbols Outlined', 'Material Symbols Rounded', 'Material Icons' !important;
    }}
    .stApp {{ background:{COLORS['canvas']}; }}
    /* 기본 우상단 실행 인디케이터(움직이는 아이콘) 숨김 → 중앙 원형 스피너로 대체 */
    [data-testid="stStatusWidget"] {{ display:none; }}
    .loader-overlay {{ position:fixed; inset:0; display:flex; align-items:center;
        justify-content:center; z-index:99999; pointer-events:none; }}
    .loader {{ width:52px; height:52px; border:5px solid #3a2b4c;
        border-top-color:{COLORS['accent']}; border-radius:50%;
        animation:ldspin .8s linear infinite; }}
    @keyframes ldspin {{ to {{ transform:rotate(360deg); }} }}
    </style>""",
    unsafe_allow_html=True,
)

pg = st.navigation([
    st.Page("views/dashboard.py", title="대시보드", icon="📊", default=True, url_path="dashboard"),
    st.Page("views/channel_stats.py", title="채널 별 통계", icon="📈", url_path="channel-stats"),
    st.Page("views/compare.py", title="채널 비교", icon="⚖️", url_path="compare"),
])
pg.run()
