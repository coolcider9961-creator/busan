"""대시보드 및 리포트 출력용 SQLite 조회 함수 (호스텔업·외국인관광도시민박업 전용 통합 뷰)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK
from src.db.database import DB_PATH, get_conn


def classify_room_category(room_type: Any, property_type: Any, capacity: Any, title: Any = "") -> str:
    """객실유형을 '도미토리·다인실' / '개인실' / '독채·단체형' 3가지로 명확히 분류한다."""
    rt = str(room_type or "").lower()
    pt = str(property_type or "").lower()
    t = str(title or "").lower()
    cap = 0
    try:
        cap = int(float(capacity)) if capacity is not None and not pd.isna(capacity) else 0
    except (TypeError, ValueError):
        cap = 0

    if "shared" in rt or "다인실" in pt or "도미토리" in t or "캡슐" in t or "dorm" in t:
        return "도미토리·다인실"
    if "entire" in rt or "전체" in pt or "독채" in t or cap >= 5:
        return "독채·단체형"
    if "private" in rt or "개인실" in pt or "객실" in pt or (1 <= cap <= 4):
        return "개인실"
    return "미분류"


def get_zone_overview(zone_key: Optional[str] = "seomyeon", db_path: Path = DB_PATH) -> Dict[str, Any]:
    """선택 권역의 업종별(호스텔업 / 외도민) 및 에어비앤비 KPI 요약을 반환한다."""
    zone_clause = ""
    params: List[Any] = []
    if zone_key and zone_key != "all":
        zone_clause = " AND zone_key = ?"
        params.append(zone_key)

    with get_conn(db_path) as conn:
        pub_counts = conn.execute(
            f"""
            SELECT industry_type, status_name, COUNT(*) AS cnt
            FROM public_businesses
            WHERE industry_type IN ('호스텔업', '외국인관광도시민박업') {zone_clause}
            GROUP BY industry_type, status_name
            """,
            params,
        ).fetchall()

        airbnb_counts = conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT m.public_id) AS matched_businesses,
                   AVG(a.rating) AS avg_rating,
                   SUM(a.reviews_count) AS total_reviews,
                   AVG(a.occ_14d_pct) AS avg_occ_14d,
                   AVG(a.occ_30d_pct) AS avg_occ_30d
            FROM airbnb_listings a
            LEFT JOIN business_listing_matches m ON m.listing_id = a.listing_id
            WHERE a.is_hostel_like = 1 {zone_clause.replace('zone_key', 'a.zone_key')}
            """,
            params,
        ).fetchone()

        latest_prices = conn.execute(
            f"""
            SELECT p.snapshot_type, p.total_price
            FROM price_snapshots p
            JOIN airbnb_listings a ON a.listing_id = p.listing_id
            WHERE a.is_hostel_like = 1
              AND p.total_price BETWEEN 10000 AND 1500000
              AND p.snapshot_date = (
                SELECT MAX(p2.snapshot_date)
                FROM price_snapshots p2
                WHERE p2.listing_id = p.listing_id
                  AND p2.snapshot_type = p.snapshot_type
                  AND p2.total_price BETWEEN 10000 AND 1500000
            )
            {zone_clause.replace('zone_key', 'a.zone_key')}
            """,
            params,
        ).fetchall()

        matched_pub_counts = conn.execute(
            f"""
            SELECT pb.industry_type, COUNT(DISTINCT pb.id) AS cnt
            FROM public_businesses pb
            JOIN business_listing_matches m ON m.public_id = pb.id
            JOIN airbnb_listings a ON a.listing_id = m.listing_id AND a.is_hostel_like = 1
            WHERE pb.industry_type IN ('호스텔업', '외국인관광도시민박업')
              AND pb.status_name = '영업/정상'
              {zone_clause.replace('zone_key', 'pb.zone_key')}
            GROUP BY pb.industry_type
            """,
            params,
        ).fetchall()

        demo_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM airbnb_listings WHERE is_demo = 1"
        ).fetchone()["c"]

    summary: Dict[str, Any] = {
        "hostel_active": 0,
        "hostel_closed": 0,
        "hostel_matched": 0,
        "minbak_active": 0,
        "minbak_closed": 0,
        "minbak_matched": 0,
        "airbnb_total": airbnb_counts["total"] or 0,
        "airbnb_active": airbnb_counts["total"] or 0,
        "matched_businesses": airbnb_counts["matched_businesses"] or 0,
        "avg_rating": round(airbnb_counts["avg_rating"], 2) if airbnb_counts["avg_rating"] else None,
        "total_reviews": airbnb_counts["total_reviews"] or 0,
        "avg_occ_14d": round(airbnb_counts["avg_occ_14d"], 1) if airbnb_counts["avg_occ_14d"] is not None else None,
        "avg_occ_30d": round(airbnb_counts["avg_occ_30d"], 1) if airbnb_counts["avg_occ_30d"] is not None else None,
        "avg_weekday_total": None,
        "avg_weekend_total": None,
        "median_weekday_total": None,
        "median_weekend_total": None,
        "has_demo_data": demo_cnt > 0,
    }

    for row in matched_pub_counts:
        if row["industry_type"] == INDUSTRY_HOSTEL:
            summary["hostel_matched"] = row["cnt"]
        elif row["industry_type"] == INDUSTRY_URBAN_MINBAK:
            summary["minbak_matched"] = row["cnt"]

    for row in pub_counts:
        ind = row["industry_type"]
        st = row["status_name"]
        cnt = row["cnt"]
        if ind == INDUSTRY_HOSTEL:
            if st == "영업/정상":
                summary["hostel_active"] += cnt
            else:
                summary["hostel_closed"] += cnt
        elif ind == INDUSTRY_URBAN_MINBAK:
            if st == "영업/정상":
                summary["minbak_active"] += cnt
            else:
                summary["minbak_closed"] += cnt

    wd_vals = sorted(float(r["total_price"]) for r in latest_prices if r["snapshot_type"] == "D+7_WEEKDAY" and r["total_price"])
    we_vals = sorted(float(r["total_price"]) for r in latest_prices if r["snapshot_type"] == "D+14_WEEKEND" and r["total_price"])
    if wd_vals:
        summary["avg_weekday_total"] = round(sum(wd_vals) / len(wd_vals))
        summary["median_weekday_total"] = round(wd_vals[len(wd_vals) // 2])
    if we_vals:
        summary["avg_weekend_total"] = round(sum(we_vals) / len(we_vals))
        summary["median_weekend_total"] = round(we_vals[len(we_vals) // 2])

    return summary


def get_competitor_df(zone_key: Optional[str] = "seomyeon", db_path: Path = DB_PATH) -> pd.DataFrame:
    """개별 에어비앤비 객실(호스텔업·외도민 전용) + 매칭 공공상호 + 평일/주말 기본가 + 30일 예약률 + 주요 고객층 테이블."""
    zone_clause = " WHERE a.is_hostel_like = 1"
    params: List[Any] = []
    if zone_key and zone_key != "all":
        zone_clause += " AND a.zone_key = ?"
        params.append(zone_key)

    sql = f"""
        SELECT
            a.listing_id,
            a.title,
            a.url,
            a.room_type,
            a.property_type,
            a.is_hostel_like,
            a.capacity,
            a.bedrooms,
            a.beds,
            a.rating,
            a.reviews_count,
            a.recent_review_date,
            a.operating_status AS airbnb_operating_status,
            a.extra_guest_fee_note,
            a.occ_14d_pct,
            a.occ_30d_pct,
            a.booked_14d_days,
            a.booked_30d_days,
            a.calendar_updated_at,
            a.thumbnail_url,
            a.registration_number,
            a.zone_key,
            COALESCE(pb.lat, a.lat) AS lat,
            COALESCE(pb.lon, a.lon) AS lon,
            a.is_demo,
            pb.id AS public_id,
            CASE
                WHEN pb.industry_type IS NOT NULL THEN pb.industry_type
                WHEN a.registration_number LIKE '%|호스텔업|%' THEN '호스텔업'
                WHEN a.registration_number LIKE '%|외국인관광도시민박업|%' THEN '외국인관광도시민박업'
                ELSE '호스텔업'
            END AS industry_type,
            pb.business_name AS public_business_name,
            COALESCE(pb.status_name, '영업/정상') AS public_status_name,
            pb.road_address,
            pb.jibun_address,
            pb.license_date,
            pb.site_area,
            m.match_score,
            m.match_reason,
            m.is_manual,
            wd.nightly_price AS weekday_nightly,
            wd.cleaning_fee AS weekday_cleaning,
            wd.total_price AS weekday_total,
            wd.check_in AS weekday_checkin,
            we.nightly_price AS weekend_nightly,
            we.cleaning_fee AS weekend_cleaning,
            we.total_price AS weekend_total,
            we.check_in AS weekend_checkin,
            ci.analysis_method,
            ci.language_breakdown_json,
            ci.guest_segments_json,
            ci.positioning_tags_json,
            ci.strengths_json,
            ci.weaknesses_json,
            ci.benchmark_note
        FROM airbnb_listings a
        LEFT JOIN business_listing_matches m ON m.listing_id = a.listing_id
        LEFT JOIN public_businesses pb ON pb.id = m.public_id
        LEFT JOIN price_snapshots wd ON wd.id = (
            SELECT p.id FROM price_snapshots p
            WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+7_WEEKDAY' AND p.total_price BETWEEN 10000 AND 1500000
            ORDER BY p.snapshot_date DESC LIMIT 1
        )
        LEFT JOIN price_snapshots we ON we.id = (
            SELECT p.id FROM price_snapshots p
            WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+14_WEEKEND' AND p.total_price BETWEEN 10000 AND 1500000
            ORDER BY p.snapshot_date DESC LIMIT 1
        )
        LEFT JOIN customer_insights ci ON ci.listing_id = a.listing_id
        {zone_clause}
        ORDER BY COALESCE(a.occ_30d_pct, -1) DESC, COALESCE(a.reviews_count, 0) DESC, COALESCE(a.rating, 0) DESC
    """
    with get_conn(db_path) as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    if not df.empty:
        df["room_category"] = df.apply(
            lambda r: classify_room_category(r.get("room_type"), r.get("property_type"), r.get("capacity"), r.get("title")),
            axis=1,
        )
    else:
        df["room_category"] = pd.Series(dtype="str")
    return df


def get_public_businesses_df(
    zone_key: Optional[str] = "seomyeon",
    matched_only: bool = False,
    include_closed: bool = False,
    db_path: Path = DB_PATH,
) -> pd.DataFrame:
    """공공 인허가 숙소 목록(호스텔업 / 외국인관광도시민박업) 및 에어비앤비 매칭 집계 정보를 조회한다."""
    zone_clause = " WHERE pb.industry_type IN ('호스텔업', '외국인관광도시민박업')"
    if not include_closed:
        zone_clause += " AND pb.status_name = '영업/정상'"
    params: List[Any] = []
    if zone_key and zone_key != "all":
        zone_clause += " AND pb.zone_key = ?"
        params.append(zone_key)

    having_clause = "HAVING COUNT(a.listing_id) > 0" if matched_only else ""

    sql = f"""
        SELECT
            pb.*,
            COUNT(a.listing_id) AS matched_room_count,
            MIN(m.listing_id) AS matched_listing_id,
            MIN(a.url) AS matched_url,
            MAX(CASE WHEN a.thumbnail_url LIKE 'http%' THEN a.thumbnail_url END) AS matched_thumbnail_url,
            CASE
                WHEN COUNT(a.listing_id) > 1 THEN MIN(a.title) || ' 외 ' || (COUNT(a.listing_id) - 1) || '개 객실'
                ELSE MIN(a.title)
            END AS matched_airbnb_title,
            MIN(a.room_type) AS sample_room_type,
            MIN(a.property_type) AS sample_property_type,
            MAX(a.capacity) AS max_capacity,
            COALESCE(
                MAX(CASE WHEN a.extra_guest_fee_note LIKE '%(%' THEN a.extra_guest_fee_note END),
                MAX(a.extra_guest_fee_note)
            ) AS extra_guest_fee_note,
            ROUND(AVG(a.occ_14d_pct), 1) AS occ_14d_pct,
            ROUND(AVG(a.occ_30d_pct), 1) AS occ_30d_pct,
            ROUND(AVG(a.rating), 2) AS matched_rating,
            SUM(a.reviews_count) AS matched_reviews_count,
            MIN(a.operating_status) AS matched_airbnb_status,
            ROUND(AVG(wd.total_price), 0) AS weekday_total,
            ROUND(AVG(we.total_price), 0) AS weekend_total,
            MIN(ci.guest_segments_json) AS guest_segments_json,
            MIN(ci.positioning_tags_json) AS positioning_tags_json
        FROM public_businesses pb
        LEFT JOIN business_listing_matches m ON m.public_id = pb.id
        LEFT JOIN airbnb_listings a ON a.listing_id = m.listing_id AND a.is_hostel_like = 1
        LEFT JOIN price_snapshots wd ON wd.id = (
            SELECT p.id FROM price_snapshots p
            WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+7_WEEKDAY' AND p.total_price BETWEEN 10000 AND 1500000
            ORDER BY p.snapshot_date DESC LIMIT 1
        )
        LEFT JOIN price_snapshots we ON we.id = (
            SELECT p.id FROM price_snapshots p
            WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+14_WEEKEND' AND p.total_price BETWEEN 10000 AND 1500000
            ORDER BY p.snapshot_date DESC LIMIT 1
        )
        LEFT JOIN customer_insights ci ON ci.listing_id = a.listing_id
        {zone_clause}
        GROUP BY pb.id
        {having_clause}
        ORDER BY (COUNT(a.listing_id) > 0) DESC, COALESCE(AVG(a.occ_30d_pct), -1) DESC, COALESCE(SUM(a.reviews_count), 0) DESC, pb.industry_type, pb.business_name
    """
    with get_conn(db_path) as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    if not df.empty:
        df["room_category"] = df.apply(
            lambda r: classify_room_category(
                r.get("sample_room_type"),
                r.get("sample_property_type"),
                r.get("max_capacity"),
                r.get("matched_airbnb_title"),
            )
            if (r.get("matched_room_count") or 0) > 0
            else "미연결",
            axis=1,
        )
    else:
        df["room_category"] = pd.Series(dtype="str")
    return df


def get_tracked_urls_df(db_path: Path = DB_PATH) -> pd.DataFrame:
    """사용자가 직접 등록한 에어비앤비 URL 목록을 조회한다."""
    sql = """
        SELECT
            t.id,
            t.listing_id,
            t.url,
            t.zone_key,
            t.memo,
            t.created_at,
            pb.industry_type,
            pb.business_name AS public_business_name,
            pb.road_address,
            a.title AS airbnb_title,
            a.occ_30d_pct,
            a.rating,
            a.reviews_count
        FROM tracked_airbnb_urls t
        LEFT JOIN public_businesses pb ON pb.id = t.public_id
        LEFT JOIN airbnb_listings a ON a.listing_id = t.listing_id
        ORDER BY t.id DESC
    """
    with get_conn(db_path) as conn:
        return pd.read_sql_query(sql, conn)


def get_price_timeseries_df(zone_key: Optional[str] = "seomyeon", db_path: Path = DB_PATH) -> pd.DataFrame:
    """시계열 객단가 스냅샷 데이터를 조회한다."""
    zone_clause = " WHERE a.is_hostel_like = 1"
    params: List[Any] = []
    if zone_key and zone_key != "all":
        zone_clause += " AND a.zone_key = ?"
        params.append(zone_key)

    sql = f"""
        SELECT
            p.snapshot_date,
            p.snapshot_type,
            p.check_in,
            p.nightly_price,
            p.cleaning_fee,
            p.total_price,
            a.listing_id,
            a.title,
            CASE
                WHEN pb.industry_type IS NOT NULL THEN pb.industry_type
                WHEN a.registration_number LIKE '%|호스텔업|%' THEN '호스텔업'
                WHEN a.registration_number LIKE '%|외국인관광도시민박업|%' THEN '외국인관광도시민박업'
                ELSE '호스텔업'
            END AS industry_type
        FROM price_snapshots p
        JOIN airbnb_listings a ON a.listing_id = p.listing_id
        LEFT JOIN business_listing_matches m ON m.listing_id = a.listing_id
        LEFT JOIN public_businesses pb ON pb.id = m.public_id
        {zone_clause}
        ORDER BY p.snapshot_date ASC
    """
    with get_conn(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def get_collection_runs_df(limit: int = 30, db_path: Path = DB_PATH) -> pd.DataFrame:
    """최근 데이터 수집/분석 실행 이력을 조회한다."""
    with get_conn(db_path) as conn:
        return pd.read_sql_query(
            "SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?",
            conn,
            params=(limit,),
        )


def parse_json_col(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def export_excel_bytes(zone_key: Optional[str] = "seomyeon", db_path: Path = DB_PATH) -> bytes:
    """현재 권역의 에어비앤비 실영업 사업장 통합 현황·객실별 상세·시계열 스냅샷을 엑셀(.xlsx) 바이트로 내보낸다."""
    pub_df = get_public_businesses_df(zone_key, matched_only=True, db_path=db_path)
    comp_df = get_competitor_df(zone_key, db_path=db_path)
    ts_df = get_price_timeseries_df(zone_key, db_path=db_path)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pub_df.to_excel(writer, sheet_name="사업장기준_통합현황", index=False)
        comp_df.to_excel(writer, sheet_name="개별객실_상세현황", index=False)
        ts_df.to_excel(writer, sheet_name="객단가_시계열스냅샷", index=False)
    return buf.getvalue()
