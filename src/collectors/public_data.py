"""공공데이터(LOCALDATA CSV 및 공공데이터포털 Open API) 수집기.

대상 업종은 사용자가 지정한 2종만 수집·라벨링한다:
1) 호스텔업 (관광사업자 / 관광숙박업 중 '호스텔' 업종)
2) 외국인관광도시민박업
"""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from src.collectors.geo import assign_zone, parse_sigungu_dong, tm_to_wgs84
from src.config import (
    INDUSTRY_HOSTEL,
    INDUSTRY_URBAN_MINBAK,
    RAW_DIR,
    ZONES,
    get_env,
)
from src.db.database import DB_PATH, get_conn, log_collection_run, upsert_public_business, utc_now_iso

TOURISM_BUSINESSES_URL = "https://apis.data.go.kr/1741000/tourism_businesses/info"
URBAN_MINBAK_URL = "https://apis.data.go.kr/1741000/foreigner_city_homestays/info"
LOCALDATA_FILE_BASE = "https://file.localdata.go.kr"
BUSAN_ORG_CODE = "6260000_ALL"


def _pick(row: Dict[str, Any], candidates: Tuple[str, ...]) -> str:
    """여러 가능한 컬럼명 중 비어있지 않은 첫 값을 반환한다."""
    for key in candidates:
        val = row.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return ""


def _to_float(val: Any) -> Optional[float]:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(str(val).strip())
    except ValueError:
        return None


def _normalize_status(status_name: str, detail_status: str = "") -> str:
    combined = f"{status_name} {detail_status}"
    if "폐업" in combined or "말소" in combined or "취소" in combined:
        return "폐업"
    if "휴업" in combined or "정지" in combined:
        return "휴업"
    if "영업" in combined or "정상" in combined:
        return "영업/정상"
    return status_name.strip() or "미상"


def _detect_industry(raw_row: Dict[str, Any], forced_industry: Optional[str]) -> Optional[str]:
    """레코드에서 대상 업종(호스텔업 / 외국인관광도시민박업)만 판별한다."""
    detail = _pick(
        raw_row,
        (
            "관광숙박업상세명",
            "업태구분명",
            "세부업종명",
            "위생업태명",
            "관광사업업종명",
            "문화체육업종명",
            "개방서비스명",
            "opnSvcNm",
            "indutyNm",
            "sntUptaeNm",
            "uptaeNm",
        ),
    )
    name = _pick(raw_row, ("사업장명", "업소명", "bplcNm", "bplc_nm", "bizName"))

    if forced_industry in (INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK):
        if forced_industry == INDUSTRY_HOSTEL:
            # 관광숙박업 통합 데이터에서 관광호텔업/콘도/소형호텔 등은 제외하고 호스텔업만 추출
            raw_sub = _pick(raw_row, ("관광숙박업상세명", "업태구분명", "세부업종명", "위생업태명", "관광사업업종명"))
            if raw_sub:
                if "호스텔" not in raw_sub:
                    return None
            else:
                # 상세 업종명이 비어 있는 구형 관광호텔 레코드 배제
                if "호스텔" not in name:
                    return None
        return forced_industry

    if "외국인관광도시민박" in detail or "외국인관광 도시민박" in detail or "도시민박" in detail:
        return INDUSTRY_URBAN_MINBAK
    if "호스텔" in detail:
        return INDUSTRY_HOSTEL
    return None


def normalize_public_row(
    raw_row: Dict[str, Any],
    forced_industry: Optional[str] = None,
    busan_only: bool = True,
) -> Optional[Dict[str, Any]]:
    """원본 공공데이터 행을 내부 스키마로 정규화한다."""
    road_addr = _pick(raw_row, ("도로명전체주소", "도로명주소", "rdnWhlAddr", "rdnmAdr", "road_address"))
    jibun_addr = _pick(raw_row, ("소재지전체주소", "지번주소", "siteWhlAddr", "locatadd", "site_whl_addr"))
    full_addr = road_addr or jibun_addr

    if busan_only and full_addr and "부산" not in full_addr:
        return None

    industry_type = _detect_industry(raw_row, forced_industry)
    if industry_type is None:
        return None

    business_name = _pick(raw_row, ("사업장명", "업소명", "bplcNm", "bplc_nm", "bizName"))
    if not business_name:
        return None

    mng_no = _pick(raw_row, ("관리번호", "mgtNo", "mngNo", "mng_no"))
    org_cd = _pick(raw_row, ("개방자치단체코드", "opnSfTeamCode", "openAutnOrgCd"))
    source_key = f"{industry_type}:{org_cd or 'busan'}:{mng_no or business_name}:{full_addr[:30]}"

    raw_x = _to_float(_pick(raw_row, ("좌표정보(x)", "좌표정보(X)", "x", "X")))
    raw_y = _to_float(_pick(raw_row, ("좌표정보(y)", "좌표정보(Y)", "y", "Y")))
    lat, lon = tm_to_wgs84(raw_x, raw_y)

    sigungu, dong_name = parse_sigungu_dong(jibun_addr or road_addr)
    zone_key = assign_zone(lat=lat, lon=lon, address=f"{jibun_addr} {road_addr}")
    # 필수 5대 권역(서면·광안리·해운대·부산역·남포) 외 지역은 수집 대상에서 제외
    if busan_only and zone_key not in ZONES:
        return None

    raw_status = _pick(raw_row, ("영업상태명", "trdStateNm", "bizoprStatNm", "bizopr_stat_nm"))
    detail_status = _pick(raw_row, ("상세영업상태명", "dtlStateNm", "dtlBizoprStatNm", "dtl_bizopr_stat_nm"))

    detail_industry = _pick(
        raw_row,
        ("관광숙박업상세명", "업태구분명", "세부업종명", "위생업태명", "관광사업업종명", "문화체육업종명", "sntUptaeNm", "uptaeNm"),
    ) or industry_type

    return {
        "source_key": source_key,
        "mng_no": mng_no or None,
        "industry_type": industry_type,
        "detail_industry": detail_industry,
        "business_name": business_name,
        "status_name": _normalize_status(raw_status, detail_status),
        "detail_status_name": detail_status or raw_status or None,
        "license_date": _pick(raw_row, ("인허가일자", "apvPermYmd", "licensingYmd", "licensing_ymd")) or None,
        "close_date": _pick(raw_row, ("폐업일자", "dcbYmd", "clsbizYmd", "clsbiz_ymd")) or None,
        "road_address": road_addr or None,
        "jibun_address": jibun_addr or None,
        "phone": _pick(raw_row, ("소재지전화", "전화번호", "siteTel", "telno")) or None,
        "site_area": _to_float(_pick(raw_row, ("시설면적", "소재지면적", "건축연면적", "siteArea", "site_area"))),
        "sigungu": sigungu,
        "dong_name": dong_name,
        "zone_key": zone_key,
        "lat": lat,
        "lon": lon,
        "raw_x": raw_x,
        "raw_y": raw_y,
        "is_demo": 0,
    }


def decode_csv_bytes(raw_bytes: bytes) -> str:
    """한글 공공데이터 CSV 인코딩(cp949, euc-kr, utf-8-sig)을 자동 판별해 디코딩한다."""
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def import_localdata_csv(
    csv_content: bytes | str | Path,
    forced_industry: Optional[str] = None,
    zone_filter: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> Dict[str, int]:
    """LOCALDATA CSV 파일 또는 바이트를 파싱해 DB에 적재한다."""
    started = utc_now_iso()
    if isinstance(csv_content, Path):
        text = decode_csv_bytes(csv_content.read_bytes())
    elif isinstance(csv_content, bytes):
        text = decode_csv_bytes(csv_content)
    else:
        text = csv_content

    reader = csv.DictReader(io.StringIO(text))
    total_rows = 0
    saved = 0
    by_industry = {INDUSTRY_HOSTEL: 0, INDUSTRY_URBAN_MINBAK: 0}

    with get_conn(db_path) as conn:
        for raw_row in reader:
            total_rows += 1
            normalized = normalize_public_row(raw_row, forced_industry=forced_industry, busan_only=True)
            if not normalized:
                continue
            if zone_filter and zone_filter != "all" and normalized["zone_key"] != zone_filter:
                continue
            upsert_public_business(conn, normalized)
            saved += 1
            by_industry[normalized["industry_type"]] = by_industry.get(normalized["industry_type"], 0) + 1

        log_collection_run(
            conn,
            run_type="public_csv_import",
            zone_key=zone_filter,
            status="success",
            records_count=saved,
            message=f"전체 {total_rows}행 중 {saved}건 저장 (호스텔업 {by_industry[INDUSTRY_HOSTEL]}, 외도민 {by_industry[INDUSTRY_URBAN_MINBAK]})",
            started_at=started,
        )

    return {"total_rows": total_rows, "saved": saved, **by_industry}


def _extract_api_items(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """행정안전부 공공데이터포털 Open API JSON 응답에서 목록과 총 건수를 추출한다."""
    # 표준 response -> body -> items -> item 구조 또는 row 구조 대응
    if "response" in payload:
        body = payload.get("response", {}).get("body", {})
        total_count = int(body.get("totalCount") or 0)
        items_obj = body.get("items") or {}
        if isinstance(items_obj, dict):
            items = items_obj.get("item") or []
        elif isinstance(items_obj, list):
            items = items_obj
        else:
            items = []
        if isinstance(items, dict):
            items = [items]
        return items, total_count

    # 일부 행안부 API의 row 기반 응답 대응
    for key, val in payload.items():
        if isinstance(val, list) and len(val) >= 2 and isinstance(val[1], dict) and "row" in val[1]:
            rows = val[1]["row"]
            return rows, len(rows)

    return [], 0


def _fetch_api_pages(
    base_url: str,
    api_key: str,
    forced_industry: str,
    zone_filter: Optional[str],
    max_pages: int = 30,
    page_size: int = 200,
) -> List[Dict[str, Any]]:
    """공공데이터포털 Open API를 페이지 단위로 호출해 부산 대상 행을 수집한다."""
    collected: List[Dict[str, Any]] = []
    zone_sigungu = ZONES[zone_filter].sigungu if (zone_filter and zone_filter in ZONES) else None

    for page_no in range(1, max_pages + 1):
        params: Dict[str, Any] = {
            "serviceKey": api_key,
            "pageNo": page_no,
            "numOfRows": page_size,
            "returnType": "json",
            "resultType": "json",
        }
        # 지역 검색 파라미터 지원 시 활용, 미지원 시 클라이언트 측에서 부산 필터링
        if zone_sigungu:
            params["cond[rdnWhlAddr::LIKE]"] = f"부산%{zone_sigungu}"
        else:
            params["cond[rdnWhlAddr::LIKE]"] = "부산"

        resp = requests.get(base_url, params=params, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
        items, total_count = _extract_api_items(payload)
        if not items:
            break

        for raw in items:
            normalized = normalize_public_row(raw, forced_industry=forced_industry, busan_only=True)
            if not normalized:
                continue
            if zone_filter and zone_filter != "all" and normalized["zone_key"] != zone_filter:
                continue
            collected.append(normalized)

        if page_no * page_size >= total_count:
            break

    return collected


def sync_public_data_from_api(
    zone_filter: Optional[str] = "seomyeon",
    api_key: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """공공데이터포털 API로 호스텔업 + 외국인관광도시민박업 최신 데이터를 동기화한다."""
    started = utc_now_iso()
    key = (api_key or get_env("DATA_GO_KR_API_KEY")).strip()
    if not key:
        raise ValueError("DATA_GO_KR_API_KEY가 설정되어 있지 않습니다. .env 또는 설정 화면에서 입력해 주세요.")

    hostels = _fetch_api_pages(TOURISM_BUSINESSES_URL, key, INDUSTRY_HOSTEL, zone_filter)
    minbaks = _fetch_api_pages(URBAN_MINBAK_URL, key, INDUSTRY_URBAN_MINBAK, zone_filter)
    all_rows = hostels + minbaks

    with get_conn(db_path) as conn:
        for row in all_rows:
            upsert_public_business(conn, row)
        log_collection_run(
            conn,
            run_type="public_api_sync",
            zone_key=zone_filter,
            status="success",
            records_count=len(all_rows),
            message=f"호스텔업 {len(hostels)}건, 외국인관광도시민박업 {len(minbaks)}건 동기화",
            started_at=started,
        )

    return {
        "hostel_count": len(hostels),
        "urban_minbak_count": len(minbaks),
        "total": len(all_rows),
    }


def sync_public_data_from_localdata_portal(
    zone_filter: Optional[str] = None,
    org_code: str = BUSAN_ORG_CODE,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """API 키 없이 행정안전부 LOCALDATA 공식 파일 서버에서 부산광역시 최신 CSV 2종을 직접 받아 적재한다."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.data.go.kr/",
        }
    )
    session.get(f"{LOCALDATA_FILE_BASE}/file/tourist_accommodations/info", timeout=15)
    session.headers["Referer"] = f"{LOCALDATA_FILE_BASE}/file/tourist_accommodations/info"

    # 1) 문화_관광숙박업_부산광역시.csv (호스텔업 추출)
    session.get(f"{LOCALDATA_FILE_BASE}/file/validate/download-count", timeout=10)
    r_hostel = session.get(
        f"{LOCALDATA_FILE_BASE}/file/download/tourist_accommodations/info",
        params={"orgCode": org_code},
        timeout=30,
    )
    r_hostel.raise_for_status()
    hostel_csv_path = RAW_DIR / "문화_관광숙박업_부산광역시.csv"
    hostel_csv_path.write_bytes(r_hostel.content)

    # 2) 문화_외국인관광도시민박업_부산광역시.csv
    session.get(f"{LOCALDATA_FILE_BASE}/file/validate/download-count", timeout=10)
    r_minbak = session.get(
        f"{LOCALDATA_FILE_BASE}/file/download/foreigner_city_homestays/info",
        params={"orgCode": org_code},
        timeout=30,
    )
    r_minbak.raise_for_status()
    minbak_csv_path = RAW_DIR / "문화_외국인관광도시민박업_부산광역시.csv"
    minbak_csv_path.write_bytes(r_minbak.content)

    res_h = import_localdata_csv(
        r_hostel.content,
        forced_industry=INDUSTRY_HOSTEL,
        zone_filter=zone_filter,
        db_path=db_path,
    )
    res_m = import_localdata_csv(
        r_minbak.content,
        forced_industry=INDUSTRY_URBAN_MINBAK,
        zone_filter=zone_filter,
        db_path=db_path,
    )
    return {
        "hostel_count": res_h[INDUSTRY_HOSTEL],
        "urban_minbak_count": res_m[INDUSTRY_URBAN_MINBAK],
        "total": res_h["saved"] + res_m["saved"],
    }

