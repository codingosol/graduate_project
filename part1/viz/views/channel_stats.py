"""페이지 B — 채널 별 통계 (주별 좌/우 시계열 + 원시/EMA 전환 + 영상·댓글 트랙).

원시/EMA 전환은 서버 rerun 없이 브라우저에서 즉시 되도록 plotly 내장 버튼(updatemenus)으로 한다
— Streamlit 위젯(selectbox)은 매번 서버 왕복이라 전환이 뚝뚝 끊긴다.
"""

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from queries import channel_summary, channel_timeseries, channel_video_count
from theme import COLORS, CONVENTION
from util import center_spinner

with center_spinner():
    summary = channel_summary()
outlets = summary.sort_values("score")["outlet"].tolist()
qp_outlet = st.query_params.get("outlet")  # 대시보드 카드 링크(?outlet=)에서 전달
default = qp_outlet or st.session_state.get("sel_outlet") or (outlets[0] if outlets else None)
ctype = st.session_state.get("sel_ctype")

st.caption("◂ 왼쪽 사이드바에서 '대시보드'로 돌아갈 수 있습니다.")
outlet = st.selectbox("채널", outlets, index=outlets.index(default) if default in outlets else 0)
st.title(f"{outlet}  ·  {CONVENTION.get(outlet, '—')}")

tab_lean, tab_int = st.tabs(["성향 (leaning)", "강도 (준비중)"])

with tab_lean:
    with center_spinner():
        ts = channel_timeseries(outlet, ctype=ctype).sort_values("wk")
    if ts.empty:
        st.warning("이 채널의 시계열 데이터가 없습니다.")
        st.stop()

    ts["tot"] = ts["l"] + ts["n"] + ts["r"]
    ts["left_pct"] = ts["l"] / ts["tot"] * 100
    ts["right_pct"] = ts["r"] / ts["tot"] * 100

    dec = int(ts["l"].sum() + ts["r"].sum())
    rr = ts["r"].sum() / dec * 100 if dec else 0
    m1, m2, m3 = st.columns(3)
    m1.metric("판정 중 우비율", f"{rr:.1f}%", help="좌+우 중 우(보수) 비율")
    m2.metric("영상 수", f"{channel_video_count(outlet, ctype=ctype):,}")
    m3.metric("댓글 수", f"{int(ts['comments'].sum()):,}")

    # EMA span만 서버 계산이라 slider(rerun). 원시/EMA 보기 전환은 아래 plotly 버튼(클라이언트).
    span = st.slider("EMA span (일)", 3, 30, 14)
    ts["left_ema"] = ts["left_pct"].ewm(span=span).mean()
    ts["right_ema"] = ts["right_pct"].ewm(span=span).mean()

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        row_heights=[0.72, 0.28])

    def line(y, color, name, width, op):
        fig.add_trace(go.Scatter(
            x=ts["wk"], y=ts[y], name=name, mode="lines",
            line=dict(color=color, width=width), opacity=op,
            hovertemplate="%{x|%m-%d} · %{y:.1f}%<extra>" + name + "</extra>"),
            row=1, col=1)

    # trace 순서 고정: 0 좌원시 · 1 우원시 · 2 좌EMA · 3 우EMA · 4 댓글막대
    line("left_pct", COLORS["left"], "진보% (원시)", 1.3, 0.5)
    line("right_pct", COLORS["right"], "보수% (원시)", 1.3, 0.5)
    line("left_ema", COLORS["left"], "진보% (EMA)", 2.8, 1.0)
    line("right_ema", COLORS["right"], "보수% (EMA)", 2.8, 1.0)
    fig.add_trace(go.Bar(
        x=ts["wk"], y=ts["comments"], name="댓글 수", marker_color=COLORS["ink_muted"],
        customdata=ts["videos"],
        hovertemplate="%{x|%m-%d} · 댓글 %{y:,} · 영상 %{customdata}<extra></extra>"),
        row=2, col=1)

    # 클라이언트 사이드 전환 버튼(서버 rerun 없음). visible + opacity + line.width 를 함께 restyle:
    # 원시 단독은 굵게/진하게, '둘 다'일 때만 원시를 얇게/옅게. (순서 0좌원시 1우원시 2좌EMA 3우EMA 4댓글)
    def btn(label, vis, op, lw):
        return dict(label=label, method="update",
                    args=[{"visible": vis, "opacity": op, "line.width": lw}])
    fig.update_layout(
        updatemenus=[dict(
            type="buttons", direction="right", showactive=True, active=2,
            x=0, xanchor="left", y=1.18, yanchor="top",
            pad=dict(r=4, t=2), bgcolor=COLORS["surface"], bordercolor=COLORS["border"],
            font=dict(color=COLORS["ink"]),
            buttons=[
                btn("원시", [True, True, False, False, True], [1, 1, 1, 1, 1], [2.6, 2.6, 2.8, 2.8, None]),
                btn("EMA", [False, False, True, True, True], [1, 1, 1, 1, 1], [1.3, 1.3, 2.8, 2.8, None]),
                btn("둘 다", [True, True, True, True, True], [0.5, 0.5, 1, 1, 1], [1.3, 1.3, 2.8, 2.8, None]),
            ])],
        template="plotly_dark", height=520, margin=dict(l=10, r=10, t=54, b=10),
        paper_bgcolor=COLORS["canvas"], plot_bgcolor=COLORS["canvas"],
        legend=dict(orientation="h", y=1.12, x=0.18), hovermode="x unified", bargap=0.3,
        transition=dict(duration=350, easing="cubic-in-out"))
    fig.update_yaxes(title_text="%", row=1, col=1, gridcolor=COLORS["surface"])
    fig.update_yaxes(title_text="댓글", row=2, col=1, gridcolor=COLORS["surface"])
    fig.update_xaxes(gridcolor=COLORS["surface"], row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)
    st.caption("그래프 위 '원시 / EMA / 둘 다' 버튼으로 즉시 전환(브라우저 내부라 부드럽게). "
               "세로 %=주별 좌/우 비율, 하단=주별 댓글 수(hover에 영상 수).")

with tab_int:
    st.info("강도 축(K-HATERS 기반 과격도)은 아직 도입 전입니다. "
            "`model_khaters_v1` run이 적재되면 여기에 동일한 시계열이 표시됩니다.")
