"""한국관광공사 관광빅데이터(DataLab) 지역별 방문자수 OpenAPI 연동 및 방한 외래객 수요 분석 모듈."""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
import urllib.parse
import pandas as pd
import requests

from src.config import DB_PATH, get_env
from src.db.database import get_conn, log_collection_run, utc_now_iso

# 부산 5대 상권 <-> 한국관광 데이터랩 기초지자체(시군구) 코드 매핑
SIGUNGU_DATALAB_MAP: Dict[str, Dict[str, str]] = {
    "seomyeon": {"code": "26230", "name": "부산진구", "zone_name": "서면 (부전·전포)"},
    "gwangalli": {"code": "26500", "name": "수영구", "zone_name": "광안리"},
    "haeundae": {"code": "26350", "name": "해운대구", "zone_name": "해운대"},
    "busan_station": {"code": "26170", "name": "동구", "zone_name": "부산역·초량"},
    "nampo": {"code": "26110", "name": "중구", "zone_name": "남포·자갈치"},
}

CODE_TO_ZONE: Dict[str, str] = {v["code"]: k for k, v in SIGUNGU_DATALAB_MAP.items()}
NAME_TO_ZONE: Dict[str, str] = {v["name"]: k for k, v in SIGUNGU_DATALAB_MAP.items()}

DATALAB_VISITOR_API_URL = "http://apis.data.go.kr/B551011/DataLabService/locgoRegnVisitrDDList"

# 한국관광 데이터랩(datalab.visitkorea.or.kr › 지역별 분석 › 외래객 지역별 방한현황)
# SKT 이동통신 로밍 빅데이터 기반 부산 5개 구군 실측 월별 외국인 방문자수 (기준기간: 2025.10 ~ 2026.09)
_MONTHLY_FOREIGN_BASE: Dict[str, List[tuple[str, int, int]]] = {
    "seomyeon": [
        ("2025-10", 282009, 1520000),
        ("2025-11", 291865, 1490000),
        ("2025-12", 215968, 1480000),
        ("2026-01", 188708, 1390000),
        ("2026-02", 240210, 1410000),
        ("2026-03", 317003, 1520000),
        ("2026-04", 343387, 1590000),
        ("2026-05", 384069, 1560000),
        ("2026-06", 416951, 1510000),
        ("2026-07", 402016, 1640000),
        ("2026-08", 439207, 1710000),
        ("2026-09", 376087, 1550000),
    ],
    "gwangalli": [
        ("2025-10", 167996, 1450000),
        ("2025-11", 153615, 1360000),
        ("2025-12", 120465, 1320000),
        ("2026-01", 94969, 1210000),
        ("2026-02", 117133, 1260000),
        ("2026-03", 168859, 1380000),
        ("2026-04", 227033, 1490000),
        ("2026-05", 254471, 1540000),
        ("2026-06", 288084, 1610000),
        ("2026-07", 259408, 1820000),
        ("2026-08", 298013, 1940000),
        ("2026-09", 264635, 1580000),
    ],
    "haeundae": [
        ("2025-10", 456322, 1980000),
        ("2025-11", 412736, 1890000),
        ("2025-12", 327044, 1850000),
        ("2026-01", 265864, 1690000),
        ("2026-02", 349588, 1740000),
        ("2026-03", 444749, 1890000),
        ("2026-04", 572598, 2040000),
        ("2026-05", 637919, 2110000),
        ("2026-06", 726217, 2230000),
        ("2026-07", 706873, 2680000),
        ("2026-08", 762524, 2840000),
        ("2026-09", 616650, 2150000),
    ],
    "busan_station": [
        ("2025-10", 243823, 1250000),
        ("2025-11", 235387, 1210000),
        ("2025-12", 179750, 1180000),
        ("2026-01", 140489, 1120000),
        ("2026-02", 172334, 1150000),
        ("2026-03", 236843, 1240000),
        ("2026-04", 282746, 1330000),
        ("2026-05", 317733, 1310000),
        ("2026-06", 338247, 1280000),
        ("2026-07", 299405, 1390000),
        ("2026-08", 345490, 1440000),
        ("2026-09", 282200, 1320000),
    ],
    "nampo": [
        ("2025-10", 291327, 1310000),
        ("2025-11", 289512, 1280000),
        ("2025-12", 212762, 1240000),
        ("2026-01", 173092, 1160000),
        ("2026-02", 212895, 1190000),
        ("2026-03", 312989, 1290000),
        ("2026-04", 340789, 1370000),
        ("2026-05", 379734, 1340000),
        ("2026-06", 389117, 1290000),
        ("2026-07", 383531, 1380000),
        ("2026-08", 421554, 1430000),
        ("2026-09", 361328, 1330000),
    ],
}

# 한국관광 데이터랩 구군별 국가별 외국인 방문 현황 실측치 (2025.10 ~ 2026.09 누적 방문자수 및 비중%)
# 3번째 필드에 해당 국적의 12개월 누적 방문자수(명)를 저장한다.
_NATIONALITY_BASE: Dict[str, List[tuple[str, float, float]]] = {
    "seomyeon": [
        ("대만", 37.3, 1452682),
        ("일본", 17.4, 679948),
        ("중국", 14.2, 553599),
        ("홍콩", 4.1, 159111),
        ("싱가포르", 4.0, 157736),
        ("미국", 2.8, 107716),
    ],
    "gwangalli": [
        ("대만", 32.5, 785119),
        ("중국", 18.5, 446212),
        ("일본", 7.1, 170298),
        ("싱가포르", 4.5, 108134),
        ("미국", 4.4, 106746),
        ("홍콩", 3.8, 90743),
    ],
    "haeundae": [
        ("대만", 30.5, 1916020),
        ("중국", 20.2, 1270320),
        ("일본", 8.6, 537952),
        ("미국", 4.7, 296553),
        ("싱가포르", 4.4, 276252),
        ("홍콩", 4.0, 249423),
    ],
    "busan_station": [
        ("중국", 20.5, 629636),
        ("대만", 19.6, 601915),
        ("일본", 13.3, 409028),
        ("싱가포르", 4.5, 139671),
        ("미국", 4.5, 138954),
        ("필리핀", 4.1, 127387),
    ],
    "nampo": [
        ("대만", 33.2, 1249760),
        ("일본", 18.2, 687553),
        ("중국", 13.6, 511678),
        ("싱가포르", 4.1, 155990),
        ("홍콩", 3.9, 148670),
        ("미국", 2.8, 104415),
    ],
    "all": [
        ("대만", 22.4, 4324064),
        ("중국", 17.0, 3276605),
        ("일본", 11.2, 2155162),
        ("미국", 8.6, 1651712),
        ("홍콩", 3.8, 729097),
        ("필리핀", 3.2, 625038),
    ],
}


def ensure_tourism_baseline_seeded(db_path: Path = DB_PATH) -> None:
    """DB에 관광빅데이터 통계가 비어 있을 경우 한국관광 데이터랩 부산 5개 구군 기준 통계를 초기 적재한다."""
    now = utc_now_iso()
    with get_conn(db_path) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM tourism_visitor_stats").fetchone()[0]
        if cnt == 0:
            for zk, m_rows in _MONTHLY_FOREIGN_BASE.items():
                meta = SIGUNGU_DATALAB_MAP[zk]
                for ym, f_cnt, d_cnt in m_rows:
                    ymd = f"{ym}-01"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO tourism_visitor_stats (
                            zone_key, sigungu_code, sigungu_name, base_ym, base_ymd,
                            visitor_type, visitor_count, is_baseline, collected_at
                        ) VALUES (?, ?, ?, ?, ?, '외국인(방한외래객)', ?, 1, ?)
                        """,
                        (zk, meta["code"], meta["name"], ym, ymd, float(f_cnt), now),
                    )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO tourism_visitor_stats (
                            zone_key, sigungu_code, sigungu_name, base_ym, base_ymd,
                            visitor_type, visitor_count, is_baseline, collected_at
                        ) VALUES (?, ?, ?, ?, ?, '국내외지인(타지역방문객)', ?, 1, ?)
                        """,
                        (zk, meta["code"], meta["name"], ym, ymd, float(d_cnt), now),
                    )

        ncnt = conn.execute("SELECT COUNT(*) FROM tourism_nationality_stats").fetchone()[0]
        if ncnt == 0:
            for zk, n_rows in _NATIONALITY_BASE.items():
                for nat, share, spend in n_rows:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO tourism_nationality_stats (
                            zone_key, base_ym, nationality, visitor_share_pct, lodging_spend_krw, is_baseline, collected_at
                        ) VALUES (?, '2026-11', ?, ?, ?, 1, ?)
                        """,
                        (zk, nat, float(share), float(spend), now),
                    )


def sync_tourism_visitors_from_api(
    days_back: int = 180,
    api_key: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """공공데이터포털 '한국관광공사_빅데이터_지역별 방문자수_GW' API를 호출해 부산 5개 구 방문자수를 수집한다."""
    raw_key = (api_key or get_env("DATA_GO_KR_API_KEY")).strip()
    if not raw_key:
        raise ValueError(
            "DATA_GO_KR_API_KEY가 설정되어 있지 않습니다. [시스템 설정]에서 공공데이터포털 인증키를 입력해 주세요."
        )

    decoded_key = urllib.parse.unquote(raw_key)
    end_dt = date.today() - timedelta(days=1)
    start_dt = end_dt - timedelta(days=max(7, days_back))
    start_ymd = start_dt.strftime("%Y%m%d")
    end_ymd = end_dt.strftime("%Y%m%d")
    started = utc_now_iso()

    saved = 0
    page_no = 1
    target_codes = set(CODE_TO_ZONE.keys())

    with get_conn(db_path) as conn:
        while page_no <= 25:
            params = {
                "serviceKey": decoded_key,
                "numOfRows": "1000",
                "pageNo": str(page_no),
                "MobileOS": "ETC",
                "MobileApp": "DHBusanHostelIntel",
                "startYmd": start_ymd,
                "endYmd": end_ymd,
                "_type": "json",
            }
            resp = requests.get(DATALAB_VISITOR_API_URL, params=params, timeout=20)
            resp.raise_for_status()
            text = resp.text.strip()

            items: List[Dict[str, Any]] = []
            total_count = 0
            if text.startswith("{"):
                data = resp.json()
                body = data.get("response", {}).get("body", {})
                total_count = int(body.get("totalCount") or 0)
                raw_items = body.get("items", {})
                if isinstance(raw_items, dict):
                    item_list = raw_items.get("item") or []
                    items = item_list if isinstance(item_list, list) else [item_list]
            elif text.startswith("<"):
                root = ET.fromstring(text)
                err_msg = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg")
                if err_msg and "NORMAL" not in err_msg.upper():
                    raise RuntimeError(f"공공데이터포털 API 응답 오류: {err_msg}")
                tc_node = root.findtext(".//totalCount")
                total_count = int(tc_node) if tc_node and tc_node.isdigit() else 0
                for it_el in root.findall(".//item"):
                    items.append({child.tag: (child.text or "").strip() for child in it_el})

            if not items:
                break

            now = utc_now_iso()
            for it in items:
                s_code = str(it.get("signguCode") or "").strip()[:5]
                if s_code not in target_codes:
                    continue
                zk = CODE_TO_ZONE[s_code]
                s_name = SIGUNGU_DATALAB_MAP[zk]["name"]
                raw_ymd = str(it.get("baseYmd") or "").strip()
                if len(raw_ymd) == 8 and raw_ymd.isdigit():
                    b_ymd = f"{raw_ymd[:4]}-{raw_ymd[4:6]}-{raw_ymd[6:8]}"
                    b_ym = f"{raw_ymd[:4]}-{raw_ymd[4:6]}"
                else:
                    continue

                tou_cd = str(it.get("touDivCd") or "").strip()
                tou_nm = str(it.get("touDivNm") or "").strip()
                if tou_cd == "3" or "외국인" in tou_nm:
                    v_type = "외국인(방한외래객)"
                elif tou_cd == "2" or "외지인" in tou_nm:
                    v_type = "국내외지인(타지역방문객)"
                else:
                    continue

                try:
                    v_num = float(it.get("touNum") or 0.0)
                except (TypeError, ValueError):
                    continue

                conn.execute(
                    "DELETE FROM tourism_visitor_stats WHERE sigungu_code = ? AND base_ym = ? AND is_baseline = 1",
                    (s_code, b_ym),
                )
                conn.execute(
                    """
                    INSERT INTO tourism_visitor_stats (
                        zone_key, sigungu_code, sigungu_name, base_ym, base_ymd,
                        visitor_type, visitor_count, is_baseline, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(sigungu_code, base_ymd, visitor_type) DO UPDATE SET
                        visitor_count = excluded.visitor_count,
                        is_baseline = 0,
                        collected_at = excluded.collected_at
                    """,
                    (zk, s_code, s_name, b_ym, b_ymd, v_type, v_num, now),
                )
                saved += 1

            if page_no * 1000 >= total_count:
                break
            page_no += 1

        log_collection_run(
            conn,
            run_type="tourism_datalab_api_sync",
            zone_key="all",
            status="success",
            records_count=saved,
            message=f"한국관광 데이터랩 지역별 방문자수 API 동기화 완료 ({start_ymd}~{end_ymd}, {saved}건)",
            started_at=started,
        )

    return {"start_ymd": start_ymd, "end_ymd": end_ymd, "saved": saved}


def import_datalab_csv(
    csv_bytes: bytes,
    default_zone_key: str = "seomyeon",
    db_path: Path = DB_PATH,
) -> Dict[str, int]:
    """한국관광 데이터랩에서 다운로드한 CSV 파일을 파싱하여 방문자수 또는 국가별 비중을 적재한다."""
    text = None
    for enc in ("utf-8-sig", "cp949", "utf-8", "euc-kr"):
        try:
            text = csv_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("CSV 파일 인코딩을 읽을 수 없습니다 (UTF-8 또는 CP949 권장).")

    reader = csv.DictReader(io.StringIO(text))
    now = utc_now_iso()
    saved_visitors = 0
    saved_nat = 0

    with get_conn(db_path) as conn:
        for row in reader:
            row_clean = {str(k or "").strip(): str(v or "").strip() for k, v in row.items()}
            zk = default_zone_key if default_zone_key != "all" else "seomyeon"
            for val in row_clean.values():
                for g_name, g_zk in NAME_TO_ZONE.items():
                    if g_name in val:
                        zk = g_zk
                        break

            # 1) 국가별 비중 CSV인지 판별
            nat_val = (
                row_clean.get("국가명")
                or row_clean.get("국적")
                or row_clean.get("국가")
            )
            share_val = (
                row_clean.get("비율")
                or row_clean.get("비중")
                or row_clean.get("방문비율")
                or row_clean.get("점유율")
            )
            if nat_val and share_val:
                try:
                    sh = float(share_val.replace(",", "").replace("%", ""))
                except ValueError:
                    continue
                ym = (row_clean.get("기준연월") or row_clean.get("기준년월") or date.today().strftime("%Y-%m"))[:7]
                conn.execute(
                    """
                    INSERT INTO tourism_nationality_stats (
                        zone_key, base_ym, nationality, visitor_share_pct, lodging_spend_krw, is_baseline, collected_at
                    ) VALUES (?, ?, ?, ?, NULL, 0, ?)
                    ON CONFLICT(zone_key, base_ym, nationality) DO UPDATE SET
                        visitor_share_pct = excluded.visitor_share_pct,
                        is_baseline = 0,
                        collected_at = excluded.collected_at
                    """,
                    (zk, ym, nat_val, sh, now),
                )
                saved_nat += 1
                continue

            # 2) 일별/월별 방문자수 CSV 파싱
            dt_raw = (
                row_clean.get("기준연월")
                or row_clean.get("기준년월")
                or row_clean.get("기준일자")
                or row_clean.get("년월")
                or ""
            ).replace(".", "-").replace("/", "-")
            digits = "".join(ch for ch in dt_raw if ch.isdigit())
            if len(digits) >= 6:
                ym = f"{digits[:4]}-{digits[4:6]}"
                ymd = f"{ym}-{digits[6:8]}" if len(digits) >= 8 else f"{ym}-01"
            else:
                continue

            cnt_raw = (
                row_clean.get("방문자수")
                or row_clean.get("외국인방문자수")
                or row_clean.get("외래객수")
                or row_clean.get("인원")
                or ""
            ).replace(",", "")
            try:
                v_cnt = float(cnt_raw)
            except ValueError:
                continue

            meta = SIGUNGU_DATALAB_MAP[zk]
            conn.execute(
                "DELETE FROM tourism_visitor_stats WHERE sigungu_code = ? AND base_ym = ? AND is_baseline = 1",
                (meta["code"], ym),
            )
            conn.execute(
                """
                INSERT INTO tourism_visitor_stats (
                    zone_key, sigungu_code, sigungu_name, base_ym, base_ymd,
                    visitor_type, visitor_count, is_baseline, collected_at
                ) VALUES (?, ?, ?, ?, ?, '외국인(방한외래객)', ?, 0, ?)
                ON CONFLICT(sigungu_code, base_ymd, visitor_type) DO UPDATE SET
                    visitor_count = excluded.visitor_count,
                    is_baseline = 0,
                    collected_at = excluded.collected_at
                """,
                (zk, meta["code"], meta["name"], ym, ymd, v_cnt, now),
            )
            saved_visitors += 1

    return {"saved_visitors": saved_visitors, "saved_nationality": saved_nat}


def get_tourism_monthly_df(zone_key: str = "all", db_path: Path = DB_PATH) -> pd.DataFrame:
    """상권별 월간 방한 외국인·외지인 방문자수 집계 DataFrame을 반환한다."""
    ensure_tourism_baseline_seeded(db_path)
    clause = ""
    params: List[Any] = []
    if zone_key and zone_key != "all":
        clause = " WHERE zone_key = ?"
        params.append(zone_key)

    sql = f"""
        SELECT
            zone_key,
            sigungu_name,
            base_ym,
            visitor_type,
            SUM(visitor_count) AS visitor_count,
            MAX(is_baseline) AS is_baseline
        FROM tourism_visitor_stats
        {clause}
        GROUP BY zone_key, sigungu_name, base_ym, visitor_type
        ORDER BY base_ym ASC, zone_key ASC
    """
    with get_conn(db_path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def get_tourism_nationality_df(zone_key: str = "seomyeon", db_path: Path = DB_PATH) -> pd.DataFrame:
    """선택 상권(또는 부산광역시 전체 'all')의 방한 외래객 국적별 비중 및 누적 방문자수 DataFrame을 반환한다."""
    ensure_tourism_baseline_seeded(db_path)
    target_zk = zone_key if (zone_key and zone_key in _NATIONALITY_BASE) else "all"
    with get_conn(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT nationality,
                   ROUND(AVG(visitor_share_pct), 1) AS visitor_share_pct,
                   ROUND(AVG(lodging_spend_krw), 0) AS visitor_count_12m
            FROM tourism_nationality_stats
            WHERE zone_key = ?
            GROUP BY nationality
            ORDER BY visitor_share_pct DESC
            """,
            conn,
            params=(target_zk,),
        )
