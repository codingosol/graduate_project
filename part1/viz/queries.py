"""대시보드 집계 쿼리 — newstance DB, 캐싱.

미분류 배제: 항상 comment_labels(run='model_kcelectra_v2')와 inner join 하므로,
collect.py가 지금 쌓는 아직 추론 안 된 신규 댓글은 자동으로 빠진다.
unusable 제외, neutral 포함(성향 집계). axis 파라미터로 강도 축 확장 대비.
"""

import os

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# 축 → 추론 run 매핑. axis는 label_runs에만 있고 comment_labels엔 없으므로,
# 축 구분은 run_id로 한다(강도 축은 model_khaters_v1 적재 시 활성).
RUN_BY_AXIS = {"leaning": "model_kcelectra_v2", "intensity": "model_khaters_v1"}


def _df(sql, params):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)
    finally:
        conn.close()


@st.cache_data(ttl=600, show_spinner=False)
def channel_summary(axis="leaning", ctype=None):
    """outlet별 좌/중립/우 카운트 + 성향점수 평균 + 표본 + 로고."""
    cond = "AND ch.channel_type=%s" if ctype else ""
    params = [RUN_BY_AXIS[axis]] + ([ctype] if ctype else [])
    sql = f"""
      SELECT ch.outlet_name AS outlet, max(ch.thumbnail_url) AS thumb,
        count(*) FILTER (WHERE l.label='left')    AS l,
        count(*) FILTER (WHERE l.label='neutral') AS n,
        count(*) FILTER (WHERE l.label='right')   AS r,
        avg(l.score)::float AS score, count(*) AS total
      FROM comment_labels l
      JOIN comments c  ON c.comment_id = l.comment_id
      JOIN videos v    ON v.video_id   = c.video_id
      JOIN channels ch ON ch.channel_id = v.channel_id
      WHERE l.run_id=%s AND l.label <> 'unusable' {cond}
      GROUP BY ch.outlet_name
    """
    return _df(sql, params)


@st.cache_data(ttl=600, show_spinner=False)
def channel_timeseries(outlet, axis="leaning", ctype=None):
    """한 outlet의 주별 좌/우/중립 카운트 + 영상 수 + 댓글 수."""
    cond = "AND ch.channel_type=%s" if ctype else ""
    params = [RUN_BY_AXIS[axis], outlet] + ([ctype] if ctype else [])
    sql = f"""
      SELECT date_trunc('day', c.published_at) AS wk,
        count(*) FILTER (WHERE l.label='left')    AS l,
        count(*) FILTER (WHERE l.label='right')   AS r,
        count(*) FILTER (WHERE l.label='neutral') AS n,
        count(DISTINCT v.video_id) AS videos, count(*) AS comments
      FROM comment_labels l
      JOIN comments c  ON c.comment_id = l.comment_id
      JOIN videos v    ON v.video_id   = c.video_id
      JOIN channels ch ON ch.channel_id = v.channel_id
      WHERE l.run_id=%s AND l.label <> 'unusable'
        AND ch.outlet_name=%s AND c.published_at IS NOT NULL {cond}
      GROUP BY 1 ORDER BY 1
    """
    return _df(sql, params)


@st.cache_data(ttl=600, show_spinner=False)
def has_intensity():
    """강도 축(model_khaters_v1) 추론 결과가 DB에 적재됐는지. 없으면 강도 탭은 '준비중'."""
    df = _df("SELECT count(*) AS n FROM comment_labels WHERE run_id=%s",
             [RUN_BY_AXIS["intensity"]])
    return int(df.iloc[0, 0]) > 0


@st.cache_data(ttl=600, show_spinner=False)
def channel_intensity_summary(ctype=None):
    """outlet별 평균 과격도(0~1, 높을수록 과격) + 표본 + 로고. 강도 축은 label이 NULL이라
    unusable 필터 없이 정치 댓글 전량을 집계한다(score=과격도)."""
    cond = "AND ch.channel_type=%s" if ctype else ""
    params = [RUN_BY_AXIS["intensity"]] + ([ctype] if ctype else [])
    sql = f"""
      SELECT ch.outlet_name AS outlet, max(ch.thumbnail_url) AS thumb,
        avg(l.score)::float AS intensity, count(*) AS total
      FROM comment_labels l
      JOIN comments c  ON c.comment_id = l.comment_id
      JOIN videos v    ON v.video_id   = c.video_id
      JOIN channels ch ON ch.channel_id = v.channel_id
      WHERE l.run_id=%s AND v.is_political {cond}
      GROUP BY ch.outlet_name
    """
    return _df(sql, params)


@st.cache_data(ttl=600, show_spinner=False)
def channel_intensity_timeseries(outlet, ctype=None):
    """한 outlet의 일별 평균 과격도(0~1) + 영상 수 + 댓글 수."""
    cond = "AND ch.channel_type=%s" if ctype else ""
    params = [RUN_BY_AXIS["intensity"], outlet] + ([ctype] if ctype else [])
    sql = f"""
      SELECT date_trunc('day', c.published_at) AS wk,
        avg(l.score)::float AS intensity,
        count(DISTINCT v.video_id) AS videos, count(*) AS comments
      FROM comment_labels l
      JOIN comments c  ON c.comment_id = l.comment_id
      JOIN videos v    ON v.video_id   = c.video_id
      JOIN channels ch ON ch.channel_id = v.channel_id
      WHERE l.run_id=%s AND ch.outlet_name=%s AND c.published_at IS NOT NULL {cond}
      GROUP BY 1 ORDER BY 1
    """
    return _df(sql, params)


@st.cache_data(ttl=600, show_spinner=False)
def all_channels_timeseries(axis="leaning", ctype=None):
    """**모든 outlet**의 일별 좌/우 카운트를 한 쿼리로 집계한다(compare 페이지용).

    채널마다 channel_timeseries를 따로 부르면 61만 행 조인을 채널 수만큼(10회) 반복해 느리다
    (실측 38s). 여기서 `GROUP BY outlet, 일`로 한 번만 훑고, compare가 pandas로 채널별로 쪼갠다."""
    cond = "AND ch.channel_type=%s" if ctype else ""
    params = [RUN_BY_AXIS[axis]] + ([ctype] if ctype else [])
    sql = f"""
      SELECT ch.outlet_name AS outlet, date_trunc('day', c.published_at) AS wk,
        count(*) FILTER (WHERE l.label='left')  AS l,
        count(*) FILTER (WHERE l.label='right') AS r
      FROM comment_labels l
      JOIN comments c  ON c.comment_id = l.comment_id
      JOIN videos v    ON v.video_id   = c.video_id
      JOIN channels ch ON ch.channel_id = v.channel_id
      WHERE l.run_id=%s AND l.label <> 'unusable' AND c.published_at IS NOT NULL {cond}
      GROUP BY 1, 2 ORDER BY 1, 2
    """
    return _df(sql, params)


@st.cache_data(ttl=600, show_spinner=False)
def channel_video_count(outlet, axis="leaning", ctype=None):
    """총 정치 영상 수(distinct). 시계열의 sum(일별 영상)은 한 영상이 여러 날에
    걸쳐 중복 집계되므로, 상단 지표에는 이 distinct 값을 쓴다."""
    cond = "AND ch.channel_type=%s" if ctype else ""
    params = [RUN_BY_AXIS[axis], outlet] + ([ctype] if ctype else [])
    sql = f"""
      SELECT count(DISTINCT v.video_id) AS videos
      FROM comment_labels l
      JOIN comments c  ON c.comment_id = l.comment_id
      JOIN videos v    ON v.video_id   = c.video_id
      JOIN channels ch ON ch.channel_id = v.channel_id
      WHERE l.run_id=%s AND l.label <> 'unusable' AND ch.outlet_name=%s {cond}
    """
    return int(_df(sql, params).iloc[0, 0])
