"""페이지 A — 채널 대시보드. (st.navigation 진입점 app.py 에서 로드)

채널 행은 HTML 카드다 — 로고+이름이 붙어 있고, 카드 전체를 누르면 상세로 이동한다
(st.image 의 확대 버튼/떨어진 배치 문제를 피하려고 st.columns 대신 HTML 을 쓴다).
"""

from urllib.parse import quote

import streamlit as st

from queries import channel_summary
from theme import COLORS, CONVENTION
from util import center_spinner

NEUTRAL_SOFT = "#544A63"  # 중립 회색을 저채도로 낮춰 좌/우가 도드라지되, 아예 사라지진 않게

st.markdown(
    f"""<style>
    .ch-list {{ display:flex; flex-direction:column; gap:8px; margin-top:6px; }}
    .ch-card {{ display:flex; align-items:center; gap:14px; padding:8px;
        background:transparent; border:none; border-radius:10px;
        text-decoration:none; transition:background .15s; }}
    .ch-card:hover {{ background:{COLORS['surface']}; }}
    .ch-card, .ch-card * {{ text-decoration:none !important; }}
    .ch-logo {{ width:46px; height:46px; border-radius:9px; object-fit:cover; flex:0 0 auto;
        background:{COLORS['canvas']}; }}
    .ch-name {{ flex:0 0 148px; color:{COLORS['ink']}; font-weight:600; font-size:1.02rem; line-height:1.25; }}
    .ch-sub {{ color:{COLORS['ink_muted']}; font-size:0.76rem; font-weight:400; }}
    .ch-bar {{ flex:1 1 auto; height:18px; border-radius:9px; border:1px solid {COLORS['border']}; }}
    .ch-meta {{ flex:0 0 88px; text-align:right; }}
    .ch-score {{ color:{COLORS['accent_soft']}; font-weight:600; }}
    </style>""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.subheader("필터")
    ctype_label = st.radio("채널 종류", ["전체", "뉴스(news)", "시사(opinion)"], index=0)
    ctype = {"전체": None, "뉴스(news)": "news", "시사(opinion)": "opinion"}[ctype_label]
    axis_label = st.radio("축", ["성향 (leaning)", "강도 (intensity)"], index=0)
    axis = "intensity" if axis_label.startswith("강도") else "leaning"


def _logo(row):
    return (f'<img class="ch-logo" src="{row["thumb"]}"/>' if row["thumb"]
            else '<div class="ch-logo"></div>')


# ── 강도 축 대시보드 (과격도) ─────────────────────────────────────
if axis == "intensity":
    from queries import has_intensity, channel_intensity_summary
    if not has_intensity():
        st.title("채널 댓글 과격도 (준비중)")
        st.info("강도 축(K-HATERS 과격도)은 아직 적재 전입니다. "
                "`model_khaters_v1` run이 적재되면 채널별 과격도가 표시됩니다.")
        st.stop()
    st.title("뉴스 채널 댓글 과격도")
    st.caption("K-HATERS 과격도(0=온건 ~ 1=과격, 높을수록 과격). 성향과 독립된 축 — "
               "'성향은 중도인데 과격한 채널'을 드러냅니다. 채널을 누르면 상세로 이동합니다.")
    with center_spinner():
        di = channel_intensity_summary(ctype=ctype)
    if di.empty:
        st.warning("표시할 데이터가 없습니다."); st.stop()
    di = di.sort_values("intensity", ascending=False)
    tot = di["total"].sum()
    ov = (di["intensity"] * di["total"]).sum() / tot if tot else 0
    k1, k2, k3 = st.columns(3)
    k1.metric("전체 평균 과격도", f"{ov:.3f}")
    k2.metric("최과격 채널", di.iloc[0]["outlet"], f"{di.iloc[0]['intensity']:.3f}")
    k3.metric("최온건 채널", di.iloc[-1]["outlet"], f"{di.iloc[-1]['intensity']:.3f}")
    st.divider()
    st.subheader("채널별 과격도")

    def card_i(row):
        pct = float(row["intensity"]) * 100
        bar = (f'<div class="ch-bar" style="background:linear-gradient(90deg,'
               f'{COLORS["warn"]} {pct:.1f}%,{COLORS["surface"]} {pct:.1f}%)"></div>')
        return (
            f'<a class="ch-card" href="/channel-stats?outlet={quote(str(row["outlet"]))}" target="_self">'
            f'{_logo(row)}'
            f'<div class="ch-name">{row["outlet"]}'
            f'<div class="ch-sub">{CONVENTION.get(row["outlet"], "—")} · 표본 {int(row["total"]):,}</div></div>'
            f'{bar}'
            f'<div class="ch-meta"><span class="ch-score">{row["intensity"]:.3f}</span>'
            f'<div class="ch-sub">과격도</div></div></a>'
        )
    st.markdown('<div class="ch-list">' + "".join(card_i(r) for _, r in di.iterrows()) + "</div>",
                unsafe_allow_html=True)
    st.stop()


# ── 성향 축 대시보드 (기본) ──────────────────────────────────────
st.title("뉴스 채널 정치 성향")
st.caption("YouTube 댓글 분류(KcELECTRA v2). 음수=진보 · 양수=보수 · 아직 분류 안 된 신규 댓글은 제외. 채널 카드를 누르면 상세 통계로 이동합니다.")

with center_spinner():
    df = channel_summary(axis="leaning", ctype=ctype)
if df.empty:
    st.warning("표시할 데이터가 없습니다.")
    st.stop()
df = df.sort_values("score")  # 진보(음수) → 보수(양수)

k1, k2, k3, k4 = st.columns(4)
k1.metric("분류 댓글", f"{int(df['total'].sum()):,}")
k2.metric("최진보 채널", df.iloc[0]["outlet"], f"{df.iloc[0]['score']:+.3f}")
k3.metric("최보수 채널", df.iloc[-1]["outlet"], f"{df.iloc[-1]['score']:+.3f}")
k4.metric("채널 수", f"{df['outlet'].nunique()}")

st.divider()
st.subheader("채널별 좌 · 중립 · 우 비율")


def grad(l, n, r):
    """전체(좌+중립+우) 기준 3색 그라데이션. 경계를 부드럽게, 중립은 저채도."""
    tot = max(l + n + r, 1)
    a = l / tot * 100                # 좌 끝
    b = a + n / tot * 100            # 중립 끝
    x = 3
    return (
        f"linear-gradient(90deg,{COLORS['left']} 0%,{COLORS['left']} {max(a-x,0):.1f}%,"
        f"{NEUTRAL_SOFT} {min(a+x,100):.1f}%,{NEUTRAL_SOFT} {max(b-x,0):.1f}%,"
        f"{COLORS['right']} {min(b+x,100):.1f}%,{COLORS['right']} 100%)"
    )


def card(row):
    tot = max(int(row["total"]), 1)
    neu = row["n"] / tot * 100
    dec = max(row["l"] + row["r"], 1)
    title = f"진보 {row['l']/dec*100:.0f}% · 보수 {row['r']/dec*100:.0f}% · 중립 {neu:.0f}%"
    logo = f'<img class="ch-logo" src="{row["thumb"]}"/>' if row["thumb"] else '<div class="ch-logo"></div>'
    return (
        f'<a class="ch-card" href="/channel-stats?outlet={quote(str(row["outlet"]))}" target="_self">'
        f'{logo}'
        f'<div class="ch-name">{row["outlet"]}'
        f'<div class="ch-sub">{CONVENTION.get(row["outlet"], "—")} · 표본 {tot:,}</div></div>'
        f'<div class="ch-bar" style="background:{grad(row["l"], row["n"], row["r"])}" title="{title}"></div>'
        f'<div class="ch-meta"><span class="ch-score">{row["score"]:+.3f}</span>'
        f'<div class="ch-sub">중립 {neu:.0f}%</div></div>'
        f"</a>"
    )


st.markdown('<div class="ch-list">' + "".join(card(r) for _, r in df.iterrows()) + "</div>",
            unsafe_allow_html=True)
