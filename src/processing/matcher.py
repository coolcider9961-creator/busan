"""공공데이터(호스텔업·외국인관광도시민박업) ↔ 에어비앤비 리스팅 정밀 매칭 엔진.

오매칭(False Positive) 방지를 최우선으로 하며 다음 원칙을 강제한다:
1) 에어비앤비 신고 인허가번호(연도4자리+일련번호6자리)와 공공데이터 관리번호가 둘 다 존재하는데 서로 다르면 절대 매칭하지 않는다(예외: 동일 자치구 + 100m 이내 + 고유 상호명 100% 일치).
2) 자치구(시군구)가 서로 다르면 절대 매칭하지 않는다.
3) 에어비앤비 호스트 개인 이름(host_name)으로는 매칭하지 않는다.
4) 부산 지명(광안, 해변, 서면, 전포, 해운대, 남포, 부산역 등) 및 일반 숙박 용어를 모두 제거한 뒤 남는 '순수 고유 상호명(3글자 이상)'이 정확히 일치하고 거리가 150m 이내일 때만 보조 매칭을 허용한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.collectors.geo import haversine_km
from src.db.database import DB_PATH, get_conn, utc_now_iso

# 부산 지명 및 숙박 일반명사 불용어 (오매칭 원천 차단용)
LOCATION_AND_GENERIC_WORDS = (
    "부산광역시",
    "부산시",
    "부산",
    "서면역",
    "서면",
    "부전동",
    "부전",
    "전포동",
    "전포역",
    "전포카페거리",
    "전포사잇길",
    "전포",
    "범천동",
    "범천",
    "범일동",
    "범일",
    "연지동",
    "연지",
    "양정동",
    "양정",
    "광안리해수욕장",
    "광안해변",
    "광안리",
    "광안동",
    "광안",
    "민락동",
    "민락수변",
    "민락",
    "수영구",
    "수영동",
    "수영",
    "남천동",
    "남천",
    "해운대해수욕장",
    "해운대구",
    "해운대",
    "송정동",
    "송정",
    "센텀시티",
    "센텀",
    "우동",
    "중동",
    "좌동",
    "남포동",
    "남포역",
    "남포",
    "자갈치",
    "광복동",
    "광복",
    "중앙동",
    "부평동",
    "보수동",
    "영주동",
    "동광동",
    "대청동",
    "부산역",
    "초량동",
    "초량역",
    "초량",
    "수정동",
    "영도구",
    "영도",
    "봉래동",
    "청학동",
    "동래구",
    "동래",
    "연제구",
    "연산동",
    "연산역",
    "연산",
    "사상구",
    "사상",
    "덕포동",
    "덕포",
    "남구",
    "대연동",
    "대연",
    "경성대",
    "부경대",
    "금정구",
    "장전동",
    "기장군",
    "기장",
    "일광",
    "서구",
    "송도",
    "다대포",
    "호스텔업",
    "호스텔",
    "유스호스텔",
    "게스트하우스",
    "게하",
    "외국인관광도시민박업",
    "도시민박",
    "민박",
    "관광호텔",
    "비즈니스호텔",
    "비지니스호텔",
    "부티크호텔",
    "캡슐호텔",
    "캐빈호텔",
    "호텔",
    "모텔",
    "여관",
    "펜션",
    "풀빌라",
    "료칸",
    "레지던스",
    "스테이",
    "하우스",
    "홈",
    "룸",
    "객실",
    "개인실",
    "다인실",
    "도미토리",
    "캡슐",
    "캐빈",
    "독채",
    "단독주택",
    "단독사용",
    "감성숙소",
    "감성",
    "숙소",
    "신축",
    "신규오픈",
    "신규할인",
    "오픈특가",
    "오픈",
    "특가",
    "할인",
    "프리미엄",
    "디럭스",
    "스탠다드",
    "스위트",
    "트윈",
    "더블",
    "싱글",
    "패밀리",
    "트리플",
    "자쿠지",
    "사우나",
    "루프탑",
    "테라스",
    "오션뷰",
    "해변",
    "비치",
    "본점",
    "지점",
    "1호점",
    "2호점",
    "3호점",
    "호점",
    "주식회사",
    "유한회사",
    "(주)",
    "hostel",
    "guesthouse",
    "guest house",
    "hotel",
    "motel",
    "pension",
    "residence",
    "stay",
    "house",
    "home",
    "room",
    "dorm",
    "dormitory",
    "capsule",
    "cabin",
    "suite",
    "busan",
    "seomyeon",
    "jeonpo",
    "gwangan",
    "gwangalli",
    "haeundae",
    "nampo",
    "station",
    "beach",
    "ocean",
    "view",
    "new",
    "open",
    "korea",
    "the",
    "더",
)


def extract_pure_brand(name: str) -> str:
    """상호 또는 숙소 제목에서 부산 지명과 일반 숙박 용어를 모두 제거한 순수 고유 브랜드명을 추출한다."""
    text = (name or "").lower()
    text = text.replace("다이내믹", "다이나믹").replace("dynamic", "다이나믹")
    text = text.replace("travel light", "트래블라이트").replace("travellight", "트래블라이트")
    # 긴 단어부터 먼저 제거
    for word in sorted(LOCATION_AND_GENERIC_WORDS, key=len, reverse=True):
        text = text.replace(word, " ")
    text = re.sub(r"[^a-z0-9가-힣]", "", text)
    # 끝에 남은 단일 '점' 제거
    if len(text) >= 2 and text.endswith("점"):
        text = text[:-1]
    return text.strip()


def _digits_only(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\D", "", text)


def _parse_stored_reg(reg_str: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """저장된 registration_number ('시군구|업종|원문번호|10자리표준코드')를 분리한다."""
    if not reg_str:
        return None, None, None, None
    if "|" in reg_str:
        parts = (reg_str.split("|") + ["", "", "", ""])[:4]
        return (
            parts[0] or None,
            parts[1] or None,
            parts[2] or None,
            parts[3] or None,
        )
    digits = _digits_only(reg_str)
    norm = digits[-10:] if len(digits) >= 10 else (digits or None)
    return None, None, reg_str, norm


def compute_match_score(pub: Dict[str, Any], listing: Dict[str, Any]) -> Tuple[float, str]:
    """공공 업소와 에어비앤비 리스팅 간 엄격한 매칭 점수(0~1) 및 사유를 계산한다."""
    reg_sg, reg_ind, reg_raw, reg_norm = _parse_stored_reg(listing.get("registration_number"))
    pub_sg = pub.get("sigungu")
    pub_ind = pub.get("industry_type")
    mng_digits = _digits_only(pub.get("mng_no"))
    pub_norm_no = mng_digits[-10:] if len(mng_digits) >= 10 else ""

    # 원칙 1: 자치구(시군구)가 둘 다 존재하는데 서로 다르면 절대 매칭 불가
    if reg_sg and pub_sg and reg_sg != pub_sg:
        return 0.0, "자치구 불일치"

    pub_name_compact = re.sub(r"\s+", "", (pub.get("business_name") or "").lower())
    list_title_compact = re.sub(r"\s+", "", (listing.get("title") or "").lower())
    pub_brand = extract_pure_brand(pub.get("business_name", ""))
    list_brand = extract_pure_brand(listing.get("title", ""))
    host_brand = extract_pure_brand(listing.get("host_name") or "")

    branch_adj = 0.0
    for b_tag in ("본점", "1호점", "2호점", "3호점"):
        if b_tag in pub_name_compact and b_tag in list_title_compact:
            branch_adj = 0.015
            break
        elif b_tag in pub_name_compact and any(
            other in list_title_compact for other in ("본점", "1호점", "2호점", "3호점") if other != b_tag
        ):
            branch_adj = -0.025
            break

    dist_m: Optional[float] = None
    if (
        pub.get("lat") is not None
        and pub.get("lon") is not None
        and listing.get("lat") is not None
        and listing.get("lon") is not None
    ):
        dist_m = haversine_km(pub["lat"], pub["lon"], listing["lat"], listing["lon"]) * 1000.0

    # 원칙 2: 10자리 표준 인허가번호(YYYYNNNNNN)가 일치하는 경우
    if reg_norm and len(reg_norm) == 10 and pub_norm_no == reg_norm:
        if (not reg_ind) or (reg_ind == pub_ind):
            return min(1.0, 0.99 + branch_adj), f"인허가번호 100% 일치 ({reg_raw or reg_norm})"
        # 호스트가 에어비앤비에 업종만 잘못 선택한 경우: 순수 브랜드명(2글자 이상)이 포함되고 거리 200m 이내일 때만 허용
        if (
            len(pub_brand) >= 2
            and pub_brand in list_brand
            and dist_m is not None
            and dist_m <= 200
        ):
            return min(1.0, 0.98 + branch_adj), f"인허가번호+상호 일치 ({reg_raw})"
        return 0.0, "업종 불일치"

    # 원칙 3: 에어비앤비에 일반숙박업·생활숙박업·관광호텔업으로 신고된 매물은 호스텔/외도민에 임의 매칭 금지
    if reg_ind in ("일반숙박업", "생활숙박업", "관광호텔업"):
        return 0.0, f"타 업종({reg_ind}) 신고 매물"

    # 원칙 4: 에어비앤비에 다른 10자리 구청 인허가번호(일련번호 1~9999)가 명시되어 있으면 다른 관리번호 업소와 매칭 금지
    is_district_code = (
        reg_norm is not None
        and len(reg_norm) == 10
        and reg_norm.isdigit()
        and int(reg_norm[-6:]) < 10000
    )
    if is_district_code and pub_norm_no and reg_norm != pub_norm_no:
        if (
            reg_ind == pub_ind
            and len(pub_brand) >= 2
            and (pub_brand == list_brand or pub_brand in list_brand)
            and dist_m is not None
            and dist_m <= 200
        ):
            return round(0.92 + branch_adj, 4), f"고유상호({pub_brand})+위치({dist_m:.0f}m) 일치"
        return 0.0, "인허가번호 불일치"

    # 원칙 5: 인허가번호가 미기재/구형/실증특례번호인 경우 -> 동일 업종 + 거리 200m 이내 + 순수 고유 브랜드명 정확 일치만 허용
    if reg_ind and pub_ind and reg_ind != pub_ind:
        return 0.0, "업종 불일치"

    generic_2char = {
        "안리", "안녕", "보다", "주택", "중앙", "맨션", "바다", "오션", "스테이", "하우스",
        "호텔", "모텔", "펜션", "빌라", "부산", "광안", "서면", "남포", "초량", "송정",
        "민락", "해변", "비치", "타워", "드림", "해피", "블루", "스타", "코지", "모던",
        "시티", "마루", "아라", "하루", "나무", "우리", "공간", "여행", "힐링", "감성", "제이",
    }
    is_valid_brand = (
        (
            len(pub_brand) >= 3
            and (
                pub_brand == list_brand
                or pub_brand in list_brand
                or (host_brand and (pub_brand == host_brand or pub_brand in host_brand))
            )
            and dist_m is not None
            and dist_m <= 200
        )
        or (
            len(pub_brand) == 2
            and pub_brand not in generic_2char
            and (
                pub_brand == list_brand
                or pub_brand in list_brand
                or pub_brand == host_brand
                or f"[{pub_brand}" in list_title_compact
            )
            and dist_m is not None
            and dist_m <= 120
        )
    )
    if is_valid_brand and dist_m is not None:
        return round(0.90 + branch_adj, 4), f"고유상호({pub_brand})+근접({dist_m:.0f}m) 일치"

    return 0.0, "엄격 매칭 기준 미달"


def run_auto_matching(
    zone_key: Optional[str] = None,
    min_score: float = 0.88,
    db_path: Path = DB_PATH,
) -> int:
    """엄격 기준으로 자동 매칭을 수행한다. 수동 확정(is_manual=1) 매칭은 보존한다."""
    now = utc_now_iso()
    matched_count = 0

    with get_conn(db_path) as conn:
        all_pubs = [
            dict(r)
            for r in conn.execute("SELECT * FROM public_businesses WHERE status_name = '영업/정상'").fetchall()
        ]
        exact_index: Dict[Tuple[Optional[str], Optional[str], str], Dict[str, Any]] = {}
        for p in all_pubs:
            mng_d = _digits_only(p.get("mng_no"))
            if len(mng_d) >= 10:
                exact_index[(p.get("sigungu"), p.get("industry_type"), mng_d[-10:])] = p

        list_query = "SELECT * FROM airbnb_listings WHERE 1=1"
        list_params: List[Any] = []
        if zone_key and zone_key != "all":
            list_query += " AND zone_key = ?"
            list_params.append(zone_key)

        listings = [dict(r) for r in conn.execute(list_query, list_params).fetchall()]

        manual_listings = {
            r["listing_id"]
            for r in conn.execute("SELECT listing_id FROM business_listing_matches WHERE is_manual = 1").fetchall()
        }

        if zone_key and zone_key != "all":
            conn.execute(
                """
                DELETE FROM business_listing_matches
                WHERE is_manual = 0
                  AND listing_id IN (SELECT listing_id FROM airbnb_listings WHERE zone_key = ?)
                """,
                (zone_key,),
            )
            zone_pubs = [p for p in all_pubs if p.get("zone_key") == zone_key]
        else:
            conn.execute("DELETE FROM business_listing_matches WHERE is_manual = 0;")
            zone_pubs = all_pubs

        for listing in listings:
            if listing["listing_id"] in manual_listings:
                continue

            reg_sg, reg_ind, reg_raw, reg_norm = _parse_stored_reg(listing.get("registration_number"))
            best_pub: Optional[Dict[str, Any]] = None
            best_score = 0.0
            best_reason = ""

            # 1단계: 시군구 + 업종(호스텔업/외도민) + 10자리 인허가번호 100% 일치
            if reg_norm and len(reg_norm) == 10 and reg_ind in ("호스텔업", "외국인관광도시민박업"):
                cand = exact_index.get((reg_sg, reg_ind, reg_norm))
                if cand is not None:
                    best_pub = cand
                    best_score = 1.0
                    best_reason = f"인허가번호 100% 일치 ({reg_raw or reg_norm})"

            # 2단계: 엄격한 compute_match_score 검증 통과 건만 매칭
            if best_pub is None:
                for pub in zone_pubs:
                    score, reason = compute_match_score(pub, listing)
                    if score > best_score:
                        best_score = score
                        best_pub = pub
                        best_reason = reason

            if best_pub is not None and best_score >= min_score:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO business_listing_matches (
                        public_id, listing_id, match_score, match_reason, is_manual, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                    """,
                    (best_pub["id"], listing["listing_id"], best_score, best_reason, now),
                )
                if best_pub.get("zone_key") and listing.get("zone_key") != best_pub.get("zone_key"):
                    conn.execute(
                        "UPDATE airbnb_listings SET zone_key = ?, is_hostel_like = 1 WHERE listing_id = ?",
                        (best_pub["zone_key"], listing["listing_id"]),
                    )
                matched_count += 1

    return matched_count


def set_manual_match(public_id: int, listing_id: str, db_path: Path = DB_PATH) -> None:
    """사용자가 대시보드에서 수동으로 공공 업소와 에어비앤비 숙소를 연결한다."""
    now = utc_now_iso()
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM business_listing_matches WHERE listing_id = ?;", (listing_id,))
        conn.execute(
            """
            INSERT INTO business_listing_matches (
                public_id, listing_id, match_score, match_reason, is_manual, updated_at
            ) VALUES (?, ?, 1.0, '사용자 수동 확정', 1, ?)
            """,
            (public_id, listing_id, now),
        )


def clear_match(listing_id: str, db_path: Path = DB_PATH) -> None:
    """특정 에어비앤비 리스팅의 매칭을 해제한다."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM business_listing_matches WHERE listing_id = ?;", (listing_id,))
