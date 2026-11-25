"""에어비앤비 리뷰 및 숙소 상세 스펙 기반 주요 고객층(여행 유형) 및 포지셔닝 분석 모듈.

불확실한 국적 비율 추정 및 예약 인원수 추정은 완전히 배제하고,
객실 구조·수용인원·하이라이트·리뷰에 기반한 여행 유형(1인/배낭객, 커플/2인, 친구/소그룹, 가족, 비즈니스/출장)과
핵심 포지셔닝 태그만 산출한다.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from src.config import RAW_DIR, get_env
from src.db.database import DB_PATH, get_conn, log_collection_run, upsert_customer_insight, utc_now_iso

SEGMENT_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "1인/배낭객": (
        "혼자",
        "나홀로",
        "솔로",
        "배낭",
        "도미토리",
        "캡슐",
        "1인",
        "게스트하우스",
        "백패커",
        "solo",
        "alone",
        "backpacker",
        "dorm",
        "capsule",
        "bunk",
    ),
    "커플/2인": (
        "남친",
        "여친",
        "커플",
        "연인",
        "부부",
        "둘이",
        "감성",
        "자쿠지",
        "디럭스 더블",
        "couple",
        "partner",
    ),
    "친구/소그룹": (
        "친구",
        "우정",
        "모임",
        "단체",
        "파티",
        "독채",
        "8인",
        "10인",
        "12인",
        "루프탑",
        "friends",
        "group",
        "party",
    ),
    "비즈니스/출장": (
        "출장",
        "업무",
        "워케이션",
        "노트북",
        "스타일러",
        "데스크",
        "작업",
        "business",
        "work",
        "workspace",
    ),
    "가족": (
        "가족",
        "아이",
        "부모님",
        "엄마",
        "아빠",
        "패밀리",
        "유아",
        "아동",
        "family",
        "kids",
        "children",
    ),
}

POSITIONING_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "역세권/접근성": ("서면역", "전포역", "지하철", "역에서", "도보", "위치", "초역세권", "location", "subway", "station", "walk"),
    "프라이빗 자쿠지/다도": ("자쿠지", "히노끼", "편백", "사우나", "반신욕", "다도", "료칸", "풀빌라", "jacuzzi", "spa", "bath"),
    "다인 독채/모임특화": ("독채", "한 층", "단독사용", "8인", "10인", "12인", "16인", "방3", "침실3", "다이닝"),
    "가성비/도미토리": ("가성비", "저렴", "도미토리", "다인실", "캡슐", "2층침대", "이층침대", "budget", "value", "cheap", "dorm"),
    "청결/신축·리모델링": ("깨끗", "청결", "신축", "깔끔", "리모델링", "신규오픈", "오픈특가", "clean", "spotless", "modern", "new"),
    "짐보관/셀프체크인": ("짐보관", "가방 보관", "셀프 체크인", "무료짐보관", "얼리체크인", "luggage", "self check"),
    "조용함/감성휴식": ("조용", "꿀잠", "편안", "감성", "뷰", "오션뷰", "카페거리", "quiet", "peaceful", "cozy"),
    "여성전용/안전": ("여성전용", "여자 도미토리", "여성 도미토리", "보안", "안전", "female", "women only", "safe"),
}

LANG_LABELS = {
    "ko": "한국어",
    "en": "영어권",
    "ja": "일본어",
    "zh": "중국어권",
    "other": "기타 외국어",
}


class GuestSegmentRatio(BaseModel):
    segment_name: str = Field(description="고객층 유형 (1인/배낭객, 커플/2인, 친구/소그룹, 비즈니스/출장, 가족)")
    percentage: int = Field(description="추정 비율 (0~100 정수)")


class HostelReviewInsight(BaseModel):
    guest_segments: List[GuestSegmentRatio] = Field(description="주요 고객층 유형 비율 합계 100")
    positioning_tags: List[str] = Field(description="숙소의 핵심 포지셔닝 태그 3~5개 (한국어)")
    strengths: List[str] = Field(description="핵심 강점 2~3가지 (한국어 요약)")
    weaknesses: List[str] = Field(description="개선점 또는 경쟁 리스크 1~3가지 (한국어 요약)")
    benchmark_note: str = Field(description="호스텔 창업자가 참고할 수 있는 1줄 전략 인사이트")


def detect_language_code(text: str) -> str:
    """텍스트 스크립트 기준 언어 코드를 판별한다."""
    if not text:
        return "other"
    hangul = len(re.findall(r"[\uac00-\ud7a3]", text))
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))

    if hangul >= max(kana, cjk, latin) and hangul > 0:
        return "ko"
    if kana > 0:
        return "ja"
    if cjk > latin and cjk > 0:
        return "zh"
    if latin > 0:
        return "en"
    return "other"


def compute_language_breakdown(reviews: List[Dict[str, Any]]) -> Dict[str, float]:
    """리뷰 목록에서 작성 언어 비율(%)을 계산한다 (하위 호환 유틸리티)."""
    if not reviews:
        return {}
    counts: Counter[str] = Counter()
    for rev in reviews:
        code = (rev.get("language_code") or "").lower()
        if code not in LANG_LABELS:
            code = detect_language_code(rev.get("comment", ""))
        counts[code] += 1

    total = sum(counts.values())
    if total == 0:
        return {}
    return {LANG_LABELS[k]: round((v / total) * 100.0, 1) for k, v in counts.most_common()}


def _load_raw_apify_metadata_map() -> Dict[str, Dict[str, Any]]:
    """data/raw/apify_run_*.json 백업 파일들에서 listing_id별 상세 설명·하이라이트를 로드한다."""
    meta_map: Dict[str, Dict[str, Any]] = {}
    if not RAW_DIR.exists():
        return meta_map
    for path in sorted(RAW_DIR.glob("apify_run_*.json")):
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and item.get("id"):
                        meta_map[str(item["id"])] = item
        except Exception:
            continue
    return meta_map


def _rule_based_insight(
    listing: Dict[str, Any],
    reviews: List[Dict[str, Any]],
    raw_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """객실 스펙(수용인원·객실유형·하이라이트·설명) 및 리뷰 기반으로 주요 고객층(여행 유형)과 포지셔닝을 도출한다."""
    seg_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()

    text_chunks = [str(listing.get("title") or "")]
    for rev in reviews:
        text_chunks.append(str(rev.get("comment") or ""))

    if raw_meta:
        text_chunks.append(str(raw_meta.get("description") or ""))
        for hl in raw_meta.get("highlights") or []:
            if isinstance(hl, dict):
                text_chunks.append(f"{hl.get('title') or ''} {hl.get('subtitle') or ''}")

    full_text = " ".join(text_chunks).lower()

    for seg, kws in SEGMENT_KEYWORDS.items():
        for kw in kws:
            if kw in full_text:
                seg_counts[seg] += 2
    for tag, kws in POSITIONING_KEYWORDS.items():
        for kw in kws:
            if kw in full_text:
                tag_counts[tag] += 1

    cap = int(listing.get("capacity") or 2)
    rt = str(listing.get("room_type") or "").lower()
    if "shared" in rt or cap == 1:
        seg_counts["1인/배낭객"] += 6
        seg_counts["친구/소그룹"] += 2
    elif cap == 2:
        seg_counts["커플/2인"] += 5
        seg_counts["1인/배낭객"] += 3
        seg_counts["비즈니스/출장"] += 2
    elif 3 <= cap <= 5:
        seg_counts["친구/소그룹"] += 5
        seg_counts["가족"] += 3
        seg_counts["커플/2인"] += 2
    else:
        seg_counts["친구/소그룹"] += 6
        seg_counts["가족"] += 4

    total_seg = sum(seg_counts.values()) or 1
    guest_segments = {k: round((v / total_seg) * 100.0, 1) for k, v in seg_counts.most_common()}
    positioning_tags = [k for k, _ in tag_counts.most_common(4)] or ["역세권/접근성"]

    strengths: List[str] = []
    if raw_meta and isinstance(raw_meta.get("highlights"), list):
        hl_titles = [str(h.get("title")) for h in raw_meta["highlights"] if isinstance(h, dict) and h.get("title")]
        if hl_titles:
            strengths.append(f"핵심 어필 포인트: {', '.join(hl_titles[:4])}")
    strengths.append(f"포지셔닝 강점: {', '.join(positioning_tags[:3])} (최대 {cap}인 수용)")

    rating_val = listing.get("rating")
    rev_cnt = int(listing.get("reviews_count") or 0)
    weaknesses: List[str] = []
    if rev_cnt < 5:
        weaknesses.append(f"신규/초기 매물(누적 리뷰 {rev_cnt}건)로 초기 리뷰 확보 필요")
    elif rating_val and float(rating_val) < 4.7:
        weaknesses.append(f"평점 ★{rating_val} (권역 평균 대비 시설·방음·청결 관리 점검 필요)")
    else:
        weaknesses.append("고평점 유지 중 — 주말 성수기 가격 및 장기 연박 경쟁 주의")

    top_seg = next(iter(guest_segments.keys()), "1인/배낭객")
    benchmark_note = (
        f"핵심 타겟 고객층은 '{top_seg}'(최대 {cap}인)이며, "
        f"주요 차별화 요소는 [{', '.join(positioning_tags[:2])}]입니다."
    )

    return {
        "listing_id": listing["listing_id"],
        "analysis_method": "spec_and_keyword",
        "review_sample_count": len(reviews) if reviews else rev_cnt,
        "language_breakdown": {},
        "guest_segments": guest_segments,
        "positioning_tags": positioning_tags,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "benchmark_note": benchmark_note,
    }


def _analyze_with_gemini(
    listing: Dict[str, Any],
    reviews: List[Dict[str, Any]],
    api_key: str,
    model_name: str,
) -> Dict[str, Any]:
    """Google GenAI SDK (`google-genai`) Structured Output으로 고객 유형과 포지셔닝을 분석한다."""
    from google import genai

    client = genai.Client(api_key=api_key)
    review_snippets = "\n".join(
        f"- [{r.get('review_date') or '날짜미상'}] {r['comment'][:350]}"
        for r in reviews[:35]
    )

    prompt = (
        "당신은 부산 호스텔·외국인관광도시민박업 상권분석 전문가입니다.\n"
        f"숙소명: {listing.get('title')}\n"
        f"숙소유형: {listing.get('room_type')} / {listing.get('property_type')} (최대 {listing.get('capacity')}인)\n"
        f"평점: {listing.get('rating')} (총 리뷰 {listing.get('reviews_count')}개)\n\n"
        "아래 고객 리뷰들을 분석하여 주요 고객층(여행 유형) 비중, 포지셔닝 태그, 강점, 약점, 창업 벤치마킹 포인트를 도출하세요.\n\n"
        f"{review_snippets}"
    )

    interaction = client.interactions.create(
        model=model_name,
        input=prompt,
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": HostelReviewInsight.model_json_schema(),
        },
    )
    parsed = HostelReviewInsight.model_validate_json(interaction.output_text)
    seg_dict = {item.segment_name: item.percentage for item in parsed.guest_segments}

    return {
        "listing_id": listing["listing_id"],
        "analysis_method": f"gemini:{model_name}",
        "review_sample_count": len(reviews),
        "language_breakdown": {},
        "guest_segments": seg_dict,
        "positioning_tags": parsed.positioning_tags,
        "strengths": parsed.strengths,
        "weaknesses": parsed.weaknesses,
        "benchmark_note": parsed.benchmark_note,
    }


def analyze_all_listings(
    zone_key: Optional[str] = None,
    use_ai: bool = True,
    db_path: Path = DB_PATH,
) -> Dict[str, int]:
    """DB에 저장된 숙소들의 주요 고객층(여행 유형) 및 포지셔닝을 일괄 분석해 저장한다."""
    started = utc_now_iso()
    gemini_key = get_env("GEMINI_API_KEY") if use_ai else ""
    model_name = get_env("GEMINI_MODEL", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite"
    raw_meta_map = _load_raw_apify_metadata_map()

    analyzed_ai = 0
    analyzed_rule = 0

    with get_conn(db_path) as conn:
        query = "SELECT * FROM airbnb_listings"
        params: List[Any] = []
        if zone_key and zone_key != "all":
            query += " WHERE zone_key = ?"
            params.append(zone_key)

        listings = [dict(r) for r in conn.execute(query, params).fetchall()]
        for listing in listings:
            revs = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM reviews WHERE listing_id = ? ORDER BY review_date DESC LIMIT 40",
                    (listing["listing_id"],),
                ).fetchall()
            ]
            raw_meta = raw_meta_map.get(str(listing["listing_id"]))
            insight: Optional[Dict[str, Any]] = None

            if revs and gemini_key:
                try:
                    insight = _analyze_with_gemini(listing, revs, gemini_key, model_name)
                    analyzed_ai += 1
                except Exception:
                    insight = None

            if insight is None:
                insight = _rule_based_insight(listing, revs, raw_meta=raw_meta)
                analyzed_rule += 1

            upsert_customer_insight(conn, insight)

        log_collection_run(
            conn,
            run_type="review_analysis",
            zone_key=zone_key,
            status="success",
            records_count=analyzed_ai + analyzed_rule,
            message=f"주요 고객층·포지셔닝 분석 완료 (Gemini AI {analyzed_ai}건, 스펙·키워드 기반 {analyzed_rule}건)",
            started_at=started,
        )

    return {"ai": analyzed_ai, "rule": analyzed_rule, "total": analyzed_ai + analyzed_rule}
