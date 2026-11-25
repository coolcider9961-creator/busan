"""부산 5대 상권 호스텔업·외국인관광도시민박업 상권분석 대시보드 (Streamlit)."""

from __future__ import annotations

import html as html_lib
import importlib
import math
import re
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Tuple

import folium
import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium

import src.collectors.airbnb_apify as _apify_mod
import src.collectors.tourism_datalab as _datalab_mod
import src.db.database as _db_mod
import src.db.queries as _queries_mod

importlib.reload(_db_mod)
importlib.reload(_apify_mod)
importlib.reload(_datalab_mod)
importlib.reload(_queries_mod)

from src.collectors.airbnb_apify import (
    compute_snapshot_windows,
    import_airbnb_json,
    purge_non_target_records,
    register_tracked_airbnb_url,
    sync_airbnb_via_apify,
    sync_calendar_30d_via_apify,
)
from src.collectors.public_data import (
    import_localdata_csv,
    sync_public_data_from_api,
    sync_public_data_from_localdata_portal,
)
from src.collectors.tourism_datalab import (
    SIGUNGU_DATALAB_MAP,
    get_tourism_monthly_df,
    get_tourism_nationality_df,
    import_datalab_csv,
    sync_tourism_visitors_from_api,
)
from src.config import (
    DATA_DIR,
    INDUSTRY_HOSTEL,
    INDUSTRY_URBAN_MINBAK,
    ZONES,
    get_env,
    save_env_values,
    verify_access_password,
)
from src.db.database import get_conn, init_db
from src.db.queries import (
    export_excel_bytes,
    get_collection_runs_df,
    get_competitor_df,
    get_price_timeseries_df,
    get_public_businesses_df,
    get_tracked_urls_df,
    get_zone_overview,
    parse_json_col,
)
from src.processing.matcher import clear_match, run_auto_matching, set_manual_match
from src.processing.review_analyzer import analyze_all_listings

st.set_page_config(
    page_title="DH 부산 호스텔/외도민 상권분석",
    layout="wide",
)


@st.cache_resource
def _ensure_db_initialized() -> bool:
    init_db()
    return True


_ensure_db_initialized()


@st.cache_data(ttl=300, show_spinner=False)
def _cached_export_excel(zone_key: str, _v: int = 10) -> bytes:
    return export_excel_bytes(zone_key)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_zone_overview(zone_key: str, _v: int = 10) -> Dict[str, Any]:
    return get_zone_overview(zone_key)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_competitor_df(zone_key: str, _v: int = 10) -> pd.DataFrame:
    df = get_competitor_df(zone_key)
    if not df.empty and ("thumbnail_url" not in df.columns or df["thumbnail_url"].isna().all()):
        with get_conn() as conn:
            t_map = dict(
                conn.execute(
                    "SELECT listing_id, thumbnail_url FROM airbnb_listings WHERE thumbnail_url IS NOT NULL"
                ).fetchall()
            )
        df["thumbnail_url"] = df["listing_id"].astype(str).map(t_map)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _cached_public_businesses_df(zone_key: str, _v: int = 10) -> pd.DataFrame:
    df = get_public_businesses_df(zone_key)
    if not df.empty and ("matched_thumbnail_url" not in df.columns or df["matched_thumbnail_url"].isna().all()):
        with get_conn() as conn:
            pub_t_map = dict(
                conn.execute(
                    """
                    SELECT m.public_id, MAX(CASE WHEN a.thumbnail_url LIKE 'http%' THEN a.thumbnail_url END)
                    FROM business_listing_matches m
                    JOIN airbnb_listings a ON a.listing_id = m.listing_id
                    WHERE a.thumbnail_url IS NOT NULL AND a.thumbnail_url != ''
                    GROUP BY m.public_id
                    """
                ).fetchall()
            )
        df["matched_thumbnail_url"] = df["id"].map(pub_t_map)
    return df


def _inject_modern_css() -> None:
    """AirROI 원본 감성 그대로(화이트 + 쿨 라이트그레이 + 은은한 파스텔 스카이/세룰리안 블루 라이트 테마) 스타일을 적용한다."""
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');
        @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');

        :root {
            --primary-color: #5B9BD5 !important;
            --font-sans: 'Plus Jakarta Sans', 'Pretendard Variable', 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
            --font-brand: 'Nunito', 'Plus Jakarta Sans', 'Pretendard Variable', sans-serif;
        }
        html, body, [class*="css"], .stApp {
            font-family: var(--font-sans) !important;
            background-color: #F8FAFC !important;
            color: #334155 !important;
        }
        header[data-testid="stHeader"] {
            background-color: rgba(255, 255, 255, 0.94) !important;
            border-bottom: 1px solid #EEF2F6 !important;
        }
        [data-testid="stWidgetLabel"] p {
            color: #475569 !important;
            font-weight: 500 !important;
            font-size: 0.85rem !important;
        }
        [data-testid="stCaptionContainer"] p {
            color: #64748B !important;
        }
        .block-container {
            padding-top: 3.1rem !important;
            padding-bottom: 2.5rem !important;
            padding-left: 2.0rem !important;
            padding-right: 2.0rem !important;
            max-width: 97% !important;
        }
        /* 사이드바 AirROI 순백(#FFFFFF) + 얇은 쿨그레이 구분선 */
        section[data-testid="stSidebar"] {
            background-color: #FFFFFF !important;
            border-right: 1px solid #EDF2F7 !important;
        }
        section[data-testid="stSidebar"] .block-container {
            padding-top: 1.25rem !important;
            padding-left: 1.15rem !important;
            padding-right: 1.15rem !important;
        }
        /* 좌측 상단 AirROI 스타일 브랜드 로고 박스 (AirROI의 airroi 워드마크처럼 가볍고 부드러운 세룰리안 톤) */
        .airroi-brand-box {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 4px 2px 14px 2px;
            margin-bottom: 10px;
            border-bottom: 1px solid #F1F5F9;
        }
        .airroi-logo-icon {
            width: 30px;
            height: 30px;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
        }
        .airroi-brand-title {
            font-family: var(--font-brand) !important;
            font-size: 1.05rem;
            font-weight: 600;
            color: #334155;
            letter-spacing: -0.02em;
            line-height: 1.25;
            word-break: keep-all;
        }
        .airroi-brand-dh {
            color: #4A90E2;
            font-weight: 700;
            letter-spacing: -0.01em;
            margin-right: 4px;
        }
        .airroi-brand-sub {
            font-size: 0.72rem;
            font-weight: 400;
            color: #94A3B8;
            margin-top: 2px;
            letter-spacing: -0.01em;
        }
        /* AirROI 좌측 메뉴 섹션 필(MARKET INSIGHTS / TOOLKIT 스타일: 연한 쿨그레이 배경 + 차분한 슬레이트 텍스트) */
        .sidebar-section-pill {
            background: #F1F5F9;
            border: none;
            border-radius: 7px;
            padding: 6px 11px;
            font-size: 0.72rem;
            font-weight: 600;
            color: #64748B;
            letter-spacing: 0.03em;
            margin-top: 12px;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        /* 입력창·셀렉트박스·익스팬더·폼을 AirROI 화이트 필(Pill) 스타일로 고정 */
        div[data-baseweb="select"] > div,
        div[data-baseweb="input"],
        div[data-baseweb="base-input"] {
            background-color: #FFFFFF !important;
            border: 1px solid #E2E8F0 !important;
            border-radius: 9px !important;
            color: #334155 !important;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03) !important;
            transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
        }
        div[data-baseweb="select"] > div:hover,
        div[data-baseweb="input"]:hover {
            border-color: #93C5FD !important;
        }
        [data-testid="stExpander"], [data-testid="stForm"] {
            background-color: #FFFFFF !important;
            border: 1px solid #E2E8F0 !important;
            border-radius: 10px !important;
        }
        /* 멀티셀렉트 태그: 쨍한 원색 파랑 대신 AirROI Overview 메뉴처럼 아주 연하고 고급스러운 파스텔 스카이 블루 */
        span[data-baseweb="tag"] {
            background-color: #EBF4FF !important;
            color: #2B6CB0 !important;
            border: 1px solid #C3DAFE !important;
            border-radius: 7px !important;
            font-weight: 500 !important;
        }
        span[data-baseweb="tag"] span,
        span[data-baseweb="tag"] svg {
            color: #2B6CB0 !important;
            fill: #4A90E2 !important;
        }
        /* Primary 버튼: 쨍한 코발트 블루 대신 AirROI 파스텔 스카이 필(#E3F0FF) 또는 부드러운 세룰리안 톤 */
        button[kind="primary"], [data-testid="stBaseButton-primary"] {
            background: #E6F2FF !important;
            border: 1px solid #B8D8FF !important;
            color: #235A96 !important;
            font-weight: 600 !important;
            border-radius: 9px !important;
            box-shadow: 0 1px 2px rgba(74, 144, 226, 0.08) !important;
        }
        button[kind="primary"] p, button[kind="primary"] span,
        [data-testid="stBaseButton-primary"] p, [data-testid="stBaseButton-primary"] span {
            color: #235A96 !important;
            font-weight: 600 !important;
        }
        button[kind="primary"]:hover, [data-testid="stBaseButton-primary"]:hover {
            background: #D6E9FF !important;
            border-color: #90C2FF !important;
            color: #1A497C !important;
        }
        button[kind="secondary"], [data-testid="stBaseButton-secondary"], [data-testid="stDownloadButton"] button {
            background-color: #FFFFFF !important;
            border: 1px solid #E2E8F0 !important;
            color: #334155 !important;
            border-radius: 9px !important;
            font-weight: 500 !important;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03) !important;
        }
        button[kind="secondary"]:hover, [data-testid="stBaseButton-secondary"]:hover, [data-testid="stDownloadButton"] button:hover {
            background-color: #F4F9FF !important;
            border-color: #B8D8FF !important;
            color: #2B6CB0 !important;
        }
        /* 체크박스·라디오·슬라이더의 쨍한 원색 블루를 차분한 세룰리안(#5B9BD5)으로 완화 */
        div[data-baseweb="checkbox"] > div:first-child,
        div[role="radiogroup"] label > div:first-child {
            filter: saturate(0.68) brightness(1.06);
        }
        div[data-baseweb="slider"] {
            filter: saturate(0.65) brightness(1.05);
        }
        /* 좌측 사이드바 AirROI 페이지 진입형 네비게이션 메뉴 버튼 스타일 */
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) {
            gap: 4px !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) button {
            display: flex !important;
            justify-content: flex-start !important;
            align-items: center !important;
            text-align: left !important;
            padding: 9px 13px !important;
            border-radius: 8px !important;
            min-height: 40px !important;
            box-shadow: none !important;
            transition: all 0.14s ease !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) button > div {
            width: 100% !important;
            display: flex !important;
            justify-content: space-between !important;
            align-items: center !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) button[kind="secondary"],
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) [data-testid="stBaseButton-secondary"] {
            background-color: transparent !important;
            border: 1px solid transparent !important;
            color: #475569 !important;
            font-weight: 500 !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) button[kind="secondary"] p,
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) [data-testid="stBaseButton-secondary"] p {
            color: #475569 !important;
            font-weight: 500 !important;
            font-size: 0.88rem !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) button[kind="secondary"]:hover,
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) [data-testid="stBaseButton-secondary"]:hover {
            background-color: #F1F5F9 !important;
            border-color: #E2E8F0 !important;
            color: #1E293B !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) button[kind="primary"],
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) [data-testid="stBaseButton-primary"] {
            background-color: #E6F2FF !important;
            border: 1px solid #C3DAFE !important;
            border-left: 3.5px solid #4A90E2 !important;
            color: #1E4E8C !important;
            font-weight: 600 !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) button[kind="primary"] p,
        div[data-testid="stVerticalBlock"]:has(> div .airroi-nav-marker) [data-testid="stBaseButton-primary"] p {
            color: #1E4E8C !important;
            font-weight: 600 !important;
            font-size: 0.88rem !important;
        }
        /* 모바일 화면(768px 이하) 반응형 최적화 및 2열·3열 그리드 강제 유지 */
        @media (max-width: 768px) {
            .block-container {
                padding-top: 1.6rem !important;
                padding-left: 0.65rem !important;
                padding-right: 0.65rem !important;
                max-width: 100% !important;
            }
            .dashboard-header {
                flex-direction: column;
                align-items: flex-start;
                gap: 6px;
                padding: 0.65rem 0.9rem;
            }
            .dashboard-title {
                font-size: 1.04rem;
            }
            [data-testid="stMetric"] {
                padding: 9px 11px !important;
            }
            [data-testid="stMetricValue"] {
                font-size: 1.12rem !important;
            }
        }
        /* 모바일에서도 가로 2열을 유지해야 하는 컨테이너(.m-two-col) */
        div[data-testid="stVerticalBlock"]:has(> div .m-two-col) div[data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            gap: 6px !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .m-two-col) div[data-testid="stColumn"] {
            width: calc(50% - 3px) !important;
            flex: 1 1 calc(50% - 3px) !important;
            min-width: calc(50% - 3px) !important;
        }
        /* 모바일 상단 메뉴 그리드(.m-nav-marker) AirROI 파스텔 세룰리안 필 디자인 */
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) button {
            border-radius: 9px !important;
            min-height: 36px !important;
            padding: 5px 10px !important;
            font-size: 0.80rem !important;
            justify-content: flex-start !important;
            text-align: left !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) button p {
            font-size: 0.80rem !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) button[kind="secondary"],
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) [data-testid="stBaseButton-secondary"] {
            background-color: #F8FAFC !important;
            border: 1px solid #E2E8F0 !important;
            color: #475569 !important;
            font-weight: 500 !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) button[kind="primary"],
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) [data-testid="stBaseButton-primary"] {
            background-color: #E6F2FF !important;
            border: 1px solid #BFDBFE !important;
            border-left: 3.5px solid #4A90E2 !important;
            color: #1E4E8C !important;
            font-weight: 600 !important;
        }
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) button[kind="primary"] p,
        div[data-testid="stVerticalBlock"]:has(> div .m-nav-marker) [data-testid="stBaseButton-primary"] p {
            color: #1E4E8C !important;
            font-weight: 600 !important;
        }
        /* 탭 메뉴를 AirROI의 Overview 선택 필(#E6F2FF 배경 + #2B6CB0 텍스트) 형태로 스타일링 */
        div[data-baseweb="tab-list"] {
            gap: 6px !important;
            border-bottom: 1px solid #E2E8F0 !important;
            padding-bottom: 6px !important;
        }
        button[data-baseweb="tab"] {
            border-radius: 8px !important;
            padding: 7px 14px !important;
            font-weight: 500 !important;
            color: #64748B !important;
            background-color: transparent !important;
            transition: all 0.15s ease !important;
        }
        button[data-baseweb="tab"]:hover {
            color: #334155 !important;
            background-color: #F1F5F9 !important;
        }
        button[data-baseweb="tab"][aria-selected="true"] {
            background-color: #E6F2FF !important;
            color: #2B6CB0 !important;
            font-weight: 600 !important;
        }
        button[data-baseweb="tab"][aria-selected="true"] p {
            color: #2B6CB0 !important;
            font-weight: 600 !important;
        }
        div[data-baseweb="tab-highlight"] {
            background-color: transparent !important;
        }
        .dashboard-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: #FFFFFF;
            border: 1px solid #EDF2F7;
            border-radius: 12px;
            padding: 0.78rem 1.2rem;
            margin-bottom: 1.0rem;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.03);
        }
        .dashboard-title {
            font-family: var(--font-brand) !important;
            font-size: 1.18rem;
            font-weight: 600;
            color: #1E293B;
            letter-spacing: -0.02em;
            margin: 0;
        }
        .dashboard-subtitle {
            font-size: 0.80rem;
            color: #64748B;
            margin-top: 3px;
        }
        [data-testid="stMetric"] {
            border: 1px solid #EDF2F7;
            border-radius: 12px;
            padding: 14px 17px;
            background: #FFFFFF !important;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.03);
        }
        [data-testid="stMetricLabel"] {
            font-size: 0.80rem !important;
            color: #64748B !important;
            font-weight: 500 !important;
        }
        [data-testid="stMetricValue"] {
            font-family: var(--font-brand) !important;
            font-size: 1.38rem !important;
            font-weight: 700 !important;
            color: #1E293B !important;
            letter-spacing: -0.02em !important;
        }
        [data-testid="stMetricDelta"] {
            font-size: 0.77rem !important;
            color: #2B6CB0 !important;
            background: #F0F7FF !important;
            padding: 2px 8px !important;
            border-radius: 6px !important;
        }
        .section-title {
            font-family: var(--font-brand) !important;
            font-size: 1.0rem;
            font-weight: 600;
            color: #1E293B;
            margin-top: 0.4rem;
            margin-bottom: 0.35rem;
            letter-spacing: -0.015em;
        }
        .mode-badge {
            display: inline-block;
            padding: 5px 13px;
            border-radius: 999px;
            font-size: 0.74rem;
            font-weight: 600;
            letter-spacing: -0.01em;
        }
        .mode-admin {
            background: #EBF4FF;
            color: #2B6CB0;
            border: 1px solid #C3DAFE;
        }
        .mode-normal {
            background: #F1F5F9;
            color: #64748B;
            border: 1px solid #E2E8F0;
        }
        .compare-card {
            border: 1px solid #EDF2F7;
            border-radius: 12px;
            padding: 16px;
            background: #FFFFFF;
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.04);
            height: 100%;
        }
        .compare-card-title {
            font-family: var(--font-brand) !important;
            font-size: 1.0rem;
            font-weight: 600;
            color: #2B6CB0;
            margin-bottom: 4px;
        }
        .compare-card-sub {
            font-size: 0.81rem;
            color: #64748B;
            margin-bottom: 10px;
        }
        .tag-pill {
            display: inline-block;
            padding: 2px 9px;
            margin: 2px 4px 2px 0;
            border-radius: 6px;
            font-size: 0.75rem;
            background: #F0F7FF;
            color: #2B6CB0;
            border: 1px solid #C3DAFE;
            font-weight: 500;
        }
        .unlinked-hover-wrap {
            position: relative;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 4px 11px;
            margin: 4px 0 6px 0;
            border-radius: 999px;
            background: #F0F7FF;
            border: 1px solid #BFDBFE;
            color: #235A96;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: help;
        }
        .unlinked-hover-box {
            visibility: hidden;
            opacity: 0;
            position: absolute;
            left: 0;
            bottom: 125%;
            width: 390px;
            max-width: 88vw;
            background: #0F172A;
            color: #F8FAFC;
            padding: 12px 14px;
            border-radius: 10px;
            font-size: 0.77rem;
            font-weight: 400;
            line-height: 1.55;
            box-shadow: 0 10px 25px rgba(15, 23, 42, 0.25);
            z-index: 9999;
            transition: opacity 0.15s ease;
            pointer-events: none;
        }
        .unlinked-hover-wrap:hover .unlinked-hover-box {
            visibility: visible;
            opacity: 1;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


_UNLINKED_REASON_TOOLTIP = (
    "💡 [에어비앤비 미연결 영업 업소 분류 사유]\n"
    "행정안전부 LOCALDATA 인허가 대장상 '영업/정상' 상태이나 아래 사유로 에어비앤비와 연결되지 않은 숙소입니다.\n\n"
    "① 타 플랫폼 전용·자체 채널 운영: 에어비앤비에 입점하지 않고 야놀자·여기어때·부킹닷컴·아고다·네이버예약·자체 홈페이지 등으로만 영업 중인 숙소\n"
    "② 달력 닫힘·장기 만실: 에어비앤비 예약 달력을 닫아두었거나(호스팅 일시중지) 장기 투숙·최소 숙박일수 제한으로 검색 노출에서 제외된 숙소\n"
    "③ 상호명 상이·인허가번호 비공개: 공공 인허가 대장의 상호명과 에어비앤비 노출 숙소명이 완전히 다르고 인허가번호도 표기되지 않은 숙소"
)


def _clean_text(val: Any, default: str = "-") -> str:
    if val is None or pd.isna(val):
        return default
    s = html_lib.unescape(str(val)).strip()
    return s if s else default


def _format_top_segments(raw_json: Any, top_n: int = 2) -> str:
    data = parse_json_col(raw_json, {})
    if not isinstance(data, dict) or not data:
        return "-"
    items = sorted(data.items(), key=lambda x: float(x[1]), reverse=True)[:top_n]
    return " · ".join(f"{k} {int(round(float(v)))}%" for k, v in items)


def _format_occupancy_badge(occ_14: Any, occ_30: Any) -> str:
    has_14 = occ_14 is not None and not pd.isna(occ_14)
    has_30 = occ_30 is not None and not pd.isna(occ_30)
    if has_14 and has_30:
        return f"14일 {float(occ_14):.0f}% / 30일 {float(occ_30):.0f}%"
    if has_30:
        return f"30일 {float(occ_30):.0f}%"
    return "수집 대기"


def _fmt_krw(val: Any) -> str:
    if val is None or pd.isna(val):
        return "-"
    try:
        v = int(round(float(val)))
        return f"{v:,}원" if v > 0 else "-"
    except (TypeError, ValueError):
        return "-"


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or pd.isna(val):
        return default
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return default


def _safe_str(val: Any, default: str = "-") -> str:
    return _clean_text(val, default)


def _industry_color(industry: str, status: str = "") -> str:
    if status and status != "영업/정상":
        return "gray"
    if industry == INDUSTRY_HOSTEL:
        return "blue"
    if industry == INDUSTRY_URBAN_MINBAK:
        return "orange"
    return "blue"


def _build_airroi_map_card_html(
    pub: pd.Series,
    include_link: bool = False,
    same_building_idx: int = 1,
    same_building_total: int = 1,
) -> str:
    """AirROI 지도 호버 카드와 동일한 레이아웃(상단 실제 에어비앤비 숙소 대표사진 + 숙소명 + 객실유형 + 도로명주소 + 3단 지표) HTML을 생성한다."""
    ind = _safe_str(pub.get("industry_type"), "숙박업")
    ind_short = "호스텔업" if ind == INDUSTRY_HOSTEL else "외도민"
    biz_name = html_lib.escape(_safe_str(pub.get("business_name"), "-"))
    air_title = html_lib.escape(_clean_text(pub.get("matched_airbnb_title"), biz_name))
    room_cnt = _safe_int(pub.get("matched_room_count"), 0)
    max_cap = _safe_int(pub.get("max_capacity"), 0)
    room_cat = html_lib.escape(_safe_str(pub.get("room_category"), "일반 객실"))

    if room_cnt > 0:
        sub_parts = [f"[{ind_short}] {biz_name}", room_cat]
        if max_cap > 0:
            sub_parts.append(f"최대 {max_cap}인")
        sub_line = " · ".join(sub_parts)
    else:
        sub_line = f"[{ind_short}] {biz_name} · 에어비앤비 미연결"

    raw_addr = _safe_str(pub.get("road_address"), "-")
    short_addr = html_lib.escape(raw_addr.replace("부산광역시 ", ""))

    rating_val = pub.get("matched_rating")
    rev_cnt = _safe_int(pub.get("matched_reviews_count"), 0)
    rating_badge = (
        f"★ {float(rating_val):.2f} ({rev_cnt:,})"
        if pd.notna(rating_val) and float(rating_val) > 0
        else (f"리뷰 {rev_cnt:,}개" if rev_cnt > 0 else "")
    )

    thumb = _safe_str(pub.get("matched_thumbnail_url"), "")
    has_real_thumb = bool(thumb and thumb != "-" and thumb.startswith("http"))
    if has_real_thumb and "muscache.com" in thumb and "?" not in thumb:
        thumb = f"{thumb}?im_w=720"

    badge_overlay = (
        f'<div style="position:absolute; top:8px; right:8px; background:rgba(15,23,42,0.78); color:#FFFFFF; '
        f'font-size:11px; font-weight:600; padding:3px 8px; border-radius:999px;">{rating_badge}</div>'
        if rating_badge
        else ""
    )
    building_overlay = (
        f'<div style="position:absolute; top:8px; left:8px; background:rgba(37,99,235,0.88); color:#FFFFFF; '
        f'font-size:10.5px; font-weight:600; padding:2.5px 7px; border-radius:999px;">동일건물 {same_building_idx}/{same_building_total}</div>'
        if same_building_total > 1
        else ""
    )

    if has_real_thumb:
        safe_photo_src = html_lib.escape(thumb)
        top_media_html = f"""
        <div style="position:relative; width:272px; height:152px; background:#F1F5F9; overflow:hidden;">
            <img src="{safe_photo_src}"
                 onerror="this.parentElement.style.display='none'"
                 style="width:100%; height:100%; object-fit:cover; display:block;" />
            {building_overlay}
            {badge_overlay}
        </div>
        """
    else:
        top_media_html = f"""
        <div style="position:relative; width:272px; height:64px; background:#F1F5F9; border-bottom:1px solid #E2E8F0;
                    display:flex; align-items:center; justify-content:space-between; padding:0 14px; box-sizing:border-box;">
            <span style="font-size:12px; font-weight:600; color:#64748B;">[{ind_short}] {biz_name}</span>
            {badge_overlay}
        </div>
        """

    wd_str = _fmt_krw(pub.get("weekday_total"))
    we_str = _fmt_krw(pub.get("weekend_total"))
    occ_30 = pub.get("occ_30d_pct")
    occ_main = f"{float(occ_30):.1f} %" if pd.notna(occ_30) else "-"

    link_html = ""
    matched_url = _safe_str(pub.get("matched_url"), "")
    if include_link:
        link_btn = (
            f'<a href="{html_lib.escape(matched_url)}" target="_blank" '
            f'style="display:block; margin-top:8px; text-align:center; background:#2563EB; color:#FFFFFF; '
            f'text-decoration:none; font-size:12px; font-weight:600; padding:7px 0; border-radius:8px;">'
            f'에어비앤비 숙소 열기 ↗</a>'
            if matched_url and matched_url != "-"
            else ""
        )
        link_html = f"""
        <div style="margin-top:8px; padding-top:6px; border-top:1px solid #F1F5F9;">
            {link_btn}
        </div>
        """

    if room_cnt > 0:
        bottom_metrics_html = f"""
            <div style="display:flex; justify-content:space-between; align-items:flex-start; padding-top:2px;">
                <div>
                    <div style="font-size:13.5px; font-weight:800; color:#0F172A;">{wd_str}</div>
                    <div style="font-size:11px; color:#64748B; margin-top:2px;">평일 기본가</div>
                </div>
                <div style="text-align:center;">
                    <div style="font-size:13.5px; font-weight:800; color:#0F172A;">{occ_main}</div>
                    <div style="font-size:11px; color:#64748B; margin-top:2px;">30일 예약률</div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:13.5px; font-weight:800; color:#0F172A;">{we_str}</div>
                    <div style="font-size:11px; color:#64748B; margin-top:2px;">주말 기본가</div>
                </div>
            </div>
        """
    else:
        bottom_metrics_html = """
            <div style="background:#F8FAFC; border:1px solid #E2E8F0; border-radius:8px; padding:8px 10px; font-size:11px; color:#475569; line-height:1.48;">
                <b style="color:#1E293B;">미연결 사유 안내 (인허가 영업중)</b><br>
                • 타 플랫폼(야놀자·부킹닷컴·자체예약 등) 전용 운영<br>
                • 에어비앤비 달력 닫힘(장기만실·일시중지)<br>
                • 인허가 상호명과 에어비앤비 노출 숙소명 상이
            </div>
        """

    ptr_rule = "" if include_link else "pointer-events:none;"
    return f"""
    <div style="width:272px; background:#FFFFFF; color:#0F172A; border-radius:12px; overflow:hidden;
                box-shadow:0 12px 28px rgba(15,23,42,0.18); border:1px solid #E2E8F0;
                font-family:'Pretendard Variable','Plus Jakarta Sans',-apple-system,sans-serif; white-space:normal; {ptr_rule}">
        {top_media_html}
        <div style="padding:12px 14px 13px 14px;">
            <div style="font-size:13.5px; font-weight:700; color:#0F172A; line-height:1.38;
                        display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;">
                {air_title}
            </div>
            <div style="font-size:11.8px; color:#475569; font-weight:600; margin-top:4px;">
                {sub_line}
            </div>
            <div style="font-size:11px; color:#64748B; margin-top:2px; margin-bottom:10px;">
                📍 {short_addr}
            </div>
            {bottom_metrics_html}
            {link_html}
        </div>
    </div>
    """


def _ensure_authenticated() -> tuple[bool, bool]:
    role = st.session_state.get("auth_role")
    if role == "admin":
        return True, True
    if role == "viewer":
        return True, False

    viewer_pw = get_env("VIEWER_PASSWORD", "")
    if not viewer_pw:
        return True, False

    st.markdown("#### DH 부산 호스텔/외도민 상권분석")
    st.caption("접속 비밀번호(일반모드 또는 관리자모드)를 입력해 주세요.")
    col1, _ = st.columns([1, 2])
    with col1:
        with st.form("login_gate_form"):
            pw_in = st.text_input("비밀번호", type="password")
            submitted = st.form_submit_button("확인", type="primary", width="stretch")
            if submitted:
                verified = verify_access_password(pw_in)
                if verified in ("admin", "viewer"):
                    st.session_state["auth_role"] = verified
                    st.rerun()
                else:
                    st.error("비밀번호가 일치하지 않습니다.")
    return False, False


def _get_or_start_cloudflare_url(force_new: bool = False, port: int = 8501) -> str:
    """Cloudflare 공식 고속 터널(data/cloudflared.exe)을 실행하거나 현재 활성 https://*.trycloudflare.com 링크를 반환한다."""
    cf_bin = DATA_DIR / "cloudflared.exe"
    log_file = DATA_DIR / "cloudflared.log"
    share_file = DATA_DIR / "mobile_share_url.txt"

    if not force_new and log_file.exists():
        try:
            txt = log_file.read_text(encoding="utf-8", errors="ignore")
            matches = re.findall(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", txt)
            if matches:
                out = subprocess.check_output(
                    ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="cp949",
                    errors="ignore",
                )
                if "cloudflared.exe" in out.lower():
                    return matches[-1].strip()
        except Exception:
            pass

    if not cf_bin.exists():
        return ""

    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "cloudflared.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if log_file.exists():
            log_file.unlink(missing_ok=True)
    except Exception:
        pass

    subprocess.Popen(
        [
            str(cf_bin),
            "tunnel",
            "--url",
            f"http://127.0.0.1:{port}",
            "--no-autoupdate",
            "--logfile",
            str(log_file),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    for _ in range(24):
        time.sleep(0.4)
        if log_file.exists():
            try:
                txt = log_file.read_text(encoding="utf-8", errors="ignore")
                matches = re.findall(r"(https://[a-zA-Z0-9-]+\.trycloudflare\.com)", txt)
                if matches:
                    url = matches[-1].strip()
                    share_file.write_text(url, encoding="utf-8")
                    return url
            except Exception:
                pass
    return ""


def _render_sidebar(is_admin: bool, current_zone: str) -> str:
    st.sidebar.markdown(
        """
        <div class="airroi-brand-box">
            <div class="airroi-logo-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#4A90E2" stroke-width="2.0" stroke-linecap="round" stroke-linejoin="round">
                    <line x1="3" y1="6" x2="21" y2="6"></line>
                    <circle cx="9" cy="6" r="2.2" fill="#FFFFFF" stroke="#4A90E2" stroke-width="2.0"></circle>
                    <line x1="3" y1="12" x2="21" y2="12"></line>
                    <circle cx="15" cy="12" r="2.2" fill="#FFFFFF" stroke="#5B9BD5" stroke-width="2.0"></circle>
                    <line x1="3" y1="18" x2="21" y2="18"></line>
                    <circle cx="10" cy="18" r="2.2" fill="#FFFFFF" stroke="#4A90E2" stroke-width="2.0"></circle>
                </svg>
            </div>
            <div>
                <div class="airroi-brand-title"><span class="airroi-brand-dh">DH</span>부산 호스텔/외도민 상권분석</div>
            </div>
        </div>
        <div class="sidebar-section-pill">
            <span>MARKET INSIGHTS · 분석 메뉴</span>
            <span>▾</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if is_admin:
        nav_items = [
            ("상권 요약 · 지도", "🗺️  상권 요약 · 지도"),
            ("통합 현황 · URL 등록", "📋  통합 현황 · URL 등록"),
            ("객단가(ADR) 맞춤 비교", "💰  객단가(ADR) 맞춤 비교"),
            ("고객층 · 포지셔닝 맞춤 비교", "👥  고객층 · 포지셔닝 비교"),
            ("외래객 수요 트렌드", "🌏  외래객 수요 트렌드"),
            ("시스템 설정", "⚙️  시스템 설정"),
        ]
    else:
        nav_items = [
            ("상권 요약 · 지도", "🗺️  상권 요약 · 지도"),
            ("통합 상권 데이터", "📋  통합 상권 데이터"),
            ("객단가(ADR) 맞춤 비교", "💰  객단가(ADR) 맞춤 비교"),
            ("고객층 · 포지셔닝 맞춤 비교", "👥  고객층 · 포지셔닝 비교"),
            ("외래객 수요 트렌드", "🌏  외래객 수요 트렌드"),
        ]

    valid_keys = [k for k, _ in nav_items]
    cur_nav = st.session_state.get("sidebar_nav_page", "상권 요약 · 지도")
    if cur_nav == "통합 상권 데이터" and is_admin:
        cur_nav = "통합 현황 · URL 등록"
    elif cur_nav == "통합 현황 · URL 등록" and not is_admin:
        cur_nav = "통합 상권 데이터"
    elif cur_nav not in valid_keys:
        cur_nav = "상권 요약 · 지도"
    st.session_state["sidebar_nav_page"] = cur_nav

    with st.sidebar.container():
        st.markdown('<div class="airroi-nav-marker" style="display:none;"></div>', unsafe_allow_html=True)
        for nav_key, nav_label in nav_items:
            is_active = cur_nav == nav_key
            btn_text = f"{nav_label}   ›" if is_active else nav_label
            if st.button(
                btn_text,
                key=f"nav_btn_{nav_key}",
                type="primary" if is_active else "secondary",
                width="stretch",
            ):
                if cur_nav != nav_key:
                    st.session_state["sidebar_nav_page"] = nav_key
                    st.rerun()

    selected_nav = st.session_state["sidebar_nav_page"]

    if is_admin:
        st.sidebar.markdown(
            '<div class="sidebar-section-pill" style="margin-top:18px;"><span>ADMIN · 관리자 메뉴</span><span>▾</span></div>',
            unsafe_allow_html=True,
        )
        if st.sidebar.button("일반모드로 전환", width="stretch"):
            st.session_state["auth_role"] = "viewer"
            st.session_state["sidebar_mode_radio"] = "일반모드"
            st.rerun()

        st.sidebar.markdown(
            '<div class="sidebar-section-pill"><span>DATA SYNC · 데이터 동기화</span><span>▾</span></div>',
            unsafe_allow_html=True,
        )
        run_calendar_now = st.sidebar.checkbox(
            "Apify 30일 달력 예약률 갱신",
            value=True,
            help="확정된 순수 호스텔·외도민 객실 ID만 Apify에 전달하여 향후 14일/30일 달력 예약률을 갱신합니다.",
        )
        run_price_now = st.sidebar.checkbox(
            "Apify 평일·주말 기본가 갱신",
            value=False,
            help="순수 호스텔·외도민 URL만 지정 호출하여 평일(D+7)/주말(D+14) 호스트 기본가를 수집합니다.",
        )
        discover_new_opt = st.sidebar.checkbox(
            "Apify 신규 숙소 발굴 스캔",
            value=False,
            help="Apify로 상권을 스캔한 뒤 생숙·모텔은 즉시 버리고 신규 호스텔·외도민만 추가합니다.",
        )
        max_listings_opt = st.sidebar.slider("최대 처리 객실 수", min_value=20, max_value=400, value=250, step=20)

        update_zone = "all" if current_zone == "all" else current_zone
        if st.sidebar.button("데이터 업데이트 실행", width="stretch", type="primary"):
            with st.sidebar.status("데이터 동기화 진행 중...", expanded=True) as status:
                if get_env("DATA_GO_KR_API_KEY"):
                    st.write("1) 공공데이터 OpenAPI 동기화 중...")
                    res_pub = sync_public_data_from_api(zone_filter="seomyeon" if update_zone == "all" else update_zone)
                    st.write(f"   → 공공데이터 {res_pub['total']}건 반영")
                else:
                    st.write("1) LOCALDATA 공식 인허가 파일 동기화 중...")
                    res_pub = sync_public_data_from_localdata_portal(zone_filter=None)
                    st.write(
                        f"   → 호스텔업 {res_pub['hostel_count']}건, "
                        f"외도민 {res_pub['urban_minbak_count']}건 반영"
                    )

                if (run_price_now or discover_new_opt) and get_env("APIFY_API_TOKEN"):
                    st.write("2) Apify 평일·주말 호스트 기본가 수집 중...")
                    apify_zone = "seomyeon" if update_zone == "all" else update_zone
                    res_air = sync_airbnb_via_apify(
                        zone_key=apify_zone,
                        max_listings=max_listings_opt,
                        hostel_only=True,
                        weekend_also=True,
                        targeted_urls_only=not discover_new_opt,
                        fetch_calendar_30d=run_calendar_now,
                    )
                    st.write(f"   → Apify 수집 완료: {res_air}")
                elif run_calendar_now and get_env("APIFY_API_TOKEN"):
                    st.write("2) Apify 향후 30일 달력 예약률 갱신 중...")
                    res_cal = sync_calendar_30d_via_apify(
                        zone_key=update_zone,
                        max_items=max_listings_opt,
                    )
                    st.write(f"   → 30일 달력 {res_cal.get('updated', 0)}건 반영")
                else:
                    st.write("2) Apify 호출 건너뜀 (기존 저장 데이터 유지)")

                st.write("3) 인허가 데이터 정제 및 상호 매칭 중...")
                purge_non_target_records()
                matched = run_auto_matching(zone_key="all")
                st.write(f"   → {matched}개 객실 매칭 완료")

                st.write("4) 고객층 및 포지셔닝 분석 중...")
                analyzed = analyze_all_listings(zone_key="all", use_ai=bool(get_env("GEMINI_API_KEY")))
                st.write(f"   → {analyzed['total']}개 객실 분석 완료")
                status.update(label="동기화 완료", state="complete")
            st.cache_data.clear()
            st.rerun()

        st.sidebar.markdown(
            '<div class="sidebar-section-pill"><span>SHARE LINK · 카톡 외부 공유 링크</span><span>▾</span></div>',
            unsafe_allow_html=True,
        )
        cf_url = _get_or_start_cloudflare_url(force_new=False, port=8501)
        if st.sidebar.button("새 공유 링크 발급", width="stretch", key="btn_cf_refresh"):
            with st.sidebar.status("Cloudflare 고속 링크 발급 중...", expanded=False):
                cf_url = _get_or_start_cloudflare_url(force_new=True, port=8501)
            st.rerun()
        if cf_url:
            st.sidebar.code(cf_url, language=None)
            st.sidebar.caption("위 주소를 복사해 카카오톡으로 보내면 PC·휴대폰 어디서나 즉시 열립니다.")
    else:
        st.sidebar.write("")
        with st.sidebar.expander("🔒 관리자 모드", expanded=False):
            admin_pw_input = st.text_input("관리자 비밀번호", type="password", key="sidebar_admin_pw")
            if st.button("관리자 전환", width="stretch", type="primary"):
                if verify_access_password(admin_pw_input) == "admin":
                    st.session_state["auth_role"] = "admin"
                    st.session_state["sidebar_mode_radio"] = "관리자모드"
                    st.rerun()
                else:
                    st.error("비밀번호가 일치하지 않습니다.")

    return selected_nav


def _is_mobile_user_agent() -> bool:
    """현재 접속 브라우저가 스마트폰(iPhone, Android, 카카오톡 인앱브라우저)인지 판별한다."""
    try:
        ua = str(st.context.headers.get("User-Agent") or st.context.headers.get("user-agent") or "").lower()
        return any(k in ua for k in ("iphone", "android", "mobile", "kakaotalk", "ipad"))
    except Exception:
        return False


def _init_mobile_view_from_user_agent() -> None:
    """스마트폰 접속 시 초기 화면을 자동으로 모바일 모드로 시작한다."""
    if "is_mobile_view" in st.session_state:
        return
    st.session_state["is_mobile_view"] = _is_mobile_user_agent()


def _apply_viewport_mode(is_mobile_view: bool) -> None:
    """휴대폰에서 'PC 버전으로 보기' 전환 시 실제 1360px 데스크톱 뷰포트와 좌측 사이드바가 그대로 열리도록 제어한다."""
    if is_mobile_view:
        components.html(
            """
            <script>
            (function() {
                try {
                    var doc = window.parent.document;
                    var vp = doc.querySelector('meta[name="viewport"]');
                    if (vp) {
                        vp.setAttribute('content', 'width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes');
                    }
                    doc.documentElement.style.minWidth = '';
                    doc.body.style.minWidth = '';
                } catch (e) {}
            })();
            </script>
            """,
            height=0,
        )
    else:
        components.html(
            """
            <script>
            (function() {
                try {
                    var doc = window.parent.document;
                    var vp = doc.querySelector('meta[name="viewport"]');
                    if (vp) {
                        vp.setAttribute('content', 'width=1360, initial-scale=0.30, minimum-scale=0.20, maximum-scale=3.0, user-scalable=yes');
                    }
                    var expandBtn = doc.querySelector('[data-testid="stExpandSidebarButton"], [data-testid="collapsedControl"] button');
                    var sb = doc.querySelector('section[data-testid="stSidebar"]');
                    if (sb && sb.getAttribute('aria-expanded') === 'false' && expandBtn) {
                        expandBtn.click();
                    }
                } catch (e) {}
            })();
            </script>
            """,
            height=0,
        )


def _render_mobile_top_nav(is_admin: bool, is_mobile_view: bool = True) -> str:
    """PC 좌측 사이드바와 100% 동일한 메뉴 이름을 2열 AirROI 카드 그리드로 상단에 깔끔하게 렌더링한다."""
    if is_admin:
        mobile_navs = [
            ("상권 요약 · 지도", "🗺️ 상권 요약 · 지도"),
            ("통합 현황 · URL 등록", "📋 통합 현황 · URL 등록"),
            ("객단가(ADR) 맞춤 비교", "💰 객단가(ADR) 맞춤 비교"),
            ("고객층 · 포지셔닝 맞춤 비교", "👥 고객층 · 포지셔닝 비교"),
            ("외래객 수요 트렌드", "🌏 외래객 수요 트렌드"),
            ("시스템 설정", "⚙️ 시스템 설정"),
        ]
    else:
        mobile_navs = [
            ("상권 요약 · 지도", "🗺️ 상권 요약 · 지도"),
            ("통합 상권 데이터", "📋 통합 상권 데이터"),
            ("객단가(ADR) 맞춤 비교", "💰 객단가(ADR) 맞춤 비교"),
            ("고객층 · 포지셔닝 맞춤 비교", "👥 고객층 · 포지셔닝 비교"),
            ("외래객 수요 트렌드", "🌏 외래객 수요 트렌드"),
        ]

    cur_nav = st.session_state.get("sidebar_nav_page", "상권 요약 · 지도")
    valid_keys = [k for k, _ in mobile_navs]
    if cur_nav not in valid_keys:
        cur_nav = "상권 요약 · 지도"
        st.session_state["sidebar_nav_page"] = cur_nav

    st.markdown(
        """
        <div style="display:flex; align-items:center; justify-content:space-between; background:#F8FAFC;
                    border:1px solid #E2E8F0; border-radius:10px; padding:8px 12px; margin-bottom:6px;">
            <div style="display:flex; align-items:center; gap:7px;">
                <span style="font-weight:800; color:#235A96; font-size:0.95rem;">DH</span>
                <span style="font-weight:700; color:#1E293B; font-size:0.90rem;">부산 호스텔/외도민 상권분석</span>
            </div>
            <span style="font-size:0.72rem; font-weight:600; color:#4A90E2; background:#E6F2FF; padding:2px 8px; border-radius:999px;">분석 메뉴</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.container():
        st.markdown('<div class="m-two-col m-nav-marker" style="display:none;"></div>', unsafe_allow_html=True)
        for i in range(0, len(mobile_navs), 2):
            pair = mobile_navs[i : i + 2]
            cols = st.columns(2)
            for col_idx, (n_key, n_lbl) in enumerate(pair):
                with cols[col_idx]:
                    is_act = cur_nav == n_key
                    lbl_txt = f"{n_lbl} ›" if is_act else n_lbl
                    if st.button(
                        lbl_txt,
                        key=f"m_top_nav_{n_key}",
                        type="primary" if is_act else "secondary",
                        width="stretch",
                    ):
                        if cur_nav != n_key:
                            st.session_state["sidebar_nav_page"] = n_key
                            st.rerun()

    return st.session_state["sidebar_nav_page"]


def _render_top_filter_bar() -> tuple[str, List[str], bool]:
    """AirROI 상단 필터 바 스타일(분석 상권 + 대상 업종 + PC/모바일 전환 + 엑셀 다운로드)을 렌더링한다."""
    zone_options = {
        **{k: v.name for k, v in ZONES.items()},
        "all": "부산 5대 상권 전체",
    }
    default_zone = get_env("DEFAULT_ZONE", "seomyeon")
    zone_keys = list(zone_options.keys())
    default_idx = zone_keys.index(default_zone) if default_zone in zone_keys else 0
    is_mobile_view = bool(st.session_state.get("is_mobile_view", False))
    industry_choices = [INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK]

    if is_mobile_view:
        with st.container():
            st.markdown('<div class="m-two-col" style="display:none;"></div>', unsafe_allow_html=True)
            m_c1, m_c2 = st.columns([1.3, 1.0])
            with m_c1:
                selected_zone = st.selectbox(
                    "분석 상권",
                    options=zone_keys,
                    index=default_idx,
                    format_func=lambda k: zone_options[k],
                    key="top_zone_select",
                )
            with m_c2:
                st.write("")
                if st.button("🖥️ PC 버전 전환", key="btn_toggle_mobile_view", type="secondary", width="stretch"):
                    st.session_state["is_mobile_view"] = False
                    st.rerun()
        with st.expander("▸ 대상 업종 선택 · 엑셀 다운로드", expanded=False):
            selected_industries = st.multiselect(
                "대상 업종",
                options=industry_choices,
                default=industry_choices,
                key="top_industry_multiselect",
            )
            xlsx_bytes = _cached_export_excel(selected_zone)
            st.download_button(
                label="엑셀 리포트 다운로드 (.xlsx)",
                data=xlsx_bytes,
                file_name=f"busan_hostel_intel_{selected_zone}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
    else:
        t_col1, t_col2, t_col3, t_col4 = st.columns([1.2, 1.95, 0.72, 0.92])
        with t_col1:
            selected_zone = st.selectbox(
                "분석 상권",
                options=zone_keys,
                index=default_idx,
                format_func=lambda k: zone_options[k],
                key="top_zone_select",
            )
        with t_col2:
            selected_industries = st.multiselect(
                "대상 업종",
                options=industry_choices,
                default=industry_choices,
                key="top_industry_multiselect",
            )
        with t_col3:
            st.write("")
            if st.button("📱 모바일 버전 전환", key="btn_toggle_mobile_view", type="secondary", width="stretch"):
                st.session_state["is_mobile_view"] = True
                st.rerun()
        with t_col4:
            st.write("")
            xlsx_bytes = _cached_export_excel(selected_zone)
            st.download_button(
                label="엑셀 리포트 다운로드 (.xlsx)",
                data=xlsx_bytes,
                file_name=f"busan_hostel_intel_{selected_zone}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )

    return selected_zone, selected_industries, bool(st.session_state.get("is_mobile_view", False))


def _render_mobile_card_list(view_df: pd.DataFrame, key_prefix: str) -> None:
    """모바일에서 실제 에어비앤비 썸네일 사진 + 핵심 지표를 보여주고 스크롤 시 오클릭이 발생하지 않는 프리미엄 카드 리스트."""
    total_cnt = len(view_df)
    page_size = 12
    total_pages = max(1, (total_cnt + page_size - 1) // page_size)
    page_key = f"{key_prefix}_m_page"
    cur_page = int(st.session_state.get(page_key, 1))
    if cur_page < 1 or cur_page > total_pages:
        cur_page = 1
        st.session_state[page_key] = 1

    start_idx = (cur_page - 1) * page_size
    end_idx = min(total_cnt, start_idx + page_size)
    page_df = view_df.iloc[start_idx:end_idx]

    cards_html_parts: List[str] = []
    for _, row in page_df.iterrows():
        ind_val = str(row.get("업종 구분") or "")
        ind_short = "호스텔업" if "호스텔" in ind_val else "외도민"
        ind_bg = "#EBF4FF" if ind_short == "호스텔업" else "#FFF7ED"
        ind_fg = "#235A96" if ind_short == "호스텔업" else "#C2410C"

        biz_nm = html_lib.escape(str(row.get("공공 상호명") or "-"))
        air_nm = html_lib.escape(str(row.get("에어비앤비 숙소명") or "-"))
        room_info = html_lib.escape(str(row.get("객실유형 · 인원") or "-"))
        occ_info = html_lib.escape(str(row.get("예약률 (14일 / 30일)") or "-"))
        wd_str = _fmt_krw(row.get("평일 기본 객단가"))
        we_str = _fmt_krw(row.get("주말 기본 객단가"))
        addr_raw = str(row.get("도로명주소") or "-").replace("부산광역시 ", "")
        addr_str = html_lib.escape(addr_raw)

        r_val = row.get("평점")
        rev_val = _safe_int(row.get("리뷰수"), 0)
        rating_str = f"★ {float(r_val):.2f} ({rev_val:,}개)" if pd.notna(r_val) and float(r_val) > 0 else f"리뷰 {rev_val:,}개"

        thumb_url = str(row.get("_thumb_url") or "").strip()
        if thumb_url and thumb_url != "-" and thumb_url.startswith("http"):
            if "muscache.com" in thumb_url and "?" not in thumb_url:
                thumb_url = f"{thumb_url}?im_w=320"
            thumb_html = (
                f'<img src="{html_lib.escape(thumb_url)}" '
                f'onerror="this.style.display=\'none\'" '
                f'style="width:76px; height:76px; border-radius:9px; object-fit:cover; flex-shrink:0; background:#F1F5F9; border:1px solid #E2E8F0;" />'
            )
        else:
            thumb_html = (
                f'<div style="width:76px; height:76px; border-radius:9px; flex-shrink:0; background:#F8FAFC; border:1px solid #E2E8F0; '
                f'display:flex; align-items:center; justify-content:center; font-size:11px; color:#94A3B8; font-weight:600;">{ind_short}</div>'
            )

        url_val = str(row.get("에어비앤비") or "").strip()
        link_btn_html = (
            f'<a href="{html_lib.escape(url_val)}" target="_blank" rel="noopener noreferrer" '
            f'style="display:inline-block; padding:4px 11px; background:#EBF4FF; color:#1E4E8C; '
            f'border:1px solid #BFDBFE; border-radius:6px; font-size:11.5px; font-weight:600; '
            f'text-decoration:none; touch-action:pan-y;">숙소 열기 ↗</a>'
            if url_val and url_val != "-" and url_val.startswith("http")
            else ""
        )

        cards_html_parts.append(
            f"""
            <div style="background:#FFFFFF; border:1px solid #E2E8F0; border-radius:12px; padding:11px 12px;
                        margin-bottom:9px; box-shadow:0 2px 6px rgba(15,23,42,0.04); touch-action:pan-y;">
                <div style="display:flex; gap:11px; align-items:flex-start;">
                    {thumb_html}
                    <div style="flex:1; min-width:0;">
                        <div style="display:flex; justify-content:space-between; align-items:center; gap:4px;">
                            <div style="font-size:13.5px; font-weight:700; color:#0F172A; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                                <span style="display:inline-block; padding:1px 6px; border-radius:5px; font-size:10.5px;
                                             background:{ind_bg}; color:{ind_fg}; margin-right:4px;">{ind_short}</span>{biz_nm}
                            </div>
                            <div style="font-size:11px; font-weight:700; color:#1E4E8C; background:#E6F2FF;
                                        padding:2px 7px; border-radius:999px; white-space:nowrap; flex-shrink:0;">{occ_info}</div>
                        </div>
                        <div style="font-size:11.8px; color:#475569; margin-top:3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                            {air_nm}
                        </div>
                        <div style="font-size:11px; color:#64748B; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                            {rating_str} · {room_info}
                        </div>
                        <div style="font-size:10.8px; color:#94A3B8; margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                            📍 {addr_str}
                        </div>
                    </div>
                </div>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:8px; padding-top:7px; border-top:1px solid #F1F5F9;">
                    <div>{link_btn_html}</div>
                    <div style="font-size:12.2px; color:#0F172A; text-align:right;">
                        평일 <b style="color:#1E4E8C;">{wd_str}</b> · 주말 <b>{we_str}</b>
                    </div>
                </div>
            </div>
            """
        )

    st.markdown("".join(cards_html_parts), unsafe_allow_html=True)

    if total_pages > 1:
        with st.container():
            st.markdown('<div class="m-two-col" style="display:none;"></div>', unsafe_allow_html=True)
            p_c1, p_c2 = st.columns(2)
            with p_c1:
                if st.button(f"◀ 이전 ({cur_page}/{total_pages})", key=f"{key_prefix}_prev_p", disabled=(cur_page <= 1), width="stretch"):
                    st.session_state[page_key] = cur_page - 1
                    st.rerun()
            with p_c2:
                if st.button(f"다음 ({cur_page}/{total_pages}) ▶", key=f"{key_prefix}_next_p", disabled=(cur_page >= total_pages), width="stretch"):
                    st.session_state[page_key] = cur_page + 1
                    st.rerun()


def _render_native_table(view_df: pd.DataFrame, key_prefix: str, compact_mobile: bool = False) -> None:
    """PC에서는 전체 표(st.dataframe), 모바일에서는 오클릭 없는 프리미엄 썸네일 카드 리스트로 렌더링한다."""
    if compact_mobile:
        _render_mobile_card_list(view_df, key_prefix=key_prefix)
        return

    pc_cols = [c for c in view_df.columns if not c.startswith("_")]
    st.dataframe(
        view_df[pc_cols],
        column_config={
            "업종 구분": st.column_config.TextColumn("업종 구분", width=135),
            "공공 상호명": st.column_config.TextColumn("공공 상호명", width=165),
            "에어비앤비 숙소명": st.column_config.TextColumn("에어비앤비 숙소명", width=310),
            "객실유형 · 인원": st.column_config.TextColumn("객실유형 · 인원", width=185),
            "예약률 (14일 / 30일)": st.column_config.TextColumn("예약률 (14일 / 30일)", width=155),
            "평점": st.column_config.NumberColumn("평점", format="%.2f", width=75),
            "리뷰수": st.column_config.NumberColumn("리뷰수", format="%,d개", width=95),
            "평일 기본 객단가": st.column_config.NumberColumn("평일 기본 객단가", format="%,d원", width=135),
            "주말 기본 객단가": st.column_config.NumberColumn("주말 기본 객단가", format="%,d원", width=135),
            "기준·추가인원 요금": st.column_config.TextColumn("기준·추가인원 요금", width=165),
            "주요 고객층": st.column_config.TextColumn("주요 고객층", width=175),
            "도로명주소": st.column_config.TextColumn("도로명주소", width=230),
            "시설면적 · 인허가일": st.column_config.TextColumn("시설면적 · 인허가일", width=155),
            "에어비앤비": st.column_config.LinkColumn("에어비앤비", display_text="숙소 열기 ↗", width=105),
        },
        width="stretch",
        hide_index=True,
        height=650,
        row_height=38,
        placeholder="-",
        key=f"{key_prefix}_df",
    )


def _render_unified_table(
    pub_df: pd.DataFrame,
    comp_df: pd.DataFrame,
    key_prefix: str = "t1",
) -> None:
    """영업중 호스텔업·외도민 데이터를 직관적인 네이티브 테이블로 렌더링한다."""
    compact_mobile = bool(st.session_state.get("is_mobile_view", False))
    st.markdown('<div class="section-title">통합 상권 데이터 조회 (휴·폐업 제외 · 영업중 호스텔업·외도민)</div>', unsafe_allow_html=True)

    if compact_mobile:
        with st.container():
            st.markdown('<div class="m-two-col" style="display:none;"></div>', unsafe_allow_html=True)
            r1_c1, r1_c2 = st.columns([1.3, 1.0])
            with r1_c1:
                search_kw = st.text_input(
                    "🔍 상호명 · 숙소명 · 주소 검색",
                    value="",
                    placeholder="상호명·도로명 (예: 무해, 광안린)",
                    key=f"{key_prefix}_kw",
                ).strip()
            with r1_c2:
                sort_by = st.selectbox(
                    "정렬 기준",
                    [
                        "리뷰 많은 순",
                        "30일 예약률 높은 순",
                        "14일 예약률 높은 순",
                        "평일 기본가 높은 순",
                        "평일 기본가 낮은 순",
                        "주말 기본가 높은 순",
                        "주말 기본가 낮은 순",
                        "평점 높은 순",
                        "상호명 가나다순",
                    ],
                    index=0,
                    key=f"{key_prefix}_sort",
                )
        with st.expander("▸ 상세 필터 (집계 기준 · 미연결 · 객실 형태)", expanded=False):
            view_mode = st.radio(
                "집계 기준",
                ["사업장(공공상호) 기준", "개별 객실(리스팅) 기준"],
                horizontal=True,
                key=f"{key_prefix}_view_mode",
            )
            scope_filter = st.selectbox(
                "표시 범위",
                ["에어비앤비 연결 완료만", "영업중 전체 (연결 우선)", "미연결 영업 업소만"],
                index=0,
                key=f"{key_prefix}_scope",
                help=_UNLINKED_REASON_TOOLTIP,
            )
            room_cat_filter = st.selectbox(
                "객실 형태",
                ["전체", "도미토리·다인실", "개인실", "독채·단체형"],
                index=0,
                key=f"{key_prefix}_rcat",
            )
    else:
        f1, f2, f3, f4, f5 = st.columns([1.3, 1.15, 0.95, 1.85, 1.25])
        with f1:
            view_mode = st.radio(
                "집계 기준",
                ["사업장(공공상호) 기준", "개별 객실(리스팅) 기준"],
                horizontal=True,
                key=f"{key_prefix}_view_mode",
            )
        with f2:
            scope_filter = st.selectbox(
                "표시 범위",
                ["에어비앤비 연결 완료만", "영업중 전체 (연결 우선)", "미연결 영업 업소만"],
                index=0,
                key=f"{key_prefix}_scope",
                help=_UNLINKED_REASON_TOOLTIP,
            )
        with f3:
            room_cat_filter = st.selectbox(
                "객실 형태",
                ["전체", "도미토리·다인실", "개인실", "독채·단체형"],
                index=0,
                key=f"{key_prefix}_rcat",
            )
        with f4:
            search_kw = st.text_input(
                "상호명 · 숙소명 · 주소 검색",
                value="",
                placeholder="상호명, 에어비앤비 숙소명, 도로명/동 입력 (예: 무해, 후추스테이, 광안린)",
                key=f"{key_prefix}_kw",
            ).strip()
        with f5:
            sort_by = st.selectbox(
                "정렬 기준",
                [
                    "리뷰 많은 순",
                    "30일 예약률 높은 순",
                    "14일 예약률 높은 순",
                    "평일 기본가 높은 순",
                    "평일 기본가 낮은 순",
                    "주말 기본가 높은 순",
                    "주말 기본가 낮은 순",
                    "평점 높은 순",
                    "상호명 가나다순",
                ],
                index=0,
                key=f"{key_prefix}_sort",
            )

    if view_mode.startswith("사업장"):
        base_active_df = pub_df[pub_df["status_name"] == "영업/정상"].copy() if not pub_df.empty else pub_df.copy()
        total_active_cnt = len(base_active_df)
        unmatched_all_df = (
            base_active_df[base_active_df["matched_room_count"] == 0].copy()
            if not base_active_df.empty
            else base_active_df.copy()
        )
        matched_cnt = total_active_cnt - len(unmatched_all_df)

        df = base_active_df.copy()
        if not df.empty:
            if scope_filter == "에어비앤비 연결 완료만":
                df = df[df["matched_room_count"] > 0]
            elif scope_filter == "미연결 영업 업소만":
                df = df[df["matched_room_count"] == 0]
        if room_cat_filter != "전체" and not df.empty:
            df = df[df["room_category"] == room_cat_filter]
        if search_kw and not df.empty:
            mask = (
                df["business_name"].fillna("").str.contains(search_kw, case=False, regex=False)
                | df["matched_airbnb_title"].fillna("").str.contains(search_kw, case=False, regex=False)
                | df["road_address"].fillna("").str.contains(search_kw, case=False, regex=False)
                | df["jibun_address"].fillna("").str.contains(search_kw, case=False, regex=False)
            )
            df = df[mask]

        if not df.empty:
            if sort_by == "30일 예약률 높은 순":
                df = df.sort_values(by=["occ_30d_pct", "matched_reviews_count"], ascending=[False, False], na_position="last")
            elif sort_by == "14일 예약률 높은 순":
                df = df.sort_values(by=["occ_14d_pct", "matched_reviews_count"], ascending=[False, False], na_position="last")
            elif sort_by == "평일 기본가 높은 순":
                df = df.sort_values(by=["weekday_total"], ascending=False, na_position="last")
            elif sort_by == "평일 기본가 낮은 순":
                df = df.sort_values(by=["weekday_total"], ascending=True, na_position="last")
            elif sort_by == "주말 기본가 높은 순":
                df = df.sort_values(by=["weekend_total"], ascending=False, na_position="last")
            elif sort_by == "주말 기본가 낮은 순":
                df = df.sort_values(by=["weekend_total"], ascending=True, na_position="last")
            elif sort_by == "평점 높은 순":
                df = df.sort_values(by=["matched_rating", "matched_reviews_count"], ascending=[False, False], na_position="last")
            elif sort_by == "상호명 가나다순":
                df = df.sort_values(by=["business_name"], ascending=True, na_position="last")
            else:
                df["_has_match"] = (df["matched_room_count"] > 0).astype(int)
                df = df.sort_values(
                    by=["_has_match", "matched_reviews_count", "matched_rating"],
                    ascending=[False, False, False],
                    na_position="last",
                )

            df["객실유형_인원"] = df.apply(
                lambda r: (
                    f"{r['room_category']} · 최대 {_safe_int(r.get('max_capacity'))}인 ({_safe_int(r.get('matched_room_count'))}객실)"
                    if _safe_int(r.get("matched_room_count")) > 0 and _safe_int(r.get("max_capacity")) > 0
                    else (
                        f"{r['room_category']} ({_safe_int(r.get('matched_room_count'))}객실)"
                        if _safe_int(r.get("matched_room_count")) > 0
                        else "미연결"
                    )
                ),
                axis=1,
            )
            df["예약률_30일"] = df.apply(
                lambda r: _format_occupancy_badge(r.get("occ_14d_pct"), r.get("occ_30d_pct"))
                if _safe_int(r.get("matched_room_count")) > 0
                else "-",
                axis=1,
            )
            df["추가요금_표시"] = df["extra_guest_fee_note"].apply(lambda v: _safe_str(v, "-"))
            df["숙소명_표시"] = df["matched_airbnb_title"].apply(lambda v: _clean_text(v, "-"))
            df["주요_고객층"] = df["guest_segments_json"].apply(lambda x: _format_top_segments(x, 2))
            df["면적_인허가일"] = df.apply(
                lambda r: f"{float(r['site_area']):.1f}㎡ · {_safe_str(r.get('license_date'), '-')}"
                if pd.notna(r.get("site_area"))
                else _safe_str(r.get("license_date"), "-"),
                axis=1,
            )

            view_df = df[
                [
                    "industry_type",
                    "business_name",
                    "숙소명_표시",
                    "객실유형_인원",
                    "예약률_30일",
                    "matched_rating",
                    "matched_reviews_count",
                    "weekday_total",
                    "weekend_total",
                    "추가요금_표시",
                    "주요_고객층",
                    "road_address",
                    "면적_인허가일",
                    "matched_url",
                    "matched_thumbnail_url",
                ]
            ].copy()
            view_df.columns = [
                "업종 구분",
                "공공 상호명",
                "에어비앤비 숙소명",
                "객실유형 · 인원",
                "예약률 (14일 / 30일)",
                "평점",
                "리뷰수",
                "평일 기본 객단가",
                "주말 기본 객단가",
                "기준·추가인원 요금",
                "주요 고객층",
                "도로명주소",
                "시설면적 · 인허가일",
                "에어비앤비",
                "_thumb_url",
            ]
            match_pct = (matched_cnt / total_active_cnt * 100.0) if total_active_cnt > 0 else 0.0
            if scope_filter == "에어비앤비 연결 완료만":
                st.caption(
                    f"조회 결과: 에어비앤비 실영업 연결 {len(view_df):,}개 사업장 "
                    f"(영업 인허가 {total_active_cnt:,}곳 중 {match_pct:.1f}% 연결 · 미연결 {len(unmatched_all_df):,}곳은 하단 접이식 목록에서 확인)"
                )
            else:
                st.caption(
                    f"조회 결과: {len(view_df):,}개 실영업 사업장 · 우측 [숙소 열기 ↗]를 클릭하면 해당 에어비앤비 숙소 페이지로 바로 이동합니다."
                )
            _render_native_table(view_df, key_prefix=f"{key_prefix}_biz", compact_mobile=compact_mobile)
        else:
            st.info("조건에 해당하는 실영업 사업장이 없습니다.")

        if scope_filter == "에어비앤비 연결 완료만" and not unmatched_all_df.empty:
            unmatched_view_src = unmatched_all_df.copy()
            if search_kw:
                u_mask = (
                    unmatched_view_src["business_name"].fillna("").str.contains(search_kw, case=False, regex=False)
                    | unmatched_view_src["road_address"].fillna("").str.contains(search_kw, case=False, regex=False)
                    | unmatched_view_src["jibun_address"].fillna("").str.contains(search_kw, case=False, regex=False)
                )
                unmatched_view_src = unmatched_view_src[u_mask]
            st.markdown(
                """
                <div class="unlinked-hover-wrap">
                    <span>ℹ️ 왜 '미연결 영업 업소'로 분류되나요? (마우스 커서를 올리면 상세 사유가 표시됩니다)</span>
                    <div class="unlinked-hover-box">
                        <b style="color:#93C5FD; font-size:0.81rem;">💡 에어비앤비 미연결 영업 업소 분류 사유</b><br>
                        행정안전부 LOCALDATA 인허가 대장상 <b>'영업/정상'</b> 상태이나 아래 사유로 에어비앤비와 연결되지 않은 숙소입니다.<br><br>
                        <b>① 타 플랫폼 전용 · 자체 운영:</b> 에어비앤비에 입점하지 않고 야놀자·여기어때·부킹닷컴·아고다·네이버예약·자체 홈페이지 등으로만 영업 중인 숙소<br>
                        <b>② 달력 닫힘 · 장기 만실:</b> 에어비앤비 예약 달력을 닫아두었거나(호스팅 일시중지) 장기 투숙·최소 숙박일수 설정으로 노출이 제외된 숙소<br>
                        <b>③ 상호명 상이 · 인허가번호 비공개:</b> 공공 인허가 대장의 상호명과 에어비앤비 노출 숙소명이 완전히 다르고 인허가번호도 표기되지 않은 숙소
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            with st.expander(
                f"에어비앤비 미연결 영업 업소 {len(unmatched_view_src):,}곳 보기",
                expanded=False,
            ):
                if unmatched_view_src.empty:
                    st.info("검색어와 일치하는 미연결 영업 업소가 없습니다.")
                else:
                    unmatched_view_src["면적_인허가일"] = unmatched_view_src.apply(
                        lambda r: f"{float(r['site_area']):.1f}㎡ · {_safe_str(r.get('license_date'), '-')}"
                        if pd.notna(r.get("site_area"))
                        else _safe_str(r.get("license_date"), "-"),
                        axis=1,
                    )
                    unmatched_view_src["미연결_사유"] = "타 플랫폼 전용 · 달력 닫힘 · 노출 상호명 상이"
                    u_df = unmatched_view_src[
                        ["industry_type", "business_name", "road_address", "면적_인허가일", "미연결_사유"]
                    ].sort_values(by=["industry_type", "business_name"])
                    u_df.columns = ["업종 구분", "공공 상호명", "도로명주소", "시설면적 · 인허가일", "미연결 분류 사유"]
                    st.dataframe(
                        u_df,
                        width="stretch",
                        hide_index=True,
                        row_height=36,
                        placeholder="-",
                        key=f"{key_prefix}_unmatched_df",
                    )
    else:
        df = comp_df.copy()
        if not df.empty:
            if scope_filter == "에어비앤비 연결 완료만":
                df = df[df["public_business_name"].notna()]
            elif scope_filter == "미연결 영업 업소만":
                df = df[df["public_business_name"].isna()]
        if room_cat_filter != "전체" and not df.empty:
            df = df[df["room_category"] == room_cat_filter]
        if search_kw and not df.empty:
            mask = (
                df["title"].fillna("").str.contains(search_kw, case=False, regex=False)
                | df["public_business_name"].fillna("").str.contains(search_kw, case=False, regex=False)
                | df["road_address"].fillna("").str.contains(search_kw, case=False, regex=False)
            )
            df = df[mask]

        if not df.empty:
            if sort_by == "30일 예약률 높은 순":
                df = df.sort_values(by=["occ_30d_pct", "reviews_count"], ascending=[False, False], na_position="last")
            elif sort_by == "14일 예약률 높은 순":
                df = df.sort_values(by=["occ_14d_pct", "reviews_count"], ascending=[False, False], na_position="last")
            elif sort_by == "평일 기본가 높은 순":
                df = df.sort_values(by=["weekday_total"], ascending=False, na_position="last")
            elif sort_by == "평일 기본가 낮은 순":
                df = df.sort_values(by=["weekday_total"], ascending=True, na_position="last")
            elif sort_by == "주말 기본가 높은 순":
                df = df.sort_values(by=["weekend_total"], ascending=False, na_position="last")
            elif sort_by == "주말 기본가 낮은 순":
                df = df.sort_values(by=["weekend_total"], ascending=True, na_position="last")
            elif sort_by == "평점 높은 순":
                df = df.sort_values(by=["rating", "reviews_count"], ascending=[False, False], na_position="last")
            elif sort_by == "상호명 가나다순":
                df = df.sort_values(by=["public_business_name", "title"], ascending=[True, True], na_position="last")
            else:
                df = df.sort_values(by=["reviews_count", "rating"], ascending=[False, False], na_position="last")

            df["공공상호_표시"] = df["public_business_name"].apply(lambda v: _clean_text(v, "-"))
            df["숙소명_표시"] = df["title"].apply(lambda v: _clean_text(v, "-"))
            df["객실유형_인원"] = df.apply(
                lambda r: f"{r['room_category']} · 최대 {_safe_int(r.get('capacity'))}인"
                if _safe_int(r.get("capacity")) > 0
                else str(r["room_category"]),
                axis=1,
            )
            df["예약률_30일"] = df.apply(
                lambda r: _format_occupancy_badge(r.get("occ_14d_pct"), r.get("occ_30d_pct")),
                axis=1,
            )
            df["추가요금_표시"] = df["extra_guest_fee_note"].apply(lambda v: _safe_str(v, "-"))
            df["도로명_표시"] = df["road_address"].apply(lambda v: _safe_str(v, "-"))
            df["주요_고객층"] = df["guest_segments_json"].apply(lambda x: _format_top_segments(x, 2))
            df["면적_인허가일"] = df.apply(
                lambda r: f"{float(r['site_area']):.1f}㎡ · {_safe_str(r.get('license_date'), '-')}"
                if pd.notna(r.get("site_area"))
                else _safe_str(r.get("license_date"), "-"),
                axis=1,
            )

            view_df = df[
                [
                    "industry_type",
                    "공공상호_표시",
                    "숙소명_표시",
                    "객실유형_인원",
                    "예약률_30일",
                    "rating",
                    "reviews_count",
                    "weekday_total",
                    "weekend_total",
                    "추가요금_표시",
                    "주요_고객층",
                    "도로명_표시",
                    "면적_인허가일",
                    "url",
                    "thumbnail_url",
                ]
            ].copy()
            view_df.columns = [
                "업종 구분",
                "공공 상호명",
                "에어비앤비 숙소명",
                "객실유형 · 인원",
                "예약률 (14일 / 30일)",
                "평점",
                "리뷰수",
                "평일 기본 객단가",
                "주말 기본 객단가",
                "기준·추가인원 요금",
                "주요 고객층",
                "도로명주소",
                "시설면적 · 인허가일",
                "에어비앤비",
                "_thumb_url",
            ]
            st.caption(
                f"조회 결과: {len(view_df):,}개 에어비앤비 객실 · 우측 [숙소 열기 ↗]를 클릭하면 해당 에어비앤비 숙소 페이지로 바로 이동합니다."
            )
            _render_native_table(view_df, key_prefix=f"{key_prefix}_room", compact_mobile=compact_mobile)
        else:
            st.info("선택한 필터 조건에 해당하는 객실이 없습니다.")


def _render_overview_tab(
    selected_zone: str,
    overview: Dict[str, Any],
    comp_df: pd.DataFrame,
    pub_df: pd.DataFrame,
) -> None:
    is_mobile_view = bool(st.session_state.get("is_mobile_view", False))
    with st.container():
        st.markdown('<div class="m-two-col" style="display:none;"></div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            st.metric(
                "호스텔업 (영업중)",
                f"{overview['hostel_active']:,}곳",
                f"에어비앤비 연결 {overview.get('hostel_matched', 0):,}곳",
                delta_color="off",
            )
        with c2:
            st.metric(
                "외국인관광도시민박업 (영업중)",
                f"{overview['minbak_active']:,}곳",
                f"에어비앤비 연결 {overview.get('minbak_matched', 0):,}곳",
                delta_color="off",
            )

    if is_mobile_view:
        st.markdown('<div class="section-title">에어비앤비 실영업 분포 지도</div>', unsafe_allow_html=True)
        with st.container():
            st.markdown('<div class="m-two-col" style="display:none;"></div>', unsafe_allow_html=True)
            mc1, mc2 = st.columns(2)
            with mc1:
                show_mobile_map = st.checkbox("🗺️ 지도 표시", value=True, key=f"m_show_map_{selected_zone}")
            with mc2:
                allow_mobile_drag = st.checkbox(
                    "👆 지도 드래그 이동",
                    value=False,
                    key=f"m_drag_map_{selected_zone}",
                    help="체크 해제 시 지도를 터치해 위아래로 넘겨도 화면 스크롤이 막히지 않고 부드럽게 내려갑니다.",
                )
        show_unmatched_map = False
    else:
        show_mobile_map = True
        allow_mobile_drag = True
        m_col1, m_col2 = st.columns([2.5, 1.2])
        with m_col1:
            st.markdown('<div class="section-title">에어비앤비 실영업 분포 지도</div>', unsafe_allow_html=True)
            st.caption(
                "파란핀: 호스텔업 | 주황핀: 외국인관광도시민박업 | 회색핀: 미연결 영업업소 · 동일 건물 다중 입점 숙소는 원형 분산 표시 · "
                "출처: 행정안전부 LOCALDATA 인허가 데이터 & Airbnb 실영업 매물·30일 달력"
            )
        with m_col2:
            st.write("")
            show_unmatched_map = st.checkbox(
                "미연결 영업 업소도 지도에 함께 표시 (회색 핀)",
                value=False,
                key=f"show_unmatched_map_{selected_zone}",
                help=_UNLINKED_REASON_TOOLTIP,
            )

    if show_mobile_map:
        if not pub_df.empty:
            if show_unmatched_map:
                active_pub = pub_df[pub_df["status_name"] == "영업/정상"].copy()
            else:
                active_pub = pub_df[(pub_df["status_name"] == "영업/정상") & (pub_df["matched_room_count"] > 0)].copy()
        else:
            active_pub = pub_df.copy()

        # 동일 건물(동일 위·경도) 다중 입점 숙소가 한 점에 겹쳐 하위 핀(예: 광안린 1층 / 더휴광안 2층)이 가려지지 않도록 원형 미세 오프셋(~7.5m) 적용
        building_groups: Dict[Tuple[float, float], List[int]] = defaultdict(list)
        for row_idx, pub in active_pub.iterrows():
            if pd.isna(pub.get("lat")) or pd.isna(pub.get("lon")):
                continue
            b_key = (round(float(pub["lat"]), 5), round(float(pub["lon"]), 5))
            building_groups[b_key].append(row_idx)

        pin_layout: Dict[int, Tuple[float, float, int, int, float, float]] = {}
        multi_building_centers: List[Tuple[float, float, int, str]] = []
        for (base_lat, base_lon), r_indices in building_groups.items():
            n = len(r_indices)
            if n == 1:
                only_idx = r_indices[0]
                r_row = active_pub.loc[only_idx]
                pin_layout[only_idx] = (float(r_row["lat"]), float(r_row["lon"]), 1, 1, base_lat, base_lon)
            else:
                sample_addr = _safe_str(active_pub.loc[r_indices[0]].get("road_address"), "-")
                multi_building_centers.append((base_lat, base_lon, n, sample_addr))
                for pos, r_idx in enumerate(r_indices):
                    if n <= 8:
                        ring_n = n
                        ring_pos = pos
                        r_lat, r_lon = 0.000068, 0.000084
                    elif pos < 8:
                        ring_n = 8
                        ring_pos = pos
                        r_lat, r_lon = 0.000068, 0.000084
                    elif pos < 20:
                        ring_n = min(12, n - 8)
                        ring_pos = pos - 8
                        r_lat, r_lon = 0.000130, 0.000160
                    else:
                        ring_n = max(1, n - 20)
                        ring_pos = pos - 20
                        r_lat, r_lon = 0.000192, 0.000236
                    angle = (2.0 * math.pi * ring_pos) / ring_n
                    disp_lat = base_lat + r_lat * math.sin(angle)
                    disp_lon = base_lon + r_lon * math.cos(angle)
                    pin_layout[r_idx] = (disp_lat, disp_lon, pos + 1, n, base_lat, base_lon)

        # 하단 테이블 검색어(t1_kw)가 입력된 경우 일치하는 숙소를 지도 중앙으로 자동 포커스 및 강조 표시
        active_search_kw = str(st.session_state.get("t1_kw", "") or "").strip()
        search_matched_indices: set[int] = set()
        search_matched_coords: List[Tuple[float, float]] = []
        if active_search_kw and not active_pub.empty:
            kw_low = active_search_kw.lower()
            for r_idx, pub in active_pub.iterrows():
                if r_idx not in pin_layout:
                    continue
                hay = " ".join(
                    [
                        str(pub.get("business_name") or ""),
                        str(pub.get("matched_airbnb_title") or ""),
                        str(pub.get("road_address") or ""),
                        str(pub.get("jibun_address") or ""),
                    ]
                ).lower()
                if kw_low in hay:
                    search_matched_indices.add(r_idx)
                    d_lat, d_lon, _, _, _, _ = pin_layout[r_idx]
                    search_matched_coords.append((d_lat, d_lon))

        zone_cfg = ZONES.get(selected_zone, ZONES["seomyeon"])
        if search_matched_coords:
            map_center_lat = sum(c[0] for c in search_matched_coords) / len(search_matched_coords)
            map_center_lon = sum(c[1] for c in search_matched_coords) / len(search_matched_coords)
            map_zoom = 17 if len(search_matched_coords) <= 3 else 15
            st.caption(
                f"🔍 하단 검색어 **'{active_search_kw}'** 일치 숙소 **{len(search_matched_coords)}곳**을 지도 중앙(빨간 강조 핀)으로 포커스했습니다."
            )
        else:
            map_center_lat = zone_cfg.center_lat
            map_center_lon = zone_cfg.center_lon
            map_zoom = 15 if selected_zone != "all" else 12

        m = folium.Map(
            location=[map_center_lat, map_center_lon],
            zoom_start=map_zoom,
            dragging=allow_mobile_drag,
            scrollWheelZoom=allow_mobile_drag,
            touchZoom=allow_mobile_drag,
        )
        m.get_root().header.add_child(
            folium.Element(
                """
                <style>
                .leaflet-tooltip {
                    opacity: 1 !important;
                    background: transparent !important;
                    border: none !important;
                    box-shadow: none !important;
                    padding: 0 !important;
                    pointer-events: none !important;
                }
                .leaflet-tooltip * {
                    pointer-events: none !important;
                }
                .leaflet-tooltip-left:before,
                .leaflet-tooltip-right:before,
                .leaflet-tooltip-top:before,
                .leaflet-tooltip-bottom:before {
                    display: none !important;
                }
                </style>
                <script>
                document.addEventListener("DOMContentLoaded", function() {
                    var observer = new MutationObserver(function() {
                        var pane = document.querySelector(".leaflet-tooltip-pane");
                        if (pane && pane.children.length > 1) {
                            while (pane.children.length > 1) {
                                pane.removeChild(pane.firstElementChild);
                            }
                        }
                    });
                    observer.observe(document.body, { childList: true, subtree: true });
                    document.addEventListener("mouseleave", function() {
                        var pane = document.querySelector(".leaflet-tooltip-pane");
                        if (pane) { pane.innerHTML = ""; }
                    });
                });
                </script>
                """
            )
        )

        if selected_zone in ZONES:
            folium.Circle(
                location=[zone_cfg.center_lat, zone_cfg.center_lon],
                radius=zone_cfg.radius_km * 1000,
                color="#60A5FA",
                weight=1.5,
                fill=True,
                fill_opacity=0.04,
                tooltip=f"{zone_cfg.name} 반경 {zone_cfg.radius_km}km",
            ).add_to(m)

        for b_lat, b_lon, b_cnt, b_addr in multi_building_centers:
            folium.CircleMarker(
                location=[b_lat, b_lon],
                radius=4,
                color="#3B82F6",
                weight=1.5,
                fill=True,
                fill_color="#93C5FD",
                fill_opacity=0.45,
                tooltip=f"동일 건물 다중 입점 ({b_cnt}곳) · {b_addr}",
            ).add_to(m)

        for r_idx, pub in active_pub.iterrows():
            if r_idx not in pin_layout:
                continue
            disp_lat, disp_lon, sb_idx, sb_total, _, _ = pin_layout[r_idx]
            has_room = _safe_int(pub.get("matched_room_count")) > 0
            is_search_hit = r_idx in search_matched_indices
            if is_search_hit:
                color = "red"
                folium.Circle(
                    location=[disp_lat, disp_lon],
                    radius=24,
                    color="#EF4444",
                    weight=2,
                    fill=True,
                    fill_opacity=0.16,
                ).add_to(m)
            else:
                color = _industry_color(pub["industry_type"], pub["status_name"]) if has_room else "gray"

            tooltip_html = _build_airroi_map_card_html(
                pub,
                include_link=False,
                same_building_idx=sb_idx,
                same_building_total=sb_total,
            )
            popup_html = _build_airroi_map_card_html(
                pub,
                include_link=True,
                same_building_idx=sb_idx,
                same_building_total=sb_total,
            )

            folium.Marker(
                location=[disp_lat, disp_lon],
                popup=folium.Popup(popup_html, max_width=290),
                tooltip=folium.Tooltip(
                    tooltip_html,
                    sticky=False,
                    direction="top",
                    offset=(0, -14),
                    opacity=1.0,
                    style="background-color: transparent; border: none; box-shadow: none; padding: 0; pointer-events: none;",
                ),
                icon=folium.Icon(
                    color=color,
                    icon="star" if is_search_hit else ("bed" if has_room else "building"),
                    prefix="fa",
                ),
            ).add_to(m)

        st_folium(
            m,
            width=None,
            height=290 if is_mobile_view else 500,
            returned_objects=[],
            key=f"folium_map_{selected_zone}_{active_search_kw}_{allow_mobile_drag}",
        )
    st.divider()
    _render_unified_table(pub_df, comp_df, key_prefix="t1")


def _render_competitors_tab(comp_df: pd.DataFrame, pub_df: pd.DataFrame, is_admin: bool = False) -> None:
    _render_unified_table(pub_df, comp_df, key_prefix="t2")

    if is_admin and not pub_df.empty:
        st.divider()
        st.markdown('<div class="section-title">누락 숙소 에어비앤비 URL 직접 등록 및 공공상호 고정 매칭</div>', unsafe_allow_html=True)
        st.caption(
            "검색일 만실 또는 최소 숙박일수 설정으로 자동 검색에서 제외된 숙소는 URL을 등록하면 즉시 해당 공공상호와 고정 매칭되고, "
            "30일 달력 예약률과 평일·주말 호스트 기본가가 수집됩니다."
        )
        pub_options = {
            f"[{r['industry_type']} · {ZONES.get(r['zone_key'], ZONES['seomyeon']).name}] {r['business_name']} ({_safe_str(r.get('road_address'), '-')})": int(r["id"])
            for _, r in pub_df.iterrows()
        }
        with st.form("register_url_form"):
            u_col1, u_col2, u_col3 = st.columns([2.2, 2.2, 1.0])
            with u_col1:
                target_pub_label = st.selectbox("연결할 공공 인허가 상호 선택", options=list(pub_options.keys()))
            with u_col2:
                input_air_url = st.text_input(
                    "에어비앤비 숙소 URL",
                    placeholder="https://www.airbnb.co.kr/rooms/12345678",
                )
            with u_col3:
                input_memo = st.text_input("비고 (선택)", placeholder="내 숙소 / 주요 경쟁사")

            if st.form_submit_button("URL 등록 및 즉시 수집·매칭", type="primary"):
                try:
                    pid = pub_options[target_pub_label]
                    lid = register_tracked_airbnb_url(
                        airbnb_url=input_air_url,
                        public_id=pid,
                        memo=input_memo,
                    )
                    st.success(f"숙소 ID ({lid}) 등록, 공공상호 매칭 및 30일 달력·기본가 수집이 완료되었습니다.")
                    st.cache_data.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        tracked_df = get_tracked_urls_df()
        if not tracked_df.empty:
            st.markdown("**등록된 고정 추적 URL 목록**")
            st.dataframe(tracked_df, width="stretch", hide_index=True)

        if not comp_df.empty:
            st.divider()
            st.markdown('<div class="section-title">수집 객실 ↔ 공공 인허가 업소 수동 매칭 변경</div>', unsafe_allow_html=True)
            col1, col2, col3 = st.columns([2, 2, 1])
            listing_map = {f"{_clean_text(r['title'])} ({r['listing_id']})": r["listing_id"] for _, r in comp_df.iterrows()}
            with col1:
                sel_listing_label = st.selectbox("에어비앤비 객실 선택", list(listing_map.keys()))
            with col2:
                sel_pub_label = st.selectbox("공공 인허가 업소 선택", list(pub_options.keys()), key="manual_pub_sel")
            with col3:
                st.write("")
                st.write("")
                b1, b2 = st.columns(2)
                if b1.button("매칭 확정", width="stretch"):
                    set_manual_match(pub_options[sel_pub_label], listing_map[sel_listing_label])
                    st.success("수동 매칭을 저장했습니다.")
                    st.cache_data.clear()
                    st.rerun()
                if b2.button("매칭 해제", width="stretch"):
                    clear_match(listing_map[sel_listing_label])
                    st.warning("매칭을 해제했습니다.")
                    st.cache_data.clear()
                    st.rerun()


@st.cache_data(ttl=300, show_spinner=False)
def _get_prepared_comparison_pool(selected_zone: str, ind_choice: str, _v: int = 5) -> pd.DataFrame:
    """비교 탭 전용 숙소 풀과 짧고 깔끔한 검색 라벨을 캐싱하여 0.01초 내에 반환한다."""
    full_df = _cached_competitor_df("all").copy()
    if full_df.empty:
        return full_df

    if ind_choice in (INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK):
        full_df = full_df[full_df["industry_type"] == ind_choice].copy()
    if full_df.empty:
        return full_df

    full_df["_in_current_zone"] = (full_df["zone_key"] == selected_zone).astype(int) if selected_zone != "all" else 1
    full_df["_has_biz"] = full_df["public_business_name"].notna().astype(int)
    full_df = full_df.sort_values(
        by=["_in_current_zone", "_has_biz", "reviews_count", "weekday_total"],
        ascending=[False, False, False, False],
        na_position="last",
    )

    biz_counts = full_df["public_business_name"].value_counts().to_dict()
    labels: List[str] = []
    seen_labels: set[str] = set()

    for _, r in full_df.iterrows():
        biz = _clean_text(r.get("public_business_name"), "")
        title = _clean_text(r.get("title"), "")
        z_key = str(r.get("zone_key") or "")
        z_name = ZONES[z_key].name.split()[0] if z_key in ZONES else "부산"
        cap = _safe_int(r.get("capacity"), 0)
        cap_str = f"{cap}인" if cap > 0 else "인원미상"
        lid_tail = str(r.get("listing_id") or "")[-4:]

        if biz and biz != "-":
            if biz_counts.get(biz, 0) > 1:
                base_lbl = f"{biz} · {title[:12]} ({z_name}·{cap_str})"
            else:
                base_lbl = f"{biz} ({z_name}·{cap_str})"
        else:
            base_lbl = f"{title[:18]} ({z_name}·{cap_str})"

        lbl = base_lbl if base_lbl not in seen_labels else f"{base_lbl} #{lid_tail}"
        seen_labels.add(lbl)
        labels.append(lbl)

    full_df["select_label"] = labels
    return full_df


def _render_property_search_selector(
    tab_prefix: str,
    selected_zone: str,
    default_count: int = 0,
    max_count: int = 6,
) -> pd.DataFrame:
    """한 줄로 깔끔하게 정돈된 초고속 숙소 검색·선택 바 (초기값 비움)."""
    c_ind, c_sel, c_btn = st.columns([1.05, 2.9, 0.45])
    with c_ind:
        ind_choice = st.radio(
            "업종 구분",
            options=["전체", INDUSTRY_URBAN_MINBAK, INDUSTRY_HOSTEL],
            format_func=lambda x: "외도민" if x == INDUSTRY_URBAN_MINBAK else ("호스텔" if x == INDUSTRY_HOSTEL else "전체"),
            horizontal=True,
            key=f"{tab_prefix}_ind_filter",
        )

    full_df = _get_prepared_comparison_pool(selected_zone, ind_choice)
    if full_df.empty:
        return pd.DataFrame()

    label_to_row = {r["select_label"]: r for _, r in full_df.iterrows()}
    all_labels = list(label_to_row.keys())

    state_key = f"{tab_prefix}_selected_labels"
    if state_key not in st.session_state:
        st.session_state[state_key] = []
    else:
        st.session_state[state_key] = [lbl for lbl in st.session_state[state_key] if lbl in label_to_row]

    def _clear_selection() -> None:
        st.session_state[state_key] = []

    with c_btn:
        st.write("")
        st.button("비우기", key=f"{tab_prefix}_clear_btn", on_click=_clear_selection, width="stretch")

    with c_sel:
        selected_labels = st.multiselect(
            f"비교할 숙소 검색 · 선택 (클릭 후 상호명 입력 · 최대 {max_count}곳)",
            options=all_labels,
            max_selections=max_count,
            placeholder="비교할 숙소 이름을 입력하세요 (예: 무해, 후추스테이, 광안린, 연온담, 여온)",
            key=state_key,
        )

    if not selected_labels:
        return pd.DataFrame()

    return pd.DataFrame([label_to_row[lbl] for lbl in selected_labels if lbl in label_to_row]).copy()


def _build_unique_chart_labels(sel_df: pd.DataFrame) -> List[str]:
    """동일 상호의 여러 객실을 동시에 선택해도 차트 X축 라벨이 겹쳐 합산되지 않도록 고유 라벨을 생성한다."""
    base_list: List[str] = []
    for _, r in sel_df.iterrows():
        biz = _clean_text(r.get("public_business_name"), "")
        title = _clean_text(r.get("title"), "")
        cap = _safe_int(r.get("capacity"), 0)
        nm = biz if (biz and biz != "-") else title[:14]
        base_list.append(f"{nm} ({cap}인)" if cap > 0 else nm)

    counts: Dict[str, int] = defaultdict(int)
    for b in base_list:
        counts[b] += 1

    unique_labels: List[str] = []
    seen: set[str] = set()
    for idx, (_, r) in enumerate(sel_df.iterrows()):
        b = base_list[idx]
        if counts[b] > 1:
            biz = _clean_text(r.get("public_business_name"), "")
            title = _clean_text(r.get("title"), "")
            cap = _safe_int(r.get("capacity"), 0)
            nm = biz if (biz and biz != "-") else title[:10]
            cand = f"{nm}·{title[:8]} ({cap}인)" if cap > 0 else f"{nm}·{title[:8]}"
        else:
            cand = b
        if cand in seen:
            cand = f"{cand} #{idx + 1}"
        seen.add(cand)
        unique_labels.append(cand)
    return unique_labels


@st.fragment
def _render_price_tab(selected_zone: str, comp_df: pd.DataFrame) -> None:
    is_mobile_view = bool(st.session_state.get("is_mobile_view", False))
    st.markdown('<div class="section-title">맞춤 숙소 객단가(ADR) 및 예약률 1:1 비교</div>', unsafe_allow_html=True)
    st.caption(
        "비교할 숙소 이름을 검색해 2~6곳을 선택하면 평일·주말 호스트 기본 객단가와 14일/30일 달력 예약률을 즉시 비교합니다."
    )

    sel_df = _render_property_search_selector(
        tab_prefix="adr",
        selected_zone=selected_zone,
        default_count=0,
        max_count=6,
    )
    if sel_df.empty:
        st.info("위 선택창을 클릭해 비교할 숙소 이름(예: 무해, 후추스테이, 광안린, 연온담)을 입력해 선택해 주세요.")
        return

    sel_df["short_name"] = _build_unique_chart_labels(sel_df)

    # 1) 선택 숙소 핵심 스펙 & 객단가 요약 비교 표 (공공 상호명을 첫 컬럼으로 배치하고 겹침 방지를 위해 pinned 해제)
    summary_rows = []
    for _, r in sel_df.iterrows():
        wd_v = r.get("weekday_total")
        we_v = r.get("weekend_total")
        premium_pct = (
            f"+{((float(we_v) - float(wd_v)) / float(wd_v) * 100.0):.0f}%"
            if pd.notna(wd_v) and pd.notna(we_v) and float(wd_v) > 0
            else "-"
        )
        cap_i = max(1, _safe_int(r.get("capacity"), 1))
        per_person_wd = round(float(wd_v) / cap_i) if pd.notna(wd_v) and float(wd_v) > 0 else None
        summary_rows.append(
            {
                "공공 상호명": _clean_text(r.get("public_business_name"), "-"),
                "업종 구분": _clean_text(r.get("industry_type"), "-"),
                "에어비앤비 숙소명": _clean_text(r.get("title"), "-"),
                "객실형태 · 인원": f"{r.get('room_category')} · 최대 {_safe_int(r.get('capacity'))}인",
                "시설면적": f"{float(r['site_area']):.1f}㎡" if pd.notna(r.get("site_area")) else "-",
                "평일 기본 객단가": round(float(wd_v)) if pd.notna(wd_v) and float(wd_v) > 0 else None,
                "주말 기본 객단가": round(float(we_v)) if pd.notna(we_v) and float(we_v) > 0 else None,
                "주말 할증률": premium_pct,
                "1인당 평일가": per_person_wd,
                "예약률 (14일 / 30일)": _format_occupancy_badge(r.get("occ_14d_pct"), r.get("occ_30d_pct")),
                "평점 · 리뷰": f"★{float(r['rating']):.2f} ({_safe_int(r.get('reviews_count')):,}개)"
                if pd.notna(r.get("rating"))
                else "-",
                "도로명주소": _clean_text(r.get("road_address"), "-"),
                "에어비앤비": _safe_str(r.get("url"), ""),
            }
        )
    st.markdown(f"**선택한 {len(sel_df)}개 숙소 핵심 스펙 · 기본 객단가 비교표**")
    sum_df = pd.DataFrame(summary_rows)
    if is_mobile_view:
        sum_df = sum_df[["공공 상호명", "평일 기본 객단가", "주말 기본 객단가", "예약률 (14일 / 30일)", "에어비앤비"]].copy()
    st.dataframe(
        sum_df,
        column_config={
            "공공 상호명": st.column_config.TextColumn("공공 상호명", width=165),
            "업종 구분": st.column_config.TextColumn("업종 구분", width=135),
            "에어비앤비 숙소명": st.column_config.TextColumn("에어비앤비 숙소명", width=260),
            "객실형태 · 인원": st.column_config.TextColumn("객실형태 · 인원", width=150),
            "시설면적": st.column_config.TextColumn("시설면적", width=90),
            "평일 기본 객단가": st.column_config.NumberColumn("평일 기본 객단가", format="%,d원", width=130),
            "주말 기본 객단가": st.column_config.NumberColumn("주말 기본 객단가", format="%,d원", width=130),
            "주말 할증률": st.column_config.TextColumn("주말 할증률", width=90),
            "1인당 평일가": st.column_config.NumberColumn("1인당 평일가", format="%,d원", width=115),
            "예약률 (14일 / 30일)": st.column_config.TextColumn("예약률 (14일 / 30일)", width=150),
            "평점 · 리뷰": st.column_config.TextColumn("평점 · 리뷰", width=120),
            "도로명주소": st.column_config.TextColumn("도로명주소", width=210),
            "에어비앤비": st.column_config.LinkColumn("에어비앤비", display_text="숙소 열기 ↗", width=110),
        },
        width="stretch",
        hide_index=True,
        row_height=38,
        placeholder="-",
    )

    # 2) 선택 숙소 평일 vs 주말 기본 객단가 차트 & 30일 예약률 비교 차트
    if is_mobile_view:
        ch_col1 = st.container()
        ch_col2 = st.container()
    else:
        ch_col1, ch_col2 = st.columns([1.35, 1.0])
    with ch_col1:
        melt_cols = ["weekday_total"]
        if sel_df["weekend_total"].notna().any():
            melt_cols.append("weekend_total")
        price_melt = sel_df.melt(
            id_vars=["short_name", "room_category"],
            value_vars=melt_cols,
            var_name="요일_구분",
            value_name="기본_객단가",
        ).dropna(subset=["기본_객단가"])
        price_melt["요일_구분"] = price_melt["요일_구분"].map(
            {"weekday_total": "평일 기본가", "weekend_total": "주말(금·토) 기본가"}
        )

        if not price_melt.empty:
            fig_bar = px.bar(
                price_melt,
                x="short_name",
                y="기본_객단가",
                color="요일_구분",
                barmode="group",
                text_auto=",.0f",
                color_discrete_map={"평일 기본가": "#5B9BD5", "주말(금·토) 기본가": "#8EA9DB"},
                height=390,
                title="선택 숙소 평일 vs 주말 기본 객단가 비교 (원)",
                labels={"short_name": "선택 숙소", "기본_객단가": "1박 기본 객단가(원)", "요일_구분": "구분"},
            )
            fig_bar.update_layout(
                yaxis_tickformat=",",
                plot_bgcolor="#F8FAFC",
                paper_bgcolor="#FFFFFF",
                font_color="#0F172A",
                legend_title_text="",
            )
            st.plotly_chart(fig_bar, width="stretch")

    with ch_col2:
        occ_melt = sel_df.melt(
            id_vars=["short_name"],
            value_vars=["occ_14d_pct", "occ_30d_pct"],
            var_name="기간",
            value_name="예약률",
        ).dropna(subset=["예약률"])
        occ_melt["기간"] = occ_melt["기간"].map({"occ_14d_pct": "향후 14일 예약률(%)", "occ_30d_pct": "향후 30일 예약률(%)"})
        if not occ_melt.empty:
            fig_occ = px.bar(
                occ_melt,
                x="short_name",
                y="예약률",
                color="기간",
                barmode="group",
                text_auto=".0f",
                color_discrete_map={"향후 14일 예약률(%)": "#63B3ED", "향후 30일 예약률(%)": "#68D391"},
                height=390,
                title="선택 숙소 향후 14일 · 30일 달력 예약률 비교 (%)",
                labels={"short_name": "선택 숙소", "예약률": "예약률(%)", "기간": "기간"},
            )
            fig_occ.update_layout(
                yaxis_range=[0, 105],
                plot_bgcolor="#F8FAFC",
                paper_bgcolor="#FFFFFF",
                font_color="#0F172A",
                legend_title_text="",
            )
            st.plotly_chart(fig_occ, width="stretch")


@st.fragment
def _render_customers_tab(selected_zone: str, comp_df: pd.DataFrame) -> None:
    is_mobile_view = bool(st.session_state.get("is_mobile_view", False))
    st.markdown('<div class="section-title">선택 숙소 고객층(여행 유형) 및 포지셔닝 나란히 비교 (2~4곳)</div>', unsafe_allow_html=True)
    st.caption(
        "비교할 숙소 이름을 검색해 2~4곳을 선택하면 여행자 유형 비중, 핵심 포지셔닝 태그, 강점·약점을 나란히 대조합니다."
    )

    sel_df = _render_property_search_selector(
        tab_prefix="cust",
        selected_zone=selected_zone,
        default_count=0,
        max_count=4,
    )
    if sel_df.empty:
        st.info("위 선택창을 클릭해 비교할 숙소 이름(예: 무해, 후추스테이, 광안린, 연온담)을 입력해 선택해 주세요.")
        return

    sel_df["unique_chart_label"] = _build_unique_chart_labels(sel_df)
    sel_rows = [r for _, r in sel_df.iterrows()]

    # 1) 선택된 2~4개 숙소의 고객층(여행 유형) 비중 비교 차트
    seg_records: List[Dict[str, Any]] = []
    for r in sel_rows:
        short_nm = str(r.get("unique_chart_label") or _clean_text(r.get("public_business_name"), "") or _clean_text(r.get("title"), "")[:16])
        segs = parse_json_col(r.get("guest_segments_json"), {})
        for seg_k, seg_v in segs.items():
            seg_records.append({"숙소명": short_nm, "여행자 유형": seg_k, "비중(%)": float(seg_v)})

    if seg_records:
        seg_df = pd.DataFrame(seg_records)
        fig_seg = px.bar(
            seg_df,
            x="숙소명",
            y="비중(%)",
            color="여행자 유형",
            barmode="group",
            text_auto=".0f",
            height=360,
            color_discrete_sequence=["#5B9BD5", "#63B3ED", "#90CDF4", "#68D391", "#F6AD55"],
            title=f"선택한 {len(sel_rows)}개 숙소의 주요 고객층(여행자 유형) 비중 비교 (%)",
        )
        fig_seg.update_layout(
            plot_bgcolor="#F8FAFC",
            paper_bgcolor="#FFFFFF",
            font_color="#0F172A",
            legend_title_text="여행자 유형",
        )
        st.plotly_chart(fig_seg, width="stretch")

    # 2) 선택된 2~4개 숙소 나란히 비교 카드 (모바일 모드에서는 1열 세로 배치, PC 모드에서는 2~4열 분할)
    st.markdown("#### 선택한 숙소별 포지셔닝 · 강점 · 약점 1:1 대조 카드")
    cols = [st.container() for _ in sel_rows] if is_mobile_view else st.columns(len(sel_rows))
    for col, r in zip(cols, sel_rows):
        with col:
            biz_nm = _clean_text(r.get("public_business_name"), "미상")
            air_title = _clean_text(r.get("title"), "-")
            ind_nm = _clean_text(r.get("industry_type"), "-")
            tags = parse_json_col(r.get("positioning_tags_json"), [])
            strengths = parse_json_col(r.get("strengths_json"), [])
            weaknesses = parse_json_col(r.get("weaknesses_json"), [])
            bm_note = _clean_text(r.get("benchmark_note"), "-")
            area_txt = f"{float(r['site_area']):.1f}㎡" if pd.notna(r.get("site_area")) else "면적미상"
            cap_txt = f"최대 {_safe_int(r.get('capacity'))}인" if _safe_int(r.get("capacity")) > 0 else "-"
            rating_txt = f"★{float(r['rating']):.2f}" if pd.notna(r.get("rating")) else "평점없음"

            tags_html = "".join(f'<span class="tag-pill">{html_lib.escape(str(t))}</span>' for t in tags) if tags else "-"
            top_seg_txt = _format_top_segments(r.get("guest_segments_json"), 3)
            str_txt = " · ".join(str(x) for x in strengths) if strengths else "-"
            weak_txt = " · ".join(str(x) for x in weaknesses) if weaknesses else "-"
            room_url = _safe_str(r.get("url"), "")
            link_html = (
                f'<div style="margin-top:8px;"><a href="{html_lib.escape(room_url)}" target="_blank" '
                f'style="color:#2563EB; text-decoration:none; font-weight:700;">에어비앤비 숙소 열기 ↗</a></div>'
                if room_url and room_url != "-"
                else ""
            )

            st.markdown(
                f"""
                <div class="compare-card">
                    <div class="compare-card-title">[{html_lib.escape(ind_nm)}] {html_lib.escape(biz_nm)}</div>
                    <div class="compare-card-sub">{html_lib.escape(air_title[:42])}</div>
                    <div style="font-size:0.82rem; color:#334155; line-height:1.65;">
                        <b>객실 · 면적:</b> {r.get('room_category')} ({cap_txt} · {area_txt})<br>
                        <b>평일 / 주말 기본가:</b> {_fmt_krw(r.get('weekday_total'))} / {_fmt_krw(r.get('weekend_total'))}<br>
                        <b>예약률(14일/30일):</b> {_format_occupancy_badge(r.get('occ_14d_pct'), r.get('occ_30d_pct'))}<br>
                        <b>평점 · 리뷰:</b> {rating_txt} ({_safe_int(r.get('reviews_count')):,}개)<br>
                        <hr style="border-color:#E2E8F0; margin:8px 0;">
                        <b>주요 고객층:</b> {html_lib.escape(top_seg_txt)}<br>
                        <b>포지셔닝 태그:</b><br>{tags_html}<br>
                        <div style="margin-top:6px;"><b>강점:</b> {html_lib.escape(str_txt)}</div>
                        <div style="margin-top:4px;"><b>약점·주의점:</b> {html_lib.escape(weak_txt)}</div>
                        <div style="margin-top:6px; color:#1D4ED8;"><b>벤치마킹:</b> {html_lib.escape(bm_note)}</div>
                        {link_html}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _render_inbound_trend_tab(selected_zone: str, overview: Dict[str, Any], is_admin: bool = False) -> None:
    """한국관광 데이터랩(DataLab) 기반 부산 5대 상권 방한 외래객 유입 추이 및 국적별 비중 분석 화면."""
    sigungu_info = SIGUNGU_DATALAB_MAP.get(selected_zone)
    sigungu_label = sigungu_info["name"] if sigungu_info else "부산진·수영·해운대·동·중구 합계"

    st.markdown(
        f'<div class="section-title">방한 외래관광객 유입 및 국적 트렌드 ({sigungu_label})</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "📌 데이터 출처: 문화체육관광부 · 한국관광공사 『한국관광 데이터랩 (datalab.visitkorea.or.kr › 외래객 지역별 방한현황)』 "
        "| SKT 이동통신 로밍 빅데이터 기반 기초지자체별 일자별 순방문자 집계 (기준기간: 2025.10 ~ 2026.09)"
    )

    monthly_df = get_tourism_monthly_df(selected_zone)
    all_zones_df = get_tourism_monthly_df("all")
    nat_df = get_tourism_nationality_df(selected_zone)

    foreign_df = (
        monthly_df[monthly_df["visitor_type"] == "외국인(방한외래객)"]
        .groupby("base_ym", as_index=False)["visitor_count"]
        .sum()
        .sort_values("base_ym")
    )

    latest_ym = "-"
    latest_cnt = 0
    total_12m_cnt = 0
    avg_12m_cnt = 0
    mom_delta_str = "-"

    if not foreign_df.empty:
        total_12m_cnt = int(round(float(foreign_df["visitor_count"].sum())))
        avg_12m_cnt = int(round(total_12m_cnt / len(foreign_df)))
        latest_row = foreign_df.iloc[-1]
        latest_ym = str(latest_row["base_ym"])
        latest_cnt = int(round(float(latest_row["visitor_count"])))
        if len(foreign_df) >= 2:
            prev_cnt = float(foreign_df.iloc[-2]["visitor_count"])
            if prev_cnt > 0:
                mom_pct = ((latest_cnt - prev_cnt) / prev_cnt) * 100.0
                sign = "+" if mom_pct >= 0 else ""
                mom_delta_str = f"전월({foreign_df.iloc[-2]['base_ym']}) 대비 {sign}{mom_pct:.1f}%"

    share_in_busan_str = f"월평균 {avg_12m_cnt:,}명 방문"
    if selected_zone != "all" and not all_zones_df.empty:
        busan_12m_total = float(
            all_zones_df[all_zones_df["visitor_type"] == "외국인(방한외래객)"]["visitor_count"].sum()
        )
        if busan_12m_total > 0:
            z_share = (total_12m_cnt / busan_12m_total) * 100.0
            share_in_busan_str = f"월평균 {avg_12m_cnt:,}명 · 5대 구군 중 {z_share:.1f}%"

    top1_title = "-"
    top1_sub = "-"
    top23_title = "-"
    top23_sub = "-"
    if not nat_df.empty:
        t1 = nat_df.iloc[0]
        t1_cnt = int(round(float(t1["visitor_count_12m"]))) if pd.notna(t1.get("visitor_count_12m")) else 0
        top1_title = f"1위 {t1['nationality']} ({float(t1['visitor_share_pct']):.1f}%)"
        top1_sub = f"1년 누적 {t1_cnt:,}명" if t1_cnt > 0 else "최다 방문 국적"
        if len(nat_df) >= 3:
            t2, t3 = nat_df.iloc[1], nat_df.iloc[2]
            top23_title = f"2위 {t2['nationality']} {float(t2['visitor_share_pct']):.1f}% · 3위 {t3['nationality']} {float(t3['visitor_share_pct']):.1f}%"
            if len(nat_df) >= 5:
                t4, t5 = nat_df.iloc[3], nat_df.iloc[4]
                top23_sub = f"4위 {t4['nationality']} {float(t4['visitor_share_pct']):.1f}% · 5위 {t5['nationality']} {float(t5['visitor_share_pct']):.1f}%"

    is_mobile_view = bool(st.session_state.get("is_mobile_view", False))
    if is_mobile_view:
        kr1_a, kr1_b = st.columns(2)
        kr2_a, kr2_b = st.columns(2)
        k1, k2, k3, k4 = kr1_a, kr1_b, kr2_a, kr2_b
    else:
        k1, k2, k3, k4 = st.columns(4)
    with k1:
        st.metric(
            f"최근 월 외국인 방문 ({latest_ym})",
            f"{latest_cnt:,}명",
            mom_delta_str,
            delta_color="off",
        )
    with k2:
        st.metric(
            "최근 1년 누적 외국인 방문",
            f"{total_12m_cnt:,}명",
            share_in_busan_str,
            delta_color="off",
        )
    with k3:
        st.metric(
            "최다 방문 국적 (1위)",
            top1_title,
            top1_sub,
            delta_color="off",
        )
    with k4:
        st.metric(
            "주요 방문 국적 (2~5위)",
            top23_title,
            top23_sub,
            delta_color="off",
        )

    if is_mobile_view:
        c_left = st.container()
        c_right = st.container()
    else:
        c_left, c_right = st.columns([1.45, 1.0])
    with c_left:
        if not foreign_df.empty:
            fig_trend = px.bar(
                foreign_df,
                x="base_ym",
                y="visitor_count",
                text_auto=",.0f",
                height=370,
                title=f"월별 방한 외국인 방문자수 추이 ({sigungu_label}, 단위: 명)",
                labels={"base_ym": "기준 연월", "visitor_count": "방문자수(명)"},
                color_discrete_sequence=["#5B9BD5"],
            )
            fig_trend.update_layout(
                xaxis_type="category",
                yaxis_tickformat=",",
                plot_bgcolor="#F8FAFC",
                paper_bgcolor="#FFFFFF",
                font_color="#0F172A",
            )
            st.plotly_chart(fig_trend, width="stretch")

    with c_right:
        if not nat_df.empty:
            fig_nat = px.bar(
                nat_df,
                x="visitor_share_pct",
                y="nationality",
                orientation="h",
                text_auto=".1f",
                height=370,
                title=f"방문 외국인 국적별 비중 ({sigungu_label}, 단위: %)",
                labels={"nationality": "국적", "visitor_share_pct": "방문 비중(%)"},
                color_discrete_sequence=["#63B3ED"],
            )
            fig_nat.update_layout(
                yaxis={"categoryorder": "total ascending"},
                plot_bgcolor="#F8FAFC",
                paper_bgcolor="#FFFFFF",
                font_color="#0F172A",
            )
            st.plotly_chart(fig_nat, width="stretch")

    if not all_zones_df.empty:
        f_all = all_zones_df[all_zones_df["visitor_type"] == "외국인(방한외래객)"].copy()
        if not f_all.empty:
            f_all["구군"] = f_all["zone_key"].map(
                lambda k: f"{SIGUNGU_DATALAB_MAP[k]['name']} ({ZONES[k].name.split()[0]})" if k in ZONES else k
            )
            fig_cmp = px.line(
                f_all,
                x="base_ym",
                y="visitor_count",
                color="구군",
                markers=True,
                height=350,
                title="부산 5대 구군별 월간 방한 외국인 방문자수 비교 (단위: 명)",
                labels={"base_ym": "기준 연월", "visitor_count": "방문자수(명)", "구군": "구군"},
                color_discrete_sequence=["#4A90E2", "#68D391", "#F6AD55", "#9F7AEA", "#F56565"],
            )
            fig_cmp.update_layout(
                xaxis_type="category",
                yaxis_tickformat=",",
                plot_bgcolor="#F8FAFC",
                paper_bgcolor="#FFFFFF",
                font_color="#0F172A",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title_text=""),
            )
            st.plotly_chart(fig_cmp, width="stretch")

    if is_admin:
        with st.expander("한국관광 데이터랩(DataLab) OpenAPI 동기화 · CSV 업로드 (관리자 전용)", expanded=False):
            st.caption(
                "출처: 한국관광공사 한국관광 데이터랩 (datalab.visitkorea.or.kr) · 공공데이터포털 '한국관광공사_빅데이터_지역별 방문자수_GW'"
            )
            d_col1, d_col2 = st.columns([1.1, 1.4])
            with d_col1:
                days_opt = st.number_input("API 조회 기간 (최근 일수)", min_value=30, max_value=365, value=180, step=30)
                if st.button("공공데이터포털 관광빅데이터 API 동기화 실행", type="primary", width="stretch"):
                    try:
                        res = sync_tourism_visitors_from_api(days_back=int(days_opt))
                        st.success(f"관광빅데이터 API 동기화 완료: {res['start_ymd']}~{res['end_ymd']} ({res['saved']}건 반영)")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            with d_col2:
                dl_csv = st.file_uploader("한국관광 데이터랩 다운로드 CSV 업로드", type=["csv"], key="datalab_csv_up")
                if dl_csv is not None and st.button("데이터랩 CSV DB 반영하기", width="stretch"):
                    try:
                        res_c = import_datalab_csv(dl_csv.getvalue(), default_zone_key=selected_zone)
                        st.success(
                            f"데이터랩 CSV 반영 완료: 방문자수 {res_c['saved_visitors']}건 · 국적별 비중 {res_c['saved_nationality']}건"
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))


def _render_settings_tab(selected_zone: str) -> None:
    st.markdown('<div class="section-title">권한 모드 비밀번호 설정 (관리자모드 / 일반모드)</div>', unsafe_allow_html=True)
    st.caption(
        "• 관리자모드 비밀번호: 데이터 동기화, URL 등록, API 설정 등 전체 관리 기능을 사용하는 비밀번호입니다.\n"
        "• 일반모드 비밀번호: 설정 시 접속 화면에서 비밀번호를 확인하며, 비워두면 바로 일반모드로 열람됩니다."
    )
    with st.form("password_settings_form"):
        pw_c1, pw_c2 = st.columns(2)
        with pw_c1:
            new_admin_pw = st.text_input(
                "관리자모드 비밀번호 (ADMIN_PASSWORD)",
                value=get_env("ADMIN_PASSWORD", "1234"),
                type="password",
            )
        with pw_c2:
            new_viewer_pw = st.text_input(
                "일반모드 비밀번호 (VIEWER_PASSWORD - 비워두면 로그인 생략)",
                value=get_env("VIEWER_PASSWORD", ""),
                type="password",
            )
        if st.form_submit_button("비밀번호 설정 저장", type="primary"):
            if not new_admin_pw.strip():
                st.error("관리자모드 비밀번호는 비워둘 수 없습니다.")
            else:
                save_env_values(
                    {
                        "ADMIN_PASSWORD": new_admin_pw.strip(),
                        "VIEWER_PASSWORD": new_viewer_pw.strip(),
                    }
                )
                st.success("비밀번호 설정이 저장되었습니다.")

    st.divider()
    st.markdown('<div class="section-title">외부 API 키 설정 (.env)</div>', unsafe_allow_html=True)
    with st.form("api_keys_form"):
        c1, c2 = st.columns(2)
        with c1:
            data_key = st.text_input(
                "공공데이터포털 인증키 (선택 - 없어도 LOCALDATA 공식파일 자동수집 동작)",
                value=get_env("DATA_GO_KR_API_KEY"),
                type="password",
            )
            apify_token = st.text_input(
                "Apify API 토큰 (APIFY_API_TOKEN)",
                value=get_env("APIFY_API_TOKEN"),
                type="password",
            )
            apify_actor = st.text_input(
                "Apify 기본 발굴 Actor ID",
                value=get_env("APIFY_AIRBNB_ACTOR", "tri_angle/airbnb-scraper"),
            )
        with c2:
            gemini_key = st.text_input(
                "Gemini API 키 (선택 - GEMINI_API_KEY)",
                value=get_env("GEMINI_API_KEY"),
                type="password",
            )
            gemini_model = st.text_input(
                "Gemini 모델명",
                value=get_env("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            )
            currency = st.selectbox("수집 통화", ["KRW", "USD"], index=0 if get_env("DEFAULT_CURRENCY", "KRW") == "KRW" else 1)

        if st.form_submit_button("API 설정 저장", type="primary"):
            save_env_values(
                {
                    "DATA_GO_KR_API_KEY": data_key,
                    "APIFY_API_TOKEN": apify_token,
                    "APIFY_AIRBNB_ACTOR": apify_actor,
                    "GEMINI_API_KEY": gemini_key,
                    "GEMINI_MODEL": gemini_model,
                    "DEFAULT_CURRENCY": currency,
                }
            )
            st.success(".env 파일에 API 설정을 저장했습니다.")

    st.divider()
    st.markdown('<div class="section-title">공공데이터 CSV / Apify JSON 수동 업로드</div>', unsafe_allow_html=True)
    u1, u2 = st.columns(2)
    with u1:
        st.markdown("**1) LOCALDATA CSV 업로드 (호스텔업 / 외도민)**")
        csv_industry = st.selectbox("업로드할 CSV 업종 지정", [INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK])
        csv_zone = "all" if selected_zone == "all" else selected_zone
        uploaded_csv = st.file_uploader("LOCALDATA CSV 파일 선택", type=["csv"], key="csv_up")
        if uploaded_csv is not None and st.button("CSV DB 반영하기", width="stretch"):
            res = import_localdata_csv(
                uploaded_csv.getvalue(),
                forced_industry=csv_industry,
                zone_filter=csv_zone,
            )
            purge_non_target_records()
            matched = run_auto_matching(zone_key="all")
            st.cache_data.clear()
            st.success(f"CSV 반영 완료: 전체 {res['total_rows']}행 중 5대 권역 {res['saved']}건 저장, {matched}건 자동 매칭")

    with u2:
        st.markdown("**2) Apify JSON 결과 업로드 (숙소 목록 또는 30일 달력 JSON)**")
        windows = compute_snapshot_windows()
        snap_type = st.selectbox("가격 JSON인 경우 스냅샷 구분", list(windows.keys()))
        uploaded_json = st.file_uploader("Apify Dataset JSON 파일 선택", type=["json"], key="json_up")
        json_zone = "seomyeon" if selected_zone == "all" else selected_zone
        if uploaded_json is not None and st.button("JSON DB 반영하기", width="stretch"):
            res = import_airbnb_json(
                uploaded_json.getvalue(),
                zone_key=json_zone,
                snapshot_type=snap_type,
                hostel_only=True,
            )
            purge_non_target_records()
            matched = run_auto_matching(zone_key="all")
            analyzed = analyze_all_listings(zone_key="all", use_ai=bool(get_env("GEMINI_API_KEY")))
            st.cache_data.clear()
            st.success(
                f"JSON 반영 완료: 호스텔·외도민 {res['listings_saved']}건 반영, "
                f"매칭 {matched}건, 고객층 분석 {analyzed['total']}건"
            )

    st.divider()
    st.markdown('<div class="section-title">수집 · 분석 실행 이력</div>', unsafe_allow_html=True)
    runs_df = get_collection_runs_df()
    if runs_df.empty:
        st.write("아직 실행 이력이 없습니다.")
    else:
        st.dataframe(runs_df, width="stretch", hide_index=True)


def _compute_filtered_overview(
    base_overview: Dict[str, Any],
    pub_df: pd.DataFrame,
    comp_df: pd.DataFrame,
    selected_industries: List[str],
) -> Dict[str, Any]:
    """상단 필터 바에서 선택한 대상 업종(호스텔업 / 외국인관광도시민박업)에 맞춰 상단 KPI 요약 수치를 실시간 동기화한다."""
    if set(selected_industries) >= {INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK}:
        return base_overview

    ov = dict(base_overview)
    if INDUSTRY_HOSTEL not in selected_industries:
        ov["hostel_active"] = 0
        ov["hostel_matched"] = 0
    if INDUSTRY_URBAN_MINBAK not in selected_industries:
        ov["minbak_active"] = 0
        ov["minbak_matched"] = 0

    ov["airbnb_total"] = len(comp_df)
    if not pub_df.empty:
        act_pub = pub_df[pub_df["status_name"] == "영업/정상"]
        ov["matched_businesses"] = int((act_pub["matched_room_count"] > 0).sum())
    else:
        ov["matched_businesses"] = 0

    if not comp_df.empty:
        valid_ratings = comp_df["rating"].dropna()
        ov["avg_rating"] = round(float(valid_ratings.mean()), 2) if not valid_ratings.empty else None
        ov["total_reviews"] = int(comp_df["reviews_count"].fillna(0).sum())
        valid_occ30 = comp_df["occ_30d_pct"].dropna()
        ov["avg_occ_30d"] = round(float(valid_occ30.mean()), 1) if not valid_occ30.empty else None
        valid_occ14 = comp_df["occ_14d_pct"].dropna()
        ov["avg_occ_14d"] = round(float(valid_occ14.mean()), 1) if not valid_occ14.empty else None

        wd_s = sorted(float(x) for x in comp_df["weekday_total"].dropna() if float(x) > 0)
        we_s = sorted(float(x) for x in comp_df["weekend_total"].dropna() if float(x) > 0)
        ov["avg_weekday_total"] = round(sum(wd_s) / len(wd_s)) if wd_s else None
        ov["median_weekday_total"] = round(wd_s[len(wd_s) // 2]) if wd_s else None
        ov["avg_weekend_total"] = round(sum(we_s) / len(we_s)) if we_s else None
        ov["median_weekend_total"] = round(we_s[len(we_s) // 2]) if we_s else None
    else:
        ov["avg_rating"] = None
        ov["total_reviews"] = 0
        ov["avg_occ_30d"] = None
        ov["avg_occ_14d"] = None
        ov["avg_weekday_total"] = None
        ov["median_weekday_total"] = None
        ov["avg_weekend_total"] = None
        ov["median_weekend_total"] = None

    return ov


def main() -> None:
    _inject_modern_css()
    _init_mobile_view_from_user_agent()
    can_view, is_admin = _ensure_authenticated()
    if not can_view:
        return

    is_mobile_now = bool(st.session_state.get("is_mobile_view", False))
    _apply_viewport_mode(is_mobile_now)

    current_zone = st.session_state.get("top_zone_select", get_env("DEFAULT_ZONE", "seomyeon"))
    selected_nav = _render_sidebar(is_admin, current_zone)
    # 모바일 모드이거나 휴대폰에서 PC버전으로 전환해 좌측 사이드바가 접힌 상태일 때도 상단 메뉴 그리드를 항상 제공
    if is_mobile_now or _is_mobile_user_agent():
        selected_nav = _render_mobile_top_nav(is_admin, is_mobile_view=is_mobile_now)
    selected_zone, selected_industries, is_mobile_view = _render_top_filter_bar()

    if is_mobile_view:
        st.markdown(
            """
            <style>
            @media (min-width: 769px) {
                .stMainBlockContainer, .block-container {
                    max-width: 560px !important;
                    margin-left: auto !important;
                    margin-right: auto !important;
                    border-left: 1px solid #E2E8F0;
                    border-right: 1px solid #E2E8F0;
                    box-shadow: 0 4px 20px rgba(15, 23, 42, 0.05);
                }
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

    if not selected_industries:
        st.warning("상단 '대상 업종' 필터에서 최소 1개 이상의 업종(호스텔업 또는 외국인관광도시민박업)을 선택해 주세요.")
        return

    base_overview = _cached_zone_overview(selected_zone)
    comp_df = _cached_competitor_df(selected_zone).copy()
    pub_df = _cached_public_businesses_df(selected_zone).copy()

    if not comp_df.empty:
        comp_df = comp_df[comp_df["industry_type"].isin(selected_industries)]
    if not pub_df.empty:
        pub_df = pub_df[pub_df["industry_type"].isin(selected_industries)]

    overview = _compute_filtered_overview(base_overview, pub_df, comp_df, selected_industries)

    if selected_nav == "상권 요약 · 지도":
        _render_overview_tab(selected_zone, overview, comp_df, pub_df)
    elif selected_nav in ("통합 현황 · URL 등록", "통합 상권 데이터"):
        _render_competitors_tab(comp_df, pub_df, is_admin=is_admin)
    elif selected_nav == "객단가(ADR) 맞춤 비교":
        _render_price_tab(selected_zone, comp_df)
    elif selected_nav == "고객층 · 포지셔닝 맞춤 비교":
        _render_customers_tab(selected_zone, comp_df)
    elif selected_nav == "외래객 수요 트렌드":
        _render_inbound_trend_tab(selected_zone, overview, is_admin=is_admin)
    elif selected_nav == "시스템 설정" and is_admin:
        _render_settings_tab(selected_zone)


if __name__ == "__main__":
    main()
