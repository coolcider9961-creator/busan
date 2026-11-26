"""Apify Airbnb Scraper 연동, 호스트 기본가 추출, 30일 달력 예약률 및 지정 URL 추적 모듈."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import html as html_lib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

from src.collectors.geo import assign_zone
from src.config import INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK, RAW_DIR, ZONES, get_env
from src.db.database import (
    DB_PATH,
    get_conn,
    insert_price_snapshot,
    insert_reviews,
    log_collection_run,
    upsert_airbnb_listing,
    utc_now_iso,
)

HOSTEL_KEYWORDS = (
    "hostel",
    "guesthouse",
    "guest house",
    "dorm",
    "dormitory",
    "bunk",
    "호스텔",
    "게스트하우스",
    "게하",
    "도미토리",
    "민박",
    "백패커",
    "쉐어",
)

REG_NO_PATTERN = re.compile(
    r"(?:영업신고번호|등록번호|허가번호|신고번호|License\s*number|Registration\s*number)\s*[:：#]?\s*([A-Za-z0-9가-힣\-_]{4,40})",
    re.IGNORECASE,
)

CALENDAR_ACTOR_ID = "cirkit/airbnb-availability-scraper"
URLS_ACTOR_ID = "tri_angle/airbnb-rooms-urls-scraper"


def compute_snapshot_windows(base_date: Optional[date] = None) -> Dict[str, Tuple[str, str]]:
    """고정 기준일(D+7 평일 1박, D+14 주말 1박) 체크인/체크아웃 날짜를 계산한다."""
    today = base_date or date.today()

    # D+7 이후 가장 가까운 화(1)·수(2)·목(3)
    weekday_start = today + timedelta(days=7)
    while weekday_start.weekday() not in (1, 2, 3):
        weekday_start += timedelta(days=1)
    weekday_end = weekday_start + timedelta(days=1)

    # D+14 이후 가장 가까운 토요일(5)
    weekend_start = today + timedelta(days=14)
    while weekend_start.weekday() != 5:
        weekend_start += timedelta(days=1)
    weekend_end = weekend_start + timedelta(days=1)

    return {
        "D+7_WEEKDAY": (weekday_start.isoformat(), weekday_end.isoformat()),
        "D+14_WEEKEND": (weekend_start.isoformat(), weekend_end.isoformat()),
    }


def _parse_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        # 할인 무시: originalPrice(할인 전 호스트 기본 설정가)를 최우선으로 읽는다
        for key in ("originalPrice", "price", "amount", "value", "total", "rate", "discountedPrice"):
            if key in value and value[key] is not None:
                parsed = _parse_money(value[key])
                if parsed is not None:
                    return parsed
        return None
    cleaned = re.sub(r"[^\d.]", "", str(value))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_korean_registration_details(text_blob: str) -> Dict[str, Optional[str]]:
    """에어비앤비 상세설명의 '등록 세부 정보(발급 지역 / 허가 유형 / 허가번호 / 사업자명)'를 파싱한다."""
    result: Dict[str, Optional[str]] = {
        "sigungu": None,
        "permit_type": None,
        "industry_type": None,
        "permit_no": None,
        "norm_code": None,
        "biz_name": None,
        "formatted": None,
    }
    if not text_blob:
        return result

    normalized = (
        text_blob.replace("\\u003cbr />", "\n")
        .replace("\u003cbr />", "\n")
        .replace("<br />", "\n")
        .replace("<br>", "\n")
        .replace("\\u003c/li>", "\n")
        .replace("</li>", "\n")
    )
    normalized = re.sub(r"\\u003c[^>]+>|<[^>]+>", " ", normalized)

    m_biz = re.search(r"사업자명\s*[:：]\s*([^\n\r\"\\]+)", normalized)
    if m_biz:
        result["biz_name"] = m_biz.group(1).strip()

    tail = normalized.split("등록 세부 정보")[-1] if "등록 세부 정보" in normalized else normalized
    m_reg = re.search(r"발급\s*지역\s*[:：]\s*(.+?)(?=허가\s*유형|\n|\r|$)", tail)
    m_type = re.search(r"허가\s*유형\s*[:：]\s*(.+?)(?=허가\s*번호|\n|\r|$)", tail)
    m_no = re.search(r"허가\s*번호\s*[:：]\s*([^\n\r\"\\]+)", tail)

    if m_reg:
        sg_m = re.search(r"([가-힣]+구|[가-힣]+군)", m_reg.group(1))
        result["sigungu"] = sg_m.group(1) if sg_m else m_reg.group(1).strip()

    if m_type:
        pt = m_type.group(1).strip()
        result["permit_type"] = pt
        if "호스텔" in pt or "관광숙박" in pt:
            result["industry_type"] = INDUSTRY_HOSTEL
        elif "외국인관광" in pt or "도시민박" in pt or "실증특례" in pt:
            result["industry_type"] = INDUSTRY_URBAN_MINBAK
        elif "일반숙박" in pt:
            result["industry_type"] = "일반숙박업"
        elif "생활숙박" in pt:
            result["industry_type"] = "생활숙박업"
        elif "관광호텔" in pt:
            result["industry_type"] = "관광호텔업"

    if m_no:
        pno = m_no.group(1).strip()
        pno = re.split(r"\s*\{", pno)[0].strip()
        result["permit_no"] = pno
        m_ys = re.search(r"(20\d{2})\s*[-~._\s제호]+\s*(\d{1,6})", pno)
        if not m_ys:
            m_ys = re.match(r"^\s*제?\s*(202[0-6])(\d{1,4})\s*호?\s*$", pno)
        if m_ys:
            yr_i = int(m_ys.group(1))
            seq_i = int(m_ys.group(2))
            # 구청 고유 일련번호는 통상 1~9999번대 (100000 이상은 위홈 등 실증특례 번호)
            if seq_i < 10000:
                result["norm_code"] = f"{yr_i:04d}{seq_i:06d}"
            else:
                result["norm_code"] = f" Sandbox:{yr_i}-{seq_i}".strip()
        else:
            digits = re.sub(r"\D", "", pno)
            if len(digits) >= 10:
                seq_tail = int(digits[-6:])
                if seq_tail < 10000:
                    result["norm_code"] = digits[-10:]

    if result["permit_no"] or result["permit_type"]:
        result["formatted"] = "|".join(
            [
                result["sigungu"] or "",
                result["industry_type"] or result["permit_type"] or "",
                result["permit_no"] or "",
                result["norm_code"] or "",
            ]
        )
        return result

    m_fallback = REG_NO_PATTERN.search(normalized)
    if m_fallback:
        raw_no = m_fallback.group(1).strip()
        digits = re.sub(r"\D", "", raw_no)
        result["permit_no"] = raw_no
        result["norm_code"] = digits[-10:] if len(digits) >= 10 else digits
        result["formatted"] = raw_no

    return result


def parse_extra_guest_fee_note(text_blob: str, capacity: Optional[int] = None) -> Optional[str]:
    """'최대 N인'을 맨 앞에 통일감 있게 고정하고, 상세설명에서 기준 인원 및 초과 1인당 추가 요금을 괄호로 병기한다."""
    cap_int = int(capacity) if (capacity is not None and int(capacity) > 0) else None
    if not text_blob:
        return f"최대 {cap_int}인" if cap_int else None

    base_n: Optional[int] = None
    for pat in (
        r"(?:기준|기본)\s*인원?\s*[:：]?\s*(\d{1,2})\s*(?:명|인)",
        r"(\d{1,2})\s*(?:명|인)\s*기준",
        r"기준\s*(\d{1,2})\s*(?:명|인)",
    ):
        m_b = re.search(pat, text_blob)
        if m_b:
            val = int(m_b.group(1))
            if 1 <= val <= 30 and (cap_int is None or val <= cap_int):
                base_n = val
                break

    fee_m = re.search(
        r"(?:추가\s*인원|1\s*(?:인|명)\s*(?:당\s*)?추가|\+1\s*(?:인|명)|인원\s*추가|기준\s*인원\s*초과)[^\n\r]{0,28}?(\d{1,2},\d{3}|\d+\s*만\s*\d*\s*천?\s*원|\d+\s*만\s*원)",
        text_blob,
    )
    fee_str: Optional[str] = None
    if fee_m:
        raw_fee = re.sub(r"\s+", "", fee_m.group(1).strip())
        if not raw_fee.endswith("원"):
            raw_fee += "원"
        fee_str = f"초과 +1인 {raw_fee}"

    extra_parts: List[str] = []
    if base_n is not None and (cap_int is None or base_n < cap_int or fee_str is not None):
        extra_parts.append(f"기준 {base_n}인")
    if fee_str:
        extra_parts.append(fee_str)

    if cap_int:
        if extra_parts:
            return f"최대 {cap_int}인 ({' · '.join(extra_parts)})"
        return f"최대 {cap_int}인"

    return " · ".join(extra_parts) if extra_parts else None


def _is_hostel_like_item(
    title: str,
    room_type: str,
    property_type: str,
    reg_info: Dict[str, Optional[str]],
) -> bool:
    """호스텔업·외국인관광도시민박업 허가 매물만 엄격히 선별한다 (생숙·일반모텔·관광호텔 완전 배제)."""
    ind = reg_info.get("industry_type")
    if ind in (INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK):
        return True
    if ind in ("일반숙박업", "생활숙박업", "관광호텔업"):
        return False

    rt = (room_type or "").lower()
    pt = (property_type or "").lower()
    t = (title or "").lower()

    if any(k in rt for k in ("shared", "hostel", "공용", "다인실")):
        return True
    if any(k in pt for k in HOSTEL_KEYWORDS):
        return True
    if any(k in t for k in HOSTEL_KEYWORDS):
        return True
    return False


def _detect_language_simple(text: str) -> str:
    """텍스트 스크립트 분포로 리뷰 언어를 빠르게 판별한다."""
    if not text:
        return "unknown"
    hangul = len(re.findall(r"[\uac00-\ud7a3]", text))
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    total = hangul + kana + cjk + latin
    if total == 0:
        return "other"
    if hangul >= max(kana, cjk, latin):
        return "ko"
    if kana > 0 and (kana + cjk) >= latin:
        return "ja"
    if cjk > latin:
        return "zh"
    if latin > 0:
        return "en"
    return "other"


def _extract_coordinates(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    coords = item.get("coordinates") or item.get("location")
    if not isinstance(coords, dict):
        coords = {}
    lat = item.get("lat") or item.get("latitude") or coords.get("latitude") or coords.get("lat")
    lon = item.get("lng") or item.get("lon") or item.get("longitude") or coords.get("longitude") or coords.get("lng")
    try:
        return (round(float(lat), 6), round(float(lon), 6)) if (lat is not None and lon is not None) else (None, None)
    except (TypeError, ValueError):
        return None, None


def _extract_prices(item: Dict[str, Any]) -> Tuple[Optional[float], float, float, Optional[float], str]:
    """일시적 특가/마감할인을 무시하고 호스트가 기본 설정한 금액(originalPrice 우선)을 추출한다."""
    price_obj = item.get("price") or item.get("pricing") or {}
    currency = "KRW"
    if isinstance(price_obj, dict) and price_obj.get("currency"):
        currency = str(price_obj["currency"]).upper()
    elif item.get("currency"):
        currency = str(item["currency"]).upper()

    nightly = None
    cleaning = 0.0
    service = 0.0
    total = None

    if isinstance(price_obj, dict):
        # 할인 전 호스트 기본 설정 요금(originalPrice) 최우선 추출
        orig_price = _parse_money(price_obj.get("originalPrice"))
        curr_price = _parse_money(
            price_obj.get("rate")
            or price_obj.get("nightly")
            or price_obj.get("pricePerNight")
            or price_obj.get("price")
            or price_obj.get("total")
            or price_obj.get("totalPrice")
        )

        bd_dict = price_obj.get("breakDown")
        bd_base = None
        if isinstance(bd_dict, dict):
            bd_base = _parse_money(bd_dict.get("basePrice"))
            clean_p = _parse_money(bd_dict.get("cleaningFee"))
            serv_p = _parse_money(bd_dict.get("serviceFee"))
            if clean_p is not None:
                cleaning = clean_p
            if serv_p is not None:
                service = serv_p

        breakdown = price_obj.get("priceItems") or price_obj.get("breakdown") or []
        if isinstance(breakdown, list):
            for entry in breakdown:
                if not isinstance(entry, dict):
                    continue
                label = str(entry.get("title") or entry.get("label") or entry.get("type") or "").lower()
                amt = _parse_money(entry.get("amount") or entry.get("price") or entry)
                if amt is None:
                    continue
                if "clean" in label or "청소" in label:
                    cleaning = amt
                elif "service" in label or "수수료" in label:
                    service = amt
                elif ("night" in label or "박" in label) and bd_base is None:
                    bd_base = amt

        # 다박(예: '2박 x ₩235,000' 또는 qualifier '· 2박')인 경우 1박 기준 기본가로 환산
        nights_div = 1
        qual_str = str(price_obj.get("qualifier") or price_obj.get("label") or "")
        if isinstance(bd_dict, dict) and isinstance(bd_dict.get("basePrice"), dict):
            qual_str += " " + str(bd_dict["basePrice"].get("description") or "")
        m_nights = re.search(r"(\d+)\s*박", qual_str)
        if m_nights and int(m_nights.group(1)) > 1:
            nights_div = int(m_nights.group(1))

        # 할인 전 정가(originalPrice 또는 breakDown.basePrice 중 큰 값)를 호스트 기본가로 채택
        candidates = [p for p in (orig_price, bd_base, curr_price) if p is not None and p > 0]
        if candidates:
            base_val = round(max(candidates) / nights_div, 0)
            nightly = base_val
            # 별도 청소비가 분리된 Actor 응답인 경우 합산, 한국 총액표시제(cleaning=0)는 그대로 유지
            explicit_total = _parse_money(price_obj.get("total") or price_obj.get("totalPrice"))
            if explicit_total and nights_div == 1 and explicit_total > base_val:
                total = explicit_total
            else:
                total = base_val + cleaning + service
    else:
        nightly = _parse_money(price_obj)
        total = nightly

    if nightly is None:
        nightly = _parse_money(item.get("nightlyPrice") or item.get("pricePerNight"))
    if total is None:
        total = _parse_money(item.get("totalPrice"))
    if total is None and nightly is not None:
        total = nightly + cleaning + service
    if nightly is None and total is not None:
        nightly = max(0.0, total - cleaning - service)

    return nightly, cleaning, service, total, currency


def _extract_rating_and_reviews_count(item: Dict[str, Any], parsed_reviews_len: int) -> Tuple[Optional[float], int]:
    """Actor별 rating(float 또는 dict) 및 reviewsCount를 안전하게 추출한다."""
    rating_raw = item.get("rating")
    rating: Optional[float] = None
    reviews_count = 0

    if isinstance(rating_raw, dict):
        r_val = rating_raw.get("guestSatisfaction") or rating_raw.get("overall") or rating_raw.get("value")
        if r_val is not None:
            try:
                rating = float(r_val)
            except (TypeError, ValueError):
                rating = None
        rc_val = rating_raw.get("reviewsCount") or rating_raw.get("count")
        if rc_val is not None:
            try:
                reviews_count = int(rc_val)
            except (TypeError, ValueError):
                reviews_count = 0
    elif rating_raw is not None:
        try:
            rating = float(rating_raw)
        except (TypeError, ValueError):
            rating = None

    if rating is None and item.get("stars") is not None:
        try:
            rating = float(item["stars"])
        except (TypeError, ValueError):
            pass

    if reviews_count == 0:
        try:
            reviews_count = int(item.get("reviewsCount") or item.get("numberOfReviews") or parsed_reviews_len or 0)
        except (TypeError, ValueError):
            reviews_count = parsed_reviews_len

    return rating, reviews_count


def _extract_room_counts(item: Dict[str, Any]) -> Tuple[Optional[int], Optional[float], Optional[float]]:
    """bedrooms, beds, bathrooms를 필드 또는 sharingConfigTitle/subDescription에서 추출한다."""
    bedrooms = item.get("bedrooms")
    beds = item.get("beds")
    bathrooms = item.get("bathrooms")

    text_sources = [str(item.get("sharingConfigTitle") or "")]
    sub_desc = item.get("subDescription")
    if isinstance(sub_desc, dict) and isinstance(sub_desc.get("items"), list):
        text_sources.extend(str(x) for x in sub_desc["items"])
    blob = " · ".join(text_sources)

    if bedrooms is None:
        m = re.search(r"침실\s*(\d+)", blob)
        if m:
            bedrooms = int(m.group(1))
    if beds is None:
        m = re.search(r"침대\s*(\d+)", blob)
        if m:
            beds = float(m.group(1))
    if bathrooms is None:
        m = re.search(r"욕실\s*(\d+(?:\.\d+)?)", blob)
        if m:
            bathrooms = float(m.group(1))

    return bedrooms, beds, bathrooms


def normalize_apify_item(
    item: Dict[str, Any],
    snapshot_type: str,
    check_in: str,
    check_out: str,
    snapshot_date: str,
    default_zone: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Apify Airbnb 결과 1건을 숙소·가격·리뷰 구조로 정규화한다."""
    if hasattr(item, "model_dump"):
        item = item.model_dump()
    elif not isinstance(item, dict):
        item = dict(item)

    raw_id = item.get("id") or item.get("listingId")
    url = str(item.get("url") or "").strip()
    if not raw_id and url:
        m = re.search(r"/rooms/(\d+)", url)
        raw_id = m.group(1) if m else None
    if not raw_id:
        return None

    listing_id = str(raw_id)
    if not url:
        url = f"https://www.airbnb.co.kr/rooms/{listing_id}"

    title = html_lib.unescape(str(item.get("title") or item.get("name") or f"Airbnb {listing_id}").strip())
    room_type = str(item.get("roomType") or item.get("room_type") or "").strip()
    property_type = str(item.get("propertyType") or item.get("property_type") or "").strip()

    text_blob = "\n".join(
        str(item.get(k) or "")
        for k in ("description", "subTitle", "subtitle", "registrationNumber", "license")
        if not isinstance(item.get(k), (dict, list))
    )
    reg_info = parse_korean_registration_details(text_blob)
    is_hostel_like = 1 if _is_hostel_like_item(title, room_type, property_type, reg_info) else 0

    host_obj = item.get("primaryHost") or item.get("host") or {}
    if not isinstance(host_obj, dict):
        host_obj = {}
    host_name = str(host_obj.get("name") or host_obj.get("firstName") or "").strip() or None
    is_superhost = 1 if (host_obj.get("isSuperhost") or host_obj.get("isSuperHost") or item.get("isSuperhost")) else 0

    lat, lon = _extract_coordinates(item)
    address_hint = str(
        reg_info.get("sigungu") or item.get("address") or item.get("locationSubtitle") or item.get("neighborhood") or ""
    ).strip()
    zone_key = assign_zone(lat=lat, lon=lon, address=address_hint) or default_zone

    raw_reviews = item.get("reviews") or []
    normalized_reviews: List[Dict[str, Any]] = []
    recent_dates: List[str] = []

    for idx, rev in enumerate(raw_reviews):
        if not isinstance(rev, dict):
            continue
        comment = str(rev.get("comments") or rev.get("comment") or rev.get("text") or "").strip()
        if not comment:
            continue
        r_date = str(rev.get("createdAt") or rev.get("date") or "")[:10] or None
        if r_date:
            recent_dates.append(r_date)
        rev_uid = str(rev.get("id") or f"{listing_id}_{r_date or 'na'}_{idx}_{hash(comment[:40])}")
        lang = str(rev.get("language") or "").strip().lower() or _detect_language_simple(comment)
        normalized_reviews.append(
            {
                "listing_id": listing_id,
                "review_uid": rev_uid,
                "reviewer_name": str(
                    (rev.get("reviewer") or {}).get("firstName") or rev.get("authorName") or ""
                ).strip() or None,
                "review_date": r_date,
                "language_code": lang,
                "rating": _parse_money(rev.get("rating")),
                "comment": comment,
            }
        )

    recent_review_date = max(recent_dates) if recent_dates else (
        str(item.get("lastReviewDate") or "")[:10] or None
    )

    rating, reviews_count = _extract_rating_and_reviews_count(item, len(normalized_reviews))
    bedrooms, beds, bathrooms = _extract_room_counts(item)
    capacity = item.get("personCapacity") or item.get("capacity")
    extra_guest_fee_note = parse_extra_guest_fee_note(text_blob, int(capacity) if capacity else None)

    nightly, cleaning, service, total, currency = _extract_prices(item)
    is_available = 1 if (item.get("isAvailable", True) and (total is not None or nightly is not None)) else 0
    operating_status = "에어비앤비 노출중 (30일 달력 미수집)" if is_available else "예약마감/비노출"

    actual_in = str(item.get("checkIn") or check_in)[:10]
    actual_out = str(item.get("checkOut") or check_out)[:10]

    thumb_url = str(item.get("thumbnail") or item.get("thumbnailUrl") or item.get("pictureUrl") or "").strip() or None
    if not thumb_url and isinstance(item.get("images"), list) and item["images"]:
        first_img = item["images"][0]
        if isinstance(first_img, str):
            thumb_url = first_img.strip() or None
        elif isinstance(first_img, dict):
            thumb_url = str(first_img.get("imageUrl") or first_img.get("url") or "").strip() or None

    listing_row = {
        "listing_id": listing_id,
        "title": title,
        "url": url,
        "room_type": room_type or None,
        "property_type": property_type or None,
        "is_hostel_like": is_hostel_like,
        "host_name": host_name,
        "is_superhost": is_superhost,
        "capacity": capacity,
        "bedrooms": bedrooms,
        "beds": beds,
        "bathrooms": bathrooms,
        "rating": rating,
        "reviews_count": reviews_count,
        "recent_review_date": recent_review_date,
        "operating_status": operating_status,
        "registration_number": reg_info.get("formatted"),
        "address_hint": address_hint or None,
        "zone_key": zone_key,
        "lat": lat,
        "lon": lon,
        "is_demo": 0,
        "extra_guest_fee_note": extra_guest_fee_note,
        "thumbnail_url": thumb_url,
    }

    snapshot_row = {
        "listing_id": listing_id,
        "snapshot_date": snapshot_date,
        "snapshot_type": snapshot_type,
        "check_in": actual_in,
        "check_out": actual_out,
        "currency": currency,
        "nightly_price": nightly,
        "cleaning_fee": cleaning,
        "service_fee": service,
        "total_price": total,
        "is_available": is_available,
    }

    return {
        "listing": listing_row,
        "snapshot": snapshot_row,
        "reviews": normalized_reviews,
    }


def purge_non_target_records(db_path: Path = DB_PATH) -> Dict[str, int]:
    """DB에서 5대 필수 권역 외 데이터와 생숙·일반모텔·관광호텔 매물을 영구 삭제한다."""
    valid_zones = tuple(ZONES.keys())
    placeholders = ",".join("?" for _ in valid_zones)
    with get_conn(db_path) as conn:
        c1 = conn.execute(
            f"DELETE FROM public_businesses WHERE zone_key IS NULL OR zone_key NOT IN ({placeholders})",
            valid_zones,
        ).rowcount
        c2 = conn.execute(
            f"""
            DELETE FROM airbnb_listings
            WHERE is_hostel_like = 0
               OR zone_key IS NULL
               OR zone_key NOT IN ({placeholders})
               OR registration_number LIKE '%|일반숙박업|%'
               OR registration_number LIKE '%|생활숙박업|%'
               OR registration_number LIKE '%|관광호텔업|%'
            """,
            valid_zones,
        ).rowcount
        # 공공 인허가상 '폐업' 확정된 10자리 인허가번호 전용 매물 제거
        closed_keys = {
            (r["sigungu"], r["industry_type"], re.sub(r"\D", "", str(r["mng_no"] or ""))[-10:])
            for r in conn.execute(
                "SELECT sigungu, industry_type, mng_no FROM public_businesses WHERE status_name != '영업/정상'"
            ).fetchall()
            if len(re.sub(r"\D", "", str(r["mng_no"] or ""))) >= 10
        }
        active_keys = {
            (r["sigungu"], r["industry_type"], re.sub(r"\D", "", str(r["mng_no"] or ""))[-10:])
            for r in conn.execute(
                "SELECT sigungu, industry_type, mng_no FROM public_businesses WHERE status_name = '영업/정상'"
            ).fetchall()
            if len(re.sub(r"\D", "", str(r["mng_no"] or ""))) >= 10
        }
        only_closed = closed_keys - active_keys
        if only_closed:
            for l_row in conn.execute("SELECT listing_id, registration_number FROM airbnb_listings").fetchall():
                parts = (str(l_row["registration_number"] or "").split("|") + ["", "", "", ""])[:4]
                if (parts[0], parts[1], parts[3]) in only_closed:
                    conn.execute("DELETE FROM airbnb_listings WHERE listing_id = ?", (l_row["listing_id"],))
                    c2 += 1
    return {"deleted_public": c1, "deleted_airbnb": c2}


def save_apify_items_to_db(
    items: List[Dict[str, Any]],
    snapshot_type: str,
    check_in: str,
    check_out: str,
    zone_key: Optional[str],
    hostel_only: bool = True,
    db_path: Path = DB_PATH,
) -> Dict[str, int]:
    """수집된 Apify 결과 리스트 중 5대 권역 내 호스텔업·외도민만 DB에 저장한다."""
    snapshot_date = date.today().isoformat()
    listings_saved = 0
    reviews_saved = 0
    skipped_entire = 0

    with get_conn(db_path) as conn:
        all_pubs = [
            dict(r)
            for r in conn.execute("SELECT * FROM public_businesses WHERE status_name = '영업/정상'").fetchall()
        ]
        for item in items:
            normalized = normalize_apify_item(
                item=item,
                snapshot_type=snapshot_type,
                check_in=check_in,
                check_out=check_out,
                snapshot_date=snapshot_date,
                default_zone=zone_key,
            )
            if not normalized:
                continue
            listing = normalized["listing"]
            text_blob = f"{item.get('title', '')} {item.get('description', '')}"
            reg_info = parse_korean_registration_details(text_blob)
            matched_pub = _match_public_business_by_brand_or_code(
                pubs=all_pubs,
                title=listing.get("title") or "",
                biz_name=reg_info.get("biz_name") or listing.get("host_name"),
                desc=text_blob,
                reg_info=reg_info,
                lat=listing.get("lat"),
                lon=listing.get("lon"),
            )
            if matched_pub:
                listing["is_hostel_like"] = 1
                listing["property_type"] = matched_pub["industry_type"]
                listing["zone_key"] = matched_pub["zone_key"] or listing.get("zone_key")
                mng_d = re.sub(r"\D", "", str(matched_pub.get("mng_no") or ""))
                p_code = mng_d[-10:] if len(mng_d) >= 10 else (reg_info.get("norm_code") or "")
                listing["registration_number"] = "|".join(
                    [
                        matched_pub.get("sigungu") or reg_info.get("sigungu") or "",
                        matched_pub["industry_type"],
                        reg_info.get("permit_no") or matched_pub["business_name"],
                        p_code,
                    ]
                )

            # 생숙/일반모텔 또는 5대 필수 권역 외 매물은 저장하지 않고 즉시 폐기
            if (hostel_only and listing["is_hostel_like"] == 0) or (listing.get("zone_key") not in ZONES):
                skipped_entire += 1
                continue

            upsert_airbnb_listing(conn, listing)
            if normalized["snapshot"].get("total_price") is not None:
                insert_price_snapshot(conn, normalized["snapshot"])
            reviews_saved += insert_reviews(conn, normalized["reviews"])
            listings_saved += 1

    return {
        "listings_saved": listings_saved,
        "reviews_saved": reviews_saved,
        "skipped_entire": skipped_entire,
    }


def apply_calendar_items_to_db(
    calendar_items: List[Dict[str, Any]],
    reference_date: Optional[date] = None,
    db_path: Path = DB_PATH,
) -> int:
    """cirkit/airbnb-availability-scraper의 30일 달력 결과를 파싱해 14일/30일 예약률을 갱신한다."""
    today = reference_date or date.today()
    d14_end = today + timedelta(days=14)
    d30_end = today + timedelta(days=30)
    now_iso = utc_now_iso()
    snap_date = today.isoformat()
    updated = 0

    with get_conn(db_path) as conn:
        for raw in calendar_items:
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump()
            elif not isinstance(raw, dict):
                raw = dict(raw)

            lid = str(raw.get("listingId") or raw.get("id") or "").strip()
            if not lid:
                continue
            days = raw.get("days") or raw.get("calendar") or []
            if not isinstance(days, list) or not days:
                continue

            total_14 = 0
            booked_14 = 0
            total_30 = 0
            booked_30 = 0
            compact_days: List[Dict[str, Any]] = []

            for d_entry in days:
                if not isinstance(d_entry, dict):
                    continue
                c_date_str = str(d_entry.get("calendarDate") or d_entry.get("date") or "")[:10]
                if not c_date_str:
                    continue
                try:
                    c_dt = date.fromisoformat(c_date_str)
                except ValueError:
                    continue

                if c_dt < today or c_dt >= d30_end:
                    continue

                avail = bool(d_entry.get("available"))
                total_30 += 1
                if not avail:
                    booked_30 += 1
                if c_dt < d14_end:
                    total_14 += 1
                    if not avail:
                        booked_14 += 1
                compact_days.append({"d": c_date_str, "a": 1 if avail else 0})

            if total_30 == 0:
                continue

            occ_14 = round((booked_14 / total_14) * 100.0, 1) if total_14 > 0 else 0.0
            occ_30 = round((booked_30 / total_30) * 100.0, 1)
            status_label = f"14일 {occ_14:.0f}%({booked_14}/{total_14}일) · 30일 {occ_30:.0f}%({booked_30}/{total_30}일)"

            cur = conn.execute(
                """
                UPDATE airbnb_listings
                SET occ_14d_pct = ?,
                    occ_30d_pct = ?,
                    booked_14d_days = ?,
                    booked_30d_days = ?,
                    operating_status = ?,
                    calendar_updated_at = ?
                WHERE listing_id = ?
                """,
                (occ_14, occ_30, booked_14, booked_30, status_label, now_iso, lid),
            )
            if cur.rowcount > 0:
                conn.execute(
                    """
                    INSERT INTO calendar_snapshots (
                        listing_id, snapshot_date, occ_14d_pct, occ_30d_pct,
                        booked_14d_days, booked_30d_days, raw_days_json, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(listing_id, snapshot_date) DO UPDATE SET
                        occ_14d_pct = excluded.occ_14d_pct,
                        occ_30d_pct = excluded.occ_30d_pct,
                        booked_14d_days = excluded.booked_14d_days,
                        booked_30d_days = excluded.booked_30d_days,
                        raw_days_json = excluded.raw_days_json,
                        collected_at = excluded.collected_at
                    """,
                    (
                        lid,
                        snap_date,
                        occ_14,
                        occ_30,
                        booked_14,
                        booked_30,
                        json.dumps(compact_days, ensure_ascii=False),
                        now_iso,
                    ),
                )
                updated += cur.rowcount

    return updated


def register_tracked_airbnb_url(
    airbnb_url: str,
    public_id: Optional[int] = None,
    zone_key: str = "seomyeon",
    title_hint: str = "",
    memo: str = "",
    db_path: Path = DB_PATH,
) -> str:
    """누락된 숙소의 에어비앤비 URL을 직접 등록해 공공상호와 고정 연결하고 추적 대상에 추가한다."""
    m = re.search(r"/rooms/(\d+)", airbnb_url)
    if not m:
        digits = re.sub(r"\D", "", airbnb_url)
        if len(digits) >= 6:
            listing_id = digits
        else:
            raise ValueError("유효한 에어비앤비 숙소 URL(예: https://www.airbnb.co.kr/rooms/12345678)을 입력해 주세요.")
    else:
        listing_id = m.group(1)

    clean_url = f"https://www.airbnb.co.kr/rooms/{listing_id}"
    now = utc_now_iso()

    with get_conn(db_path) as conn:
        pub_row = None
        if public_id is not None:
            pub_row = conn.execute("SELECT * FROM public_businesses WHERE id = ?", (public_id,)).fetchone()
            if pub_row and pub_row["zone_key"]:
                zone_key = pub_row["zone_key"]

        conn.execute(
            """
            INSERT INTO tracked_airbnb_urls (listing_id, url, public_id, zone_key, memo, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                url = excluded.url,
                public_id = COALESCE(excluded.public_id, tracked_airbnb_urls.public_id),
                zone_key = COALESCE(excluded.zone_key, tracked_airbnb_urls.zone_key),
                memo = COALESCE(excluded.memo, tracked_airbnb_urls.memo)
            """,
            (listing_id, clean_url, public_id, zone_key, memo, now),
        )

        existing = conn.execute("SELECT listing_id FROM airbnb_listings WHERE listing_id = ?", (listing_id,)).fetchone()
        if not existing:
            default_title = title_hint.strip() or (
                f"{pub_row['business_name']} (직접등록 URL)" if pub_row else f"직접등록 숙소 ({listing_id})"
            )
            upsert_airbnb_listing(
                conn,
                {
                    "listing_id": listing_id,
                    "title": default_title,
                    "url": clean_url,
                    "room_type": None,
                    "property_type": pub_row["industry_type"] if pub_row else INDUSTRY_HOSTEL,
                    "is_hostel_like": 1,
                    "host_name": None,
                    "is_superhost": 0,
                    "capacity": None,
                    "bedrooms": None,
                    "beds": None,
                    "bathrooms": None,
                    "rating": None,
                    "reviews_count": 0,
                    "recent_review_date": None,
                    "operating_status": "URL 등록됨 (다음 수집 시 가격·30일달력 자동 반영)",
                    "registration_number": f"{pub_row['sigungu'] if pub_row else ''}|{pub_row['industry_type'] if pub_row else ''}|직접등록|",
                    "address_hint": pub_row["road_address"] if pub_row else None,
                    "zone_key": zone_key,
                    "lat": pub_row["lat"] if pub_row else None,
                    "lon": pub_row["lon"] if pub_row else None,
                    "is_demo": 0,
                    "extra_guest_fee_note": None,
                },
            )

        if public_id is not None:
            conn.execute("DELETE FROM business_listing_matches WHERE listing_id = ?", (listing_id,))
            conn.execute(
                """
                INSERT INTO business_listing_matches (
                    public_id, listing_id, match_score, match_reason, is_manual, updated_at
                ) VALUES (?, ?, 1.0, '사용자 URL 직접 등록 확정', 1, ?)
                """,
                (public_id, listing_id, now),
            )

    # 등록 즉시 직접 수집 엔진으로 메타데이터 + 30일 달력 + 평일/주말 기본가를 동기화 시도
    try:
        fetch_single_room_direct(listing_id=listing_id, fetch_metadata=True, db_path=db_path)
    except Exception:
        pass

    return listing_id


AIRBNB_WEB_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"
AIRBNB_CALENDAR_HASH = "8f08e03c7bd16fcad3c92a3592c19a8b559a0d0855a84028d1163d4733ed9ade"
AIRBNB_BOOKIT_HASH = "88cfaaf83f882114af67dd726ac1d71f5b5594499b3ca5af263f406e6a18b615"


def _query_bookit_base_price(
    sess: requests.Session,
    headers: Dict[str, str],
    listing_id: str,
    check_in: str,
    check_out: str,
    nights: int,
) -> Optional[float]:
    """Airbnb StaysPdpBookItQuery에서 할인 전 호스트 기본 설정 1박 단가(originalPrice 우선)를 추출한다."""
    demand_id = base64.b64encode(f"DemandStayListing:{listing_id}".encode()).decode()
    b_vars = {
        "id": demand_id,
        "dateRange": {"startDate": check_in, "endDate": check_out},
        "guestCounts": {"numberOfAdults": 1},
        "includePdpMigrationBookItCalendarSheetFragment": True,
        "includePdpMigrationBookItFloatingFooterFragment": True,
        "includePdpMigrationBookItNavFragment": True,
        "includePdpMigrationBookItSidebarFragment": True,
        "includeGalleryBookItCtaFragment": True,
        "includeGalleryBookItCalendarSheetFragment": False,
        "includeOverviewMerchandisingTipsFragment": True,
        "includeStaysPdpPriceHeatmapFragment": False,
        "priceHeatmapDateRange": {"startDate": check_in, "endDate": check_out},
        "p3ImpressionId": "p3_1795490116_P3u-fHPzXHS2NUOx",
        "selectedCancellationPolicyId": None,
        "causeId": None,
        "selectedGuestOptionId": None,
        "selectedBookableOptionId": None,
        "federatedSearchId": None,
        "interactionType": None,
        "logEntityPricingViewEvent": True,
    }
    b_ext = {"persistedQuery": {"version": 1, "sha256Hash": AIRBNB_BOOKIT_HASH}}
    resp = sess.get(
        f"https://www.airbnb.co.kr/api/v3/StaysPdpBookItQuery/{AIRBNB_BOOKIT_HASH}",
        params={
            "operationName": "StaysPdpBookItQuery",
            "locale": "ko",
            "currency": "KRW",
            "variables": json.dumps(b_vars, separators=(",", ":")),
            "extensions": json.dumps(b_ext, separators=(",", ":")),
        },
        headers=headers,
        timeout=12,
    )
    if resp.status_code != 200:
        return None
    book_it = (
        resp.json()
        .get("data", {})
        .get("node", {})
        .get("pdpPresentation", {})
        .get("bookIt", {})
    )
    sdp = (book_it or {}).get("structuredDisplayPrice") or {}
    pline = sdp.get("primaryLine") or {}
    orig_s = pline.get("originalPrice") or pline.get("price") or pline.get("discountedPrice")
    val = _parse_money(orig_s)
    if val is not None and val > 0:
        return round(float(val) / max(1, nights), 0)
    return None


def _find_available_stay_window(
    day_map: Dict[str, Dict[str, Any]],
    ref_date: date,
    target_weekdays: Tuple[int, ...],
) -> Optional[Tuple[str, str, int]]:
    """향후 65일 달력에서 target_weekdays(평일: 0~3, 주말: 4~5)에 해당하는 예약 가능 구간을 찾는다."""
    for offset in range(1, 65):
        cand = ref_date + timedelta(days=offset)
        if cand.weekday() not in target_weekdays:
            continue
        cs = cand.isoformat()
        info = day_map.get(cs)
        if not info or not info.get("available") or info.get("availableForCheckin") is False:
            continue
        nights = max(1, min(int(info.get("minNights") or 1), 3))
        all_ok = True
        for n_off in range(nights):
            ns = (cand + timedelta(days=n_off)).isoformat()
            if not day_map.get(ns, {}).get("available"):
                all_ok = False
                break
        if all_ok:
            return cs, (cand + timedelta(days=nights)).isoformat(), nights
    return None


def _match_public_business_by_brand_or_code(
    pubs: List[Dict[str, Any]],
    title: str,
    biz_name: Optional[str],
    desc: str,
    reg_info: Dict[str, Optional[str]],
    lat: Optional[float],
    lon: Optional[float],
) -> Optional[Dict[str, Any]]:
    """공공 인허가(호스텔업·외도민) DB와 10자리 인허가코드 또는 고유상호+근접거리(300m)로 대조한다."""
    from src.collectors.geo import haversine_km
    from src.processing.matcher import extract_pure_brand

    norm_code = reg_info.get("norm_code")
    reg_sg = reg_info.get("sigungu")
    reg_ind = reg_info.get("industry_type")

    # 1) 10자리 표준 인허가번호 일치 확인
    if norm_code and len(norm_code) == 10:
        for p in pubs:
            mng_d = re.sub(r"\D", "", str(p.get("mng_no") or ""))
            p_code = mng_d[-10:] if len(mng_d) >= 10 else ""
            if p_code == norm_code:
                if reg_sg and p.get("sigungu") and reg_sg != p.get("sigungu"):
                    continue
                if reg_ind in (INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK) and reg_ind == p.get("industry_type"):
                    return p

    # 2) 상호명(전체 상호 또는 순수 고유 브랜드명) + 근접 거리 + 지점명(본점/1호점/2호점) 대조
    title_compact = re.sub(r"\s+", "", (title or "").lower())
    biz_compact = re.sub(r"\s+", "", (biz_name or "").lower())
    desc_compact = re.sub(r"\s+", "", (desc or "").lower())
    list_brand = extract_pure_brand(title or "")
    biz_brand = extract_pure_brand(biz_name or "")
    generic_2char = {
        "안리", "안녕", "보다", "주택", "중앙", "맨션", "바다", "오션", "스테이", "하우스",
        "호텔", "모텔", "펜션", "빌라", "부산", "광안", "서면", "남포", "초량", "송정",
        "민락", "해변", "비치", "타워", "드림", "해피", "블루", "스타", "코지", "모던",
        "시티", "마루", "아라", "하루", "나무", "우리", "공간", "여행", "힐링", "감성", "제이",
    }

    best_pub = None
    best_rank = -1.0
    for p in pubs:
        p_name_raw = str(p.get("business_name") or "").strip()
        p_compact = re.sub(r"\s+", "", p_name_raw.lower())
        p_first_token = re.sub(r"\s+", "", p_name_raw.split()[0].lower()) if p_name_raw else ""
        p_brand = extract_pure_brand(p_name_raw)
        if not p_brand:
            continue

        dist_m = 999999.0
        if lat is not None and lon is not None and p.get("lat") is not None and p.get("lon") is not None:
            dist_m = haversine_km(lat, lon, float(p["lat"]), float(p["lon"])) * 1000.0

        if dist_m > 280.0:
            continue

        branch_bonus = 0.0
        for b_tag in ("본점", "1호점", "2호점", "3호점"):
            if b_tag in p_compact and b_tag in title_compact:
                branch_bonus = 50.0
                break
            elif b_tag in p_compact and any(
                other in title_compact for other in ("본점", "1호점", "2호점", "3호점") if other != b_tag
            ):
                branch_bonus = -50.0
                break

        mng_d = re.sub(r"\D", "", str(p.get("mng_no") or ""))
        p_code = mng_d[-10:] if len(mng_d) >= 10 else ""
        same_code = bool(norm_code and len(norm_code) == 10 and p_code == norm_code)

        # 동일 시군구 + 10자리 인허가 일련번호 일치 + 초근접(30m 이내) + 고유 브랜드명이 제목/설명에 포함된 경우
        if same_code and dist_m <= 30.0 and len(p_brand) >= 2 and (
            p_brand in list_brand or p_brand in desc_compact
        ):
            rank = 350.0 - dist_m + branch_bonus
            if rank > best_rank:
                best_rank = rank
                best_pub = p
            continue

        # 1) 제목 또는 사업자명(biz_name)에 전체 상호(4글자 이상)가 명시된 경우
        is_non_generic_brand = len(p_brand) >= 3 or (len(p_brand) == 2 and p_brand not in generic_2char)
        if len(p_compact) >= 4 and (p_compact in title_compact or (is_non_generic_brand and p_compact in biz_compact)):
            rank = 300.0 - dist_m + branch_bonus
            if rank > best_rank:
                best_rank = rank
                best_pub = p
            continue

        # 2) 상세설명(desc) 본문에 고유 상호가 등장하는 경우 (일반 명사 브랜드 남천바다·보다스테이 등은 배제)
        if (
            len(p_compact) >= 4
            and is_non_generic_brand
            and (
                (p_compact in desc_compact and dist_m <= 120.0)
                or (len(p_first_token) >= 5 and p_first_token in desc_compact and dist_m <= 80.0)
            )
        ):
            rank = 280.0 - dist_m + branch_bonus
            if rank > best_rank:
                best_rank = rank
                best_pub = p
            continue

        # 3) 순수 브랜드명(3자 이상, 또는 고유 2자 브랜드+초근접 80m 이내)이 제목/호스트 브랜드에 일치하는 경우
        brand_match = (
            (len(p_brand) >= 3 and (p_brand == list_brand or p_brand == biz_brand or p_brand in list_brand))
            or (
                len(p_brand) == 2
                and p_brand not in generic_2char
                and (
                    p_brand == list_brand
                    or p_brand == biz_brand
                    or f"[{p_brand}" in title_compact
                    or (same_code and p_brand in list_brand)
                )
                and dist_m <= 80.0
            )
        )
        if brand_match and dist_m <= 160.0:
            rank = 200.0 - dist_m + branch_bonus
            if rank > best_rank:
                best_rank = rank
                best_pub = p

    return best_pub


def fetch_single_room_direct(
    listing_id: str,
    reference_date: Optional[date] = None,
    fetch_metadata: bool = False,
    strict_filter: bool = False,
    search_title_hint: str = "",
    public_pubs_cache: Optional[List[Dict[str, Any]]] = None,
    fetch_weekend_price: bool = True,
    fetch_weekday_price: bool = True,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """Apify 크레딧 소모 없이($0) 단일 숙소의 메타데이터, 30일 달력 예약률, 평일·주말 기본가를 직접 수집한다."""
    ref = reference_date or date.today()
    html_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    headers = {
        **html_headers,
        "X-Airbnb-API-Key": AIRBNB_WEB_API_KEY,
        "Content-Type": "application/json",
    }
    sess = requests.Session()
    result: Dict[str, Any] = {
        "listing_id": str(listing_id),
        "saved_listing": False,
        "calendar_updated": False,
        "weekend_price": None,
        "weekday_price": None,
    }

    if fetch_metadata:
        try:
            page_resp = sess.get(f"https://www.airbnb.co.kr/rooms/{listing_id}", headers=html_headers, timeout=12)
            if page_resp.status_code != 200:
                if strict_filter:
                    return result
            else:
                html = page_resp.text
                m_html_title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
                html_title = ""
                if m_html_title:
                    raw_ht = re.sub(r"\s+", " ", m_html_title.group(1)).strip()
                    if " - " in raw_ht:
                        html_title = raw_ht.split(" - ")[0].strip()

                scripts = re.findall(r'<script[^>]*id="data-deferred-state-0"[^>]*>(.*?)</script>', html, re.S)
                if scripts:
                    d = json.loads(scripts[0])
                    pdp = (
                        d.get("niobeClientData", [[None, {}]])[0][1]
                        .get("data", {})
                        .get("presentation", {})
                        .get("stayProductDetailPage", {})
                        .get("sections", {})
                    )
                    meta = pdp.get("metadata", {})
                    sharing = meta.get("sharingConfig", {})
                    ev = meta.get("loggingContext", {}).get("eventDataLogging", {})
                    desc_texts: List[str] = []
                    for sec in pdp.get("sections", []):
                        if sec.get("sectionId") in ("DESCRIPTION_MODAL", "DESCRIPTION_DEFAULT", "POLICIES_DEFAULT"):
                            desc_texts.append(json.dumps(sec.get("section"), ensure_ascii=False))
                    full_desc = " ".join(desc_texts)
                    reg_info = parse_korean_registration_details(full_desc)
                    cap = ev.get("personCapacity") or sharing.get("personCapacity")
                    cap_int = int(cap) if cap else None
                    extra_fee = parse_extra_guest_fee_note(full_desc, capacity=cap_int)

                    best_title = (
                        html_title
                        or search_title_hint.strip()
                        or str(sharing.get("title") or meta.get("pageTitle") or "").split("·")[0].strip()
                        or f"숙소 {listing_id}"
                    )
                    rating = sharing.get("starRating") or ev.get("guestSatisfactionOverall")
                    rev_cnt = sharing.get("reviewCount") or ev.get("visibleReviewCount")
                    lat = float(ev["listingLat"]) if ev.get("listingLat") is not None else None
                    lon = float(ev["listingLng"]) if ev.get("listingLng") is not None else None
                    room_type = ev.get("roomType") or sharing.get("propertyType")
                    is_super = 1 if ev.get("isSuperhost") else 0
                    zone_key = assign_zone(lat, lon)

                    m_og_img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
                    thumb_direct = (
                        str(sharing.get("imageUrl") or sharing.get("pictureUrl") or (m_og_img.group(1) if m_og_img else "") or "").strip()
                        or None
                    )
                    rating_f = float(rating) if rating else None
                    rev_cnt_i = int(rev_cnt) if rev_cnt else None

                    if not strict_filter:
                        with get_conn(db_path) as conn:
                            cur = conn.execute(
                                """
                                UPDATE airbnb_listings
                                SET title = COALESCE(?, title),
                                    rating = COALESCE(?, rating),
                                    reviews_count = CASE WHEN ? IS NOT NULL AND ? >= 0 THEN ? ELSE reviews_count END,
                                    is_superhost = COALESCE(?, is_superhost),
                                    capacity = COALESCE(?, capacity),
                                    extra_guest_fee_note = COALESCE(?, extra_guest_fee_note),
                                    thumbnail_url = COALESCE(?, thumbnail_url),
                                    last_seen_at = ?
                                WHERE listing_id = ?
                                """,
                                (
                                    best_title if best_title and not best_title.startswith("숙소 ") else None,
                                    rating_f,
                                    rev_cnt_i,
                                    rev_cnt_i,
                                    rev_cnt_i,
                                    is_super,
                                    cap_int,
                                    extra_fee,
                                    thumb_direct,
                                    utc_now_iso(),
                                    str(listing_id),
                                ),
                            )
                            if cur.rowcount > 0:
                                result["saved_listing"] = True
                                result["rating"] = rating_f
                                result["reviews_count"] = rev_cnt_i

                    if not result["saved_listing"]:
                        pubs = public_pubs_cache
                        if pubs is None:
                            with get_conn(db_path) as conn:
                                pubs = [
                                    dict(r)
                                    for r in conn.execute(
                                        "SELECT * FROM public_businesses WHERE status_name = '영업/정상'"
                                    ).fetchall()
                                ]

                        matched_pub = _match_public_business_by_brand_or_code(
                            pubs=pubs,
                            title=best_title,
                            biz_name=reg_info.get("biz_name"),
                            desc=full_desc,
                            reg_info=reg_info,
                            lat=lat,
                            lon=lon,
                        )

                        ind_type = reg_info.get("industry_type")
                        reg_formatted = reg_info.get("formatted")

                        if matched_pub:
                            ind_type = matched_pub["industry_type"]
                            zone_key = matched_pub["zone_key"] or zone_key
                            mng_d = re.sub(r"\D", "", str(matched_pub.get("mng_no") or ""))
                            p_code = mng_d[-10:] if len(mng_d) >= 10 else (reg_info.get("norm_code") or "")
                            reg_formatted = "|".join(
                                [
                                    matched_pub.get("sigungu") or reg_info.get("sigungu") or "",
                                    ind_type,
                                    reg_info.get("permit_no") or matched_pub["business_name"],
                                    p_code,
                                ]
                            )

                        if strict_filter:
                            if zone_key not in ZONES:
                                return result
                            if ind_type not in (INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK):
                                return result

                        final_ind = ind_type if ind_type in (INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK) else INDUSTRY_URBAN_MINBAK
                        with get_conn(db_path) as conn:
                            upsert_airbnb_listing(
                                conn,
                                {
                                    "listing_id": str(listing_id),
                                    "title": best_title,
                                    "url": f"https://www.airbnb.co.kr/rooms/{listing_id}",
                                    "room_type": str(room_type) if room_type else None,
                                    "property_type": final_ind,
                                    "is_hostel_like": 1,
                                    "host_name": reg_info.get("biz_name"),
                                    "is_superhost": is_super,
                                    "capacity": cap_int,
                                    "bedrooms": None,
                                    "beds": None,
                                    "bathrooms": None,
                                    "rating": rating_f,
                                    "reviews_count": rev_cnt_i or 0,
                                    "recent_review_date": None,
                                    "operating_status": "예약 가능 (활성)",
                                    "registration_number": reg_formatted,
                                    "address_hint": matched_pub["road_address"] if matched_pub else None,
                                    "zone_key": zone_key or "gwangalli",
                                    "lat": lat,
                                    "lon": lon,
                                    "is_demo": 0,
                                    "extra_guest_fee_note": extra_fee,
                                    "thumbnail_url": thumb_direct,
                                },
                            )
                            result["saved_listing"] = True
                            result["rating"] = rating_f
                            result["reviews_count"] = rev_cnt_i
        except Exception:
            if strict_filter:
                return result

    # 1) 30일 달력 가용성 조회 (PdpAvailabilityCalendar)
    cal_vars = {"request": {"count": 3, "listingId": str(listing_id), "month": ref.month, "year": ref.year}}
    cal_ext = {"persistedQuery": {"version": 1, "sha256Hash": AIRBNB_CALENDAR_HASH}}
    cr = sess.get(
        f"https://www.airbnb.co.kr/api/v3/PdpAvailabilityCalendar/{AIRBNB_CALENDAR_HASH}",
        params={
            "operationName": "PdpAvailabilityCalendar",
            "locale": "ko",
            "currency": "KRW",
            "variables": json.dumps(cal_vars, separators=(",", ":")),
            "extensions": json.dumps(cal_ext, separators=(",", ":")),
        },
        headers=headers,
        timeout=12,
    )
    if cr.status_code != 200:
        return result

    months = cr.json().get("data", {}).get("merlin", {}).get("pdpAvailabilityCalendar", {}).get("calendarMonths", [])
    day_map: Dict[str, Dict[str, Any]] = {}
    flat_days: List[Dict[str, Any]] = []
    for m in months:
        for d in m.get("days", []):
            dt_str = d.get("calendarDate")
            if dt_str:
                avail = bool(d.get("available"))
                day_map[dt_str] = {
                    "available": avail,
                    "minNights": int(d.get("minNights") or 1),
                    "availableForCheckin": d.get("availableForCheckin", True),
                }
                flat_days.append({"date": dt_str, "available": avail})

    if flat_days:
        n_up = apply_calendar_items_to_db(
            [{"listingId": str(listing_id), "days": flat_days}],
            reference_date=ref,
            db_path=db_path,
        )
        result["calendar_updated"] = n_up > 0

    # 2) 주말 기본 객단가(D+14_WEEKEND) 및 평일 기본 객단가(D+7_WEEKDAY) 추출
    snap_date = ref.isoformat()
    if fetch_weekend_price and day_map:
        we_win = _find_available_stay_window(day_map, ref, (4, 5))
        if we_win:
            ci, co, nights = we_win
            we_price = _query_bookit_base_price(sess, headers, str(listing_id), ci, co, nights)
            if we_price:
                result["weekend_price"] = we_price
                with get_conn(db_path) as conn:
                    insert_price_snapshot(
                        conn,
                        {
                            "listing_id": str(listing_id),
                            "snapshot_date": snap_date,
                            "snapshot_type": "D+14_WEEKEND",
                            "check_in": ci,
                            "check_out": co,
                            "nightly_price": we_price,
                            "cleaning_fee": 0.0,
                            "service_fee": 0.0,
                            "total_price": we_price,
                            "currency": "KRW",
                            "is_available": 1,
                        },
                    )

    if fetch_weekday_price and day_map:
        wd_win = _find_available_stay_window(day_map, ref, (0, 1, 2, 3))
        if wd_win:
            ci, co, nights = wd_win
            wd_price = _query_bookit_base_price(sess, headers, str(listing_id), ci, co, nights)
            if wd_price:
                result["weekday_price"] = wd_price
                with get_conn(db_path) as conn:
                    insert_price_snapshot(
                        conn,
                        {
                            "listing_id": str(listing_id),
                            "snapshot_date": snap_date,
                            "snapshot_type": "D+7_WEEKDAY",
                            "check_in": ci,
                            "check_out": co,
                            "nightly_price": wd_price,
                            "cleaning_fee": 0.0,
                            "service_fee": 0.0,
                            "total_price": wd_price,
                            "currency": "KRW",
                            "is_available": 1,
                        },
                    )

    return result


def sync_calendar_and_prices_direct(
    zone_key: str = "all",
    max_items: int = 1500,
    fetch_metadata: bool = True,
    fetch_weekend_price: bool = True,
    fetch_weekday_if_missing: bool = True,
    fetch_all_weekday_prices: bool = False,
    matched_only: bool = False,
    max_workers: int = 14,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """확정된 순수 호스텔업·외도민 숙소들의 최신 평점·후기수, 향후 30일 달력 예약률, 주말/평일 호스트 기본가를 직접 수집한다."""
    started = utc_now_iso()
    today = date.today()
    matched_clause = (
        " AND EXISTS (SELECT 1 FROM business_listing_matches m WHERE m.listing_id = a.listing_id)"
        if matched_only
        else ""
    )
    with get_conn(db_path) as conn:
        if zone_key and zone_key != "all":
            rows = conn.execute(
                f"""
                SELECT a.listing_id,
                       (SELECT COUNT(*) FROM price_snapshots p WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+7_WEEKDAY') AS has_wd,
                       (SELECT COUNT(*) FROM price_snapshots p WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+14_WEEKEND') AS has_we
                FROM airbnb_listings a
                WHERE a.is_hostel_like = 1 AND a.zone_key = ?{matched_clause}
                ORDER BY (CASE WHEN EXISTS (SELECT 1 FROM business_listing_matches m WHERE m.listing_id = a.listing_id) THEN 0 ELSE 1 END),
                         a.reviews_count DESC
                LIMIT ?
                """,
                (zone_key, max_items),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT a.listing_id,
                       (SELECT COUNT(*) FROM price_snapshots p WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+7_WEEKDAY') AS has_wd,
                       (SELECT COUNT(*) FROM price_snapshots p WHERE p.listing_id = a.listing_id AND p.snapshot_type = 'D+14_WEEKEND') AS has_we
                FROM airbnb_listings a
                WHERE a.is_hostel_like = 1{matched_clause}
                ORDER BY (CASE WHEN EXISTS (SELECT 1 FROM business_listing_matches m WHERE m.listing_id = a.listing_id) THEN 0 ELSE 1 END),
                         a.reviews_count DESC
                LIMIT ?
                """,
                (max_items,),
            ).fetchall()

    tasks = [
        (
            str(r["listing_id"]),
            bool(fetch_weekend_price),
            bool(fetch_all_weekday_prices or (fetch_weekday_if_missing and int(r["has_wd"] or 0) == 0)),
        )
        for r in rows
    ]
    if not tasks:
        return {"requested": 0, "meta_updated": 0, "updated": 0, "weekend_prices": 0}

    meta_updated = 0
    cal_updated = 0
    we_updated = 0

    def _worker(args: Tuple[str, bool, bool]) -> Dict[str, Any]:
        lid, do_we, do_wd = args
        try:
            return fetch_single_room_direct(
                listing_id=lid,
                reference_date=today,
                fetch_metadata=fetch_metadata,
                fetch_weekend_price=do_we,
                fetch_weekday_price=do_wd,
                db_path=db_path,
            )
        except Exception:
            return {"listing_id": lid, "saved_listing": False, "calendar_updated": False, "weekend_price": None}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_worker, t) for t in tasks]
        for fut in as_completed(futures):
            res = fut.result()
            if res.get("saved_listing"):
                meta_updated += 1
            if res.get("calendar_updated"):
                cal_updated += 1
            if res.get("weekend_price") is not None:
                we_updated += 1

    with get_conn(db_path) as conn:
        log_collection_run(
            conn,
            run_type="airbnb_calendar_30d",
            zone_key=zone_key,
            status="success",
            records_count=max(meta_updated, cal_updated),
            message=(
                f"평점·후기수 {meta_updated}건 · 30일 달력 예약률 {cal_updated}건 · 주말 기본가 {we_updated}건 갱신 완료"
            ),
            started_at=started,
        )
    return {
        "requested": len(tasks),
        "meta_updated": meta_updated,
        "updated": cal_updated,
        "weekend_prices": we_updated,
    }


def _scan_map_grid_cell(
    lat: float,
    lon: float,
    delta: float = 0.0020,
    max_pages: int = 3,
) -> Dict[str, str]:
    """단일 지도 격자(날짜 미지정)에서 노출되는 모든 에어비앤비 후보 (listing_id -> title)를 수집한다."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    sess = requests.Session()
    base_params = {
        "refinement_paths[]": "/homes",
        "search_mode": "regular_search",
        "price_filter_input_type": "2",
        "channel": "EXPLORE",
        "ne_lat": f"{lat + delta:.6f}",
        "ne_lng": f"{lon + delta:.6f}",
        "sw_lat": f"{lat - delta:.6f}",
        "sw_lng": f"{lon - delta:.6f}",
        "zoom": "17",
        "zoom_level": "17",
        "search_by_map": "true",
        "search_type": "user_map_move",
    }
    discovered: Dict[str, str] = {}
    try:
        r = sess.get("https://www.airbnb.co.kr/s/부산/homes", params=base_params, headers=headers, timeout=14)
        if r.status_code != 200:
            return discovered
        scripts = re.findall(r'<script[^>]*id="data-deferred-state-0"[^>]*>(.*?)</script>', r.text, re.S)
        if not scripts:
            return discovered
        d = json.loads(scripts[0])
        stays = d.get("niobeClientData", [[None, {}]])[0][1].get("data", {}).get("presentation", {}).get("staysSearch", {}).get("results", {})
        cursors = stays.get("paginationInfo", {}).get("pageCursors", []) or [""]
        for idx, cur in enumerate(cursors[:max_pages]):
            if idx > 0:
                p = dict(base_params)
                p["cursor"] = cur
                r = sess.get("https://www.airbnb.co.kr/s/부산/homes", params=p, headers=headers, timeout=14)
                scripts = re.findall(r'<script[^>]*id="data-deferred-state-0"[^>]*>(.*?)</script>', r.text, re.S)
                if not scripts:
                    continue
                d = json.loads(scripts[0])
                stays = d.get("niobeClientData", [[None, {}]])[0][1].get("data", {}).get("presentation", {}).get("staysSearch", {}).get("results", {})
            for sr in stays.get("searchResults", []) or []:
                dsl = sr.get("demandStayListing") or {}
                b64_id = dsl.get("id") or ""
                try:
                    lid = base64.b64decode(b64_id).decode("utf-8", errors="ignore").split(":")[-1]
                except Exception:
                    continue
                if not lid or not lid.isdigit():
                    continue
                name_loc = (sr.get("nameLocalized") or {}).get("localizedStringWithTranslationPreference") or sr.get("subtitle") or sr.get("title") or ""
                discovered[lid] = str(name_loc).strip()
    except Exception:
        pass
    return discovered


def discover_zone_listings_direct(
    zone_key: str = "all",
    max_cells: int = 15,
    pages_per_cell: int = 2,
    max_workers: int = 4,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """Apify 크레딧 없이($0) 공공 인허가 밀집 격자를 스캔하여 호스텔업·외도민 숙소를 발굴하고 30일 달력·기본가를 수집한다."""
    started = utc_now_iso()
    today = date.today()
    with get_conn(db_path) as conn:
        all_pubs = [
            dict(r)
            for r in conn.execute("SELECT * FROM public_businesses WHERE status_name = '영업/정상'").fetchall()
        ]
        existing_ids = {
            str(r["listing_id"])
            for r in conn.execute("SELECT listing_id FROM airbnb_listings").fetchall()
        }

    zone_pubs = [
        p
        for p in all_pubs
        if (zone_key == "all" or p.get("zone_key") == zone_key)
        and p.get("lat") is not None
        and p.get("lon") is not None
    ]

    # 약 320m 단위 격자 클러스터 생성 (공공 업소 밀집 순 정렬)
    cell_counts: Dict[Tuple[float, float], int] = {}
    grid_step = 0.0032
    for p in zone_pubs:
        clat = round(round(float(p["lat"]) / grid_step) * grid_step, 5)
        clon = round(round(float(p["lon"]) / grid_step) * grid_step, 5)
        cell_counts[(clat, clon)] = cell_counts.get((clat, clon), 0) + 1

    sorted_cells = [cell for cell, _ in sorted(cell_counts.items(), key=lambda x: x[1], reverse=True)[:max_cells]]

    candidates: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(10, max_workers)) as pool:
        futs = [pool.submit(_scan_map_grid_cell, clat, clon, 0.0020, pages_per_cell) for clat, clon in sorted_cells]
        for fut in as_completed(futs):
            for lid, title in fut.result().items():
                if lid not in existing_ids and lid not in candidates:
                    candidates[lid] = title

    saved_count = 0
    cal_count = 0
    we_count = 0

    def _verify_and_save(item: Tuple[str, str]) -> Dict[str, Any]:
        lid, t_hint = item
        return fetch_single_room_direct(
            listing_id=lid,
            reference_date=today,
            fetch_metadata=True,
            strict_filter=True,
            search_title_hint=t_hint,
            public_pubs_cache=all_pubs,
            fetch_weekend_price=True,
            fetch_weekday_price=True,
            db_path=db_path,
        )

    if candidates:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_verify_and_save, item) for item in candidates.items()]
            for fut in as_completed(futs):
                res = fut.result()
                if res.get("saved_listing"):
                    saved_count += 1
                if res.get("calendar_updated"):
                    cal_count += 1
                if res.get("weekend_price") is not None:
                    we_count += 1

    with get_conn(db_path) as conn:
        log_collection_run(
            conn,
            run_type="airbnb_direct_discovery",
            zone_key=zone_key,
            status="success",
            records_count=saved_count,
            message=f"격자 {len(sorted_cells)}곳 스캔 (후보 {len(candidates)}건 중 순수 호스텔·외도민 {saved_count}건 신규 반영)",
            started_at=started,
        )

    return {
        "cells_scanned": len(sorted_cells),
        "candidates_checked": len(candidates),
        "saved_hostel_minbak": saved_count,
        "calendar_updated": cal_count,
        "weekend_prices": we_count,
    }


def _extract_run_dataset_id(run: Any) -> str:
    """apify_client v1~v3의 Run 객체 또는 dict에서 default_dataset_id를 추출한다."""
    dataset_id = getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None)
    if not dataset_id and isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    if not dataset_id:
        raise ValueError(f"Apify 실행 결과에서 dataset ID를 찾을 수 없습니다: {run}")
    return str(dataset_id)


def sync_calendar_30d_via_apify(
    zone_key: str = "seomyeon",
    max_items: int = 300,
    api_token: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """Apify 공식 Actor(cirkit/airbnb-availability-scraper)로 순수 호스텔·외도민의 향후 30일 달력 예약률을 수집한다."""
    from apify_client import ApifyClient

    token = (api_token or get_env("APIFY_API_TOKEN")).strip()
    if not token:
        raise ValueError("APIFY_API_TOKEN이 설정되어 있지 않습니다.")

    with get_conn(db_path) as conn:
        if zone_key and zone_key != "all":
            rows = conn.execute(
                "SELECT listing_id FROM airbnb_listings WHERE is_hostel_like = 1 AND zone_key = ? ORDER BY reviews_count DESC LIMIT ?",
                (zone_key, max_items),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT listing_id FROM airbnb_listings WHERE is_hostel_like = 1 ORDER BY reviews_count DESC LIMIT ?",
                (max_items,),
            ).fetchall()

    listing_ids = [str(r["listing_id"]) for r in rows]
    if not listing_ids:
        return {"requested": 0, "updated": 0}

    started = utc_now_iso()
    today = date.today()
    client = ApifyClient(token)
    run_input = {
        "listingIds": listing_ids,
        "months": 2,
        "startMonth": today.month,
        "startYear": today.year,
        "currency": "KRW",
        "locale": "ko-KR",
        "maxItems": len(listing_ids),
    }
    run = client.actor(CALENDAR_ACTOR_ID).call(run_input=run_input)
    dataset_id = _extract_run_dataset_id(run)
    run_id = getattr(run, "id", None) or dataset_id
    cal_items = list(client.dataset(dataset_id).iterate_items())

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"apify_calendar_{run_id}.json").write_text(
        json.dumps(cal_items, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    updated = apply_calendar_items_to_db(cal_items, reference_date=today, db_path=db_path)
    with get_conn(db_path) as conn:
        log_collection_run(
            conn,
            run_type="airbnb_calendar_30d",
            zone_key=zone_key,
            status="success",
            records_count=updated,
            message=f"Apify 30일 달력 예약률 갱신 완료 ({len(listing_ids)}개 요청 -> {updated}개 반영)",
            started_at=started,
        )
    return {"requested": len(listing_ids), "updated": updated}


def sync_airbnb_via_apify(
    zone_key: str = "seomyeon",
    max_listings: int = 60,
    max_reviews: int = 0,
    hostel_only: bool = True,
    weekend_also: bool = True,
    targeted_urls_only: bool = True,
    fetch_calendar_30d: bool = True,
    api_token: Optional[str] = None,
    actor_id: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """Apify를 통해 평일 기본가(D+7), 주말 기본가(D+14), 30일 달력 예약률을 수집한다.

    targeted_urls_only=True(기본값)이면 이미 DB에 확정된 순수 호스텔·외도민 URL과
    사용자가 직접 등록한 URL만 호출하여 생숙·모텔 수집에 크레딧이 낭비되는 것을 원천 차단한다.
    """
    from apify_client import ApifyClient

    if zone_key not in ZONES:
        raise ValueError(f"지원하지 않는 권역입니다: {zone_key}")

    token = (api_token or get_env("APIFY_API_TOKEN")).strip()
    if not token:
        raise ValueError("APIFY_API_TOKEN이 설정되어 있지 않습니다. .env 또는 설정 화면에서 입력해 주세요.")

    currency = get_env("DEFAULT_CURRENCY", "KRW") or "KRW"
    zone = ZONES[zone_key]
    windows = compute_snapshot_windows()
    if not weekend_also:
        windows = {"D+7_WEEKDAY": windows["D+7_WEEKDAY"]}

    with get_conn(db_path) as conn:
        target_rows = conn.execute(
            """
            SELECT DISTINCT listing_id FROM (
                SELECT listing_id FROM airbnb_listings WHERE is_hostel_like = 1 AND zone_key = ?
                UNION
                SELECT listing_id FROM tracked_airbnb_urls WHERE zone_key = ?
            )
            LIMIT ?
            """,
            (zone_key, zone_key, max_listings),
        ).fetchall()
    target_urls = [{"url": f"https://www.airbnb.co.kr/rooms/{r['listing_id']}"} for r in target_rows]

    started = utc_now_iso()
    client = ApifyClient(token)
    summary: Dict[str, Any] = {"zone": zone.name, "windows": {}}
    total_listings = 0

    for snap_type, (check_in, check_out) in windows.items():
        if targeted_urls_only and target_urls:
            used_actor = URLS_ACTOR_ID
            run_input: Dict[str, Any] = {
                "startUrls": target_urls,
                "checkIn": check_in,
                "checkOut": check_out,
                "currency": currency,
                "locale": "ko-KR",
                "adults": 1,
            }
        else:
            used_actor = (actor_id or get_env("APIFY_AIRBNB_ACTOR", "tri_angle/airbnb-scraper")).strip()
            run_input = {
                "locationQueries": [zone.airbnb_query],
                "checkIn": check_in,
                "checkOut": check_out,
                "currency": currency,
                "maxResults": max_listings,
                "locale": "ko-KR",
                "adults": 1,
            }

        run = client.actor(used_actor).call(run_input=run_input)
        dataset_id = _extract_run_dataset_id(run)
        run_id = getattr(run, "id", None) or dataset_id
        dataset_items = list(client.dataset(dataset_id).iterate_items())

        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"apify_run_{run_id}.json").write_text(
            json.dumps(dataset_items, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        stats = save_apify_items_to_db(
            items=dataset_items,
            snapshot_type=snap_type,
            check_in=check_in,
            check_out=check_out,
            zone_key=zone_key,
            hostel_only=True,
            db_path=db_path,
        )
        summary["windows"][snap_type] = {"check_in": check_in, "check_out": check_out, **stats}
        total_listings += stats["listings_saved"]

    if fetch_calendar_30d:
        cal_res = sync_calendar_30d_via_apify(
            zone_key=zone_key,
            max_items=max_listings,
            api_token=token,
            db_path=db_path,
        )
        summary["calendar_30d"] = cal_res

    with get_conn(db_path) as conn:
        log_collection_run(
            conn,
            run_type="airbnb_apify_sync",
            zone_key=zone_key,
            status="success",
            records_count=total_listings,
            message=f"{zone.name} 정밀 수집 완료 ({summary})",
            started_at=started,
        )

    return summary


def import_airbnb_json(
    json_bytes: bytes,
    zone_key: Optional[str] = "seomyeon",
    snapshot_type: str = "D+7_WEEKDAY",
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    hostel_only: bool = True,
    db_path: Path = DB_PATH,
) -> Dict[str, int]:
    """Apify에서 내려받거나 백업된 JSON 파일(숙소 목록 또는 30일 달력 JSON)을 DB에 적재한다."""
    started = utc_now_iso()
    items = json.loads(json_bytes.decode("utf-8-sig"))
    if not isinstance(items, list):
        raise ValueError("JSON 최상위 구조가 배열(list) 형태여야 합니다.")

    # 달력 전용 JSON(days 배열 포함)인지 판별
    if items and isinstance(items[0], dict) and "days" in items[0] and "listingId" in items[0]:
        updated = apply_calendar_items_to_db(items, db_path=db_path)
        with get_conn(db_path) as conn:
            log_collection_run(
                conn,
                run_type="airbnb_calendar_json_import",
                zone_key=zone_key,
                status="success",
                records_count=updated,
                message=f"30일 달력 JSON 적재: {updated}개 숙소 예약률 갱신",
                started_at=started,
            )
        return {"listings_saved": updated, "reviews_saved": 0, "skipped_entire": 0}

    windows = compute_snapshot_windows()
    default_in, default_out = windows.get(snapshot_type, windows["D+7_WEEKDAY"])

    stats = save_apify_items_to_db(
        items=items,
        snapshot_type=snapshot_type,
        check_in=check_in or default_in,
        check_out=check_out or default_out,
        zone_key=None if zone_key == "all" else zone_key,
        hostel_only=hostel_only,
        db_path=db_path,
    )
    with get_conn(db_path) as conn:
        log_collection_run(
            conn,
            run_type="airbnb_json_import",
            zone_key=zone_key,
            status="success",
            records_count=stats["listings_saved"],
            message=f"JSON 적재 ({snapshot_type}): 호스텔·외도민 {stats['listings_saved']}건 저장, 생숙/모텔/타권역 {stats['skipped_entire']}건 제외",
            started_at=started,
        )
    return stats
