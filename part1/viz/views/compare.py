"""페이지 C — 채널 비교. 채널별 '판정 중 우비율' EMA를 한 그래프에 겹쳐 본다."""

import plotly.graph_objects as go
import streamlit as st

from queries import channel_summary, channel_timeseries
from theme import CHANNEL_COLORS, COLORS
from util import center_spinner

summary = channel_summary()
outlets = summary.sort_values("score")["outlet"].tolist()

st.title("채널 비교 — 보수 비율 추세")
st.caption("각 선 = 채널의 일별 '판정(좌+우) 중 우(보수) 비율' EMA. **색은 채널 구분**(로고 브랜드색 기반)이고, "
           "세로축이 높을수록 보수 담론이 많습니다. 점선(50%)이 좌우 균형선입니다.")

c1, c2 = st.columns([3, 1])
sel = c1.multiselect("채널", outlets, default=outlets)
span = c2.slider("EMA span (일)", 3, 30, 14)

if not sel:
    st.info("비교할 채널을 하나 이상 선택하세요.")
    st.stop()

fig = go.Figure()
with center_spinner():
    for outlet in sel:
        ts = channel_timeseries(outlet).sort_values("wk")
        if ts.empty:
            continue
        dec = ts["l"] + ts["r"]
        rr = (ts["r"] / dec.where(dec > 0) * 100)     # 판정 중 우비율(판정 0인 날은 NaN)
        ema = rr.ewm(span=span, ignore_na=True).mean()
        fig.add_trace(go.Scatter(
            x=ts["wk"], y=ema, name=outlet, mode="lines",
            line=dict(color=CHANNEL_COLORS.get(outlet, COLORS["ink_muted"]), width=2.4),
            hovertemplate="%{x|%m-%d} · " + outlet + " %{y:.1f}%<extra></extra>"))

fig.add_hline(y=50, line=dict(color=COLORS["ink_muted"], dash="dot", width=1))
fig.update_layout(
    template="plotly_dark", height=560, margin=dict(l=10, r=10, t=20, b=10),
    paper_bgcolor=COLORS["canvas"], plot_bgcolor=COLORS["canvas"],
    legend=dict(orientation="h", y=1.08, x=0), hovermode="x unified",
    yaxis=dict(title="우비율 %", gridcolor=COLORS["surface"]),
    xaxis=dict(gridcolor=COLORS["surface"]))
st.plotly_chart(fig, use_container_width=True)
