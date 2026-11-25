"""대시보드 기능 체험 및 테스트용 샘플(데모) 데이터 시더.

실제 데이터와 혼동되지 않도록 모든 레코드에 is_demo = 1 플래그를 부여한다.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from src.config import INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK
from src.db.database import (
    DB_PATH,
    clear_demo_data,
    get_conn,
    init_db,
    insert_price_snapshot,
    insert_reviews,
    log_collection_run,
    upsert_airbnb_listing,
    upsert_public_business,
    utc_now_iso,
)
from src.processing.matcher import run_auto_matching
from src.processing.review_analyzer import analyze_all_listings


def seed_demo_data(db_path: Path = DB_PATH) -> None:
    """서면 권역 기준 데모 데이터(공공 2업종 + 에어비앤비 + 3주치 스냅샷 + 다국어 리뷰)를 생성한다."""
    init_db(db_path)
    clear_demo_data(db_path)

    today = date.today()
    started = utc_now_iso()

    public_samples = [
        {
            "source_key": "demo:hostel:1",
            "mng_no": "3290000-101-2023-00012",
            "industry_type": INDUSTRY_HOSTEL,
            "detail_industry": "호스텔업",
            "business_name": "[데모] 서면 브릿지 호스텔",
            "status_name": "영업/정상",
            "detail_status_name": "영업",
            "license_date": "20230418",
            "close_date": None,
            "road_address": "부산광역시 부산진구 중앙대로691번가길 24",
            "jibun_address": "부산광역시 부산진구 부전동 520-12",
            "phone": "051-000-1001",
            "site_area": 412.5,
            "sigungu": "부산진구",
            "dong_name": "부전동",
            "zone_key": "seomyeon",
            "lat": 35.1566,
            "lon": 129.0574,
            "raw_x": None,
            "raw_y": None,
            "is_demo": 1,
        },
        {
            "source_key": "demo:hostel:2",
            "mng_no": "3290000-101-2021-00008",
            "industry_type": INDUSTRY_HOSTEL,
            "detail_industry": "호스텔업",
            "business_name": "[데모] 전포 라운지 호스텔",
            "status_name": "영업/정상",
            "detail_status_name": "영업",
            "license_date": "20210902",
            "close_date": None,
            "road_address": "부산광역시 부산진구 전포대로199번길 18",
            "jibun_address": "부산광역시 부산진구 전포동 663-5",
            "phone": "051-000-1002",
            "site_area": 355.0,
            "sigungu": "부산진구",
            "dong_name": "전포동",
            "zone_key": "seomyeon",
            "lat": 35.1553,
            "lon": 129.0631,
            "raw_x": None,
            "raw_y": None,
            "is_demo": 1,
        },
        {
            "source_key": "demo:hostel:3",
            "mng_no": "3290000-101-2019-00004",
            "industry_type": INDUSTRY_HOSTEL,
            "detail_industry": "호스텔업",
            "business_name": "[데모] 부전 구형 백패커스",
            "status_name": "폐업",
            "detail_status_name": "폐업",
            "license_date": "20190311",
            "close_date": "20250214",
            "road_address": "부산광역시 부산진구 부전로 88",
            "jibun_address": "부산광역시 부산진구 부전동 241-3",
            "phone": None,
            "site_area": 280.0,
            "sigungu": "부산진구",
            "dong_name": "부전동",
            "zone_key": "seomyeon",
            "lat": 35.1592,
            "lon": 129.0558,
            "raw_x": None,
            "raw_y": None,
            "is_demo": 1,
        },
        {
            "source_key": "demo:minbak:1",
            "mng_no": "3290000-204-2024-00031",
            "industry_type": INDUSTRY_URBAN_MINBAK,
            "detail_industry": "외국인관광도시민박업",
            "business_name": "[데모] 전포카페거리 코지하우스",
            "status_name": "영업/정상",
            "detail_status_name": "영업",
            "license_date": "20240520",
            "close_date": None,
            "road_address": "부산광역시 부산진구 동천로85번길 15",
            "jibun_address": "부산광역시 부산진구 전포동 684-9",
            "phone": "051-000-2001",
            "site_area": 118.2,
            "sigungu": "부산진구",
            "dong_name": "전포동",
            "zone_key": "seomyeon",
            "lat": 35.1561,
            "lon": 129.0617,
            "raw_x": None,
            "raw_y": None,
            "is_demo": 1,
        },
        {
            "source_key": "demo:minbak:2",
            "mng_no": "3290000-204-2025-00014",
            "industry_type": INDUSTRY_URBAN_MINBAK,
            "detail_industry": "외국인관광도시민박업",
            "business_name": "[데모] 서면 스테이 754 게스트하우스",
            "status_name": "영업/정상",
            "detail_status_name": "영업",
            "license_date": "20250115",
            "close_date": None,
            "road_address": "부산광역시 부산진구 서면로 41",
            "jibun_address": "부산광역시 부산진구 부전동 515-4",
            "phone": "051-000-2002",
            "site_area": 146.0,
            "sigungu": "부산진구",
            "dong_name": "부전동",
            "zone_key": "seomyeon",
            "lat": 35.1548,
            "lon": 129.0579,
            "raw_x": None,
            "raw_y": None,
            "is_demo": 1,
        },
    ]

    airbnb_samples = [
        {
            "listing_id": "demo_1001",
            "title": "[데모] Seomyeon Bridge Hostel - 6인 도미토리 1베드 (서면역 3분)",
            "url": "https://www.airbnb.co.kr/rooms/demo_1001",
            "room_type": "Shared room",
            "property_type": "Hostel",
            "is_hostel_like": 1,
            "host_name": "Bridge Hostel",
            "is_superhost": 1,
            "capacity": 1,
            "bedrooms": 1,
            "beds": 6,
            "bathrooms": 2.0,
            "rating": 4.87,
            "reviews_count": 214,
            "recent_review_date": (today - timedelta(days=3)).isoformat(),
            "operating_status": "활성 영업",
            "registration_number": "3290000-101-2023-00012",
            "address_hint": "부산진구 부전동",
            "zone_key": "seomyeon",
            "lat": 35.1569,
            "lon": 129.0578,
            "is_demo": 1,
            "weekday_base": 29000,
            "weekend_base": 42000,
            "cleaning": 0,
            "reviews": [
                ("en", "Great location right next to Seomyeon subway station. Met lots of solo travelers in the lounge!", 3),
                ("ja", "一人旅で利用しました。駅から近くてコスパ最高です。スタッフも親切でした。", 8),
                ("ko", "서면역에서 진짜 가깝고 도미토리 침구 깨끗해서 혼자 묵기 딱 좋아요. 외국인 투숙객이 많네요.", 14),
                ("zh", "离西面地铁站很近，一个人住很安全，性价比很高。", 21),
                ("en", "Clean bunk beds with privacy curtains. Quiet at night despite being near bars.", 28),
            ],
        },
        {
            "listing_id": "demo_1002",
            "title": "[데모] Jeonpo Lounge Hostel | 여성전용 도미토리 & 루프탑",
            "url": "https://www.airbnb.co.kr/rooms/demo_1002",
            "room_type": "Shared room",
            "property_type": "Hostel",
            "is_hostel_like": 1,
            "host_name": "Jeonpo Lounge",
            "is_superhost": 0,
            "capacity": 1,
            "bedrooms": 1,
            "beds": 4,
            "bathrooms": 1.5,
            "rating": 4.74,
            "reviews_count": 138,
            "recent_review_date": (today - timedelta(days=6)).isoformat(),
            "operating_status": "활성 영업",
            "registration_number": "3290000-101-2021-00008",
            "address_hint": "부산진구 전포동",
            "zone_key": "seomyeon",
            "lat": 35.1556,
            "lon": 129.0628,
            "is_demo": 1,
            "weekday_base": 33000,
            "weekend_base": 49000,
            "cleaning": 5000,
            "reviews": [
                ("ko", "전포 카페거리 바로 앞이라 친구랑 호캉스 대신 가성비로 묵기 좋았어요. 루프탑 펍 분위기 좋아요.", 6),
                ("en", "Fun social hostel! Great for solo female backpackers. A bit noisy on Friday night.", 12),
                ("ja", "カフェ通りに近くて友達との旅行に最高でした。女性専用なので安心です。", 19),
                ("ko", "방음은 조금 아쉽지만 파우더룸이 잘 되어 있어서 만족합니다.", 25),
            ],
        },
        {
            "listing_id": "demo_1003",
            "title": "[데모] Jeonpo Cafe Street Cozy House - 프라이빗 트윈룸",
            "url": "https://www.airbnb.co.kr/rooms/demo_1003",
            "room_type": "Private room",
            "property_type": "Guesthouse",
            "is_hostel_like": 1,
            "host_name": "Cozy Jeonpo",
            "is_superhost": 1,
            "capacity": 2,
            "bedrooms": 1,
            "beds": 2,
            "bathrooms": 1.0,
            "rating": 4.92,
            "reviews_count": 96,
            "recent_review_date": (today - timedelta(days=2)).isoformat(),
            "operating_status": "활성 영업",
            "registration_number": "3290000-204-2024-00031",
            "address_hint": "부산진구 전포동",
            "zone_key": "seomyeon",
            "lat": 35.1563,
            "lon": 129.0619,
            "is_demo": 1,
            "weekday_base": 68000,
            "weekend_base": 98000,
            "cleaning": 12000,
            "reviews": [
                ("en", "My girlfriend and I loved staying here. Very quiet residential alley near awesome cafes.", 2),
                ("ja", "カップルで利用。ホストがとても親切で朝ごはんも美味しかったです。", 11),
                ("zh", "房间非常干净，适合两个人住，去田浦咖啡街步行五分钟。", 17),
                ("en", "Spotless private room, fast wifi for remote work.", 27),
            ],
        },
        {
            "listing_id": "demo_1004",
            "title": "[데모] Seomyeon Stay 754 - 4인 친구/패밀리 룸 (조식포함)",
            "url": "https://www.airbnb.co.kr/rooms/demo_1004",
            "room_type": "Private room",
            "property_type": "Guesthouse",
            "is_hostel_like": 1,
            "host_name": "Stay 754",
            "is_superhost": 0,
            "capacity": 4,
            "bedrooms": 1,
            "beds": 2,
            "bathrooms": 1.0,
            "rating": 4.68,
            "reviews_count": 47,
            "recent_review_date": (today - timedelta(days=9)).isoformat(),
            "operating_status": "활성 영업",
            "registration_number": "3290000-204-2025-00014",
            "address_hint": "부산진구 부전동",
            "zone_key": "seomyeon",
            "lat": 35.1551,
            "lon": 129.0582,
            "is_demo": 1,
            "weekday_base": 82000,
            "weekend_base": 119000,
            "cleaning": 15000,
            "reviews": [
                ("zh", "四个朋友一起来釜山玩，房间宽敞，西面逛街吃东西很方便。", 9),
                ("en", "Good value for a small group of 3-4 friends. Easy self check-in.", 20),
                ("ko", "출장 겸 친구와 묵었는데 책상도 있고 서면 중심가라 이동하기 좋았습니다.", 35),
            ],
        },
    ]

    with get_conn(db_path) as conn:
        for pub in public_samples:
            upsert_public_business(conn, pub)

        for item in airbnb_samples:
            weekday_base = item.pop("weekday_base")
            weekend_base = item.pop("weekend_base")
            cleaning = item.pop("cleaning")
            revs = item.pop("reviews")

            upsert_airbnb_listing(conn, item)

            # 3주치 시계열 스냅샷 생성 (추세 그래프 확인용)
            for week_offset, factor in ((14, 0.94), (7, 0.98), (0, 1.0)):
                snap_dt = (today - timedelta(days=week_offset)).isoformat()
                wd_in = (today - timedelta(days=week_offset) + timedelta(days=7)).isoformat()
                wd_out = (today - timedelta(days=week_offset) + timedelta(days=8)).isoformat()
                we_in = (today - timedelta(days=week_offset) + timedelta(days=14)).isoformat()
                we_out = (today - timedelta(days=week_offset) + timedelta(days=15)).isoformat()

                wd_night = round(weekday_base * factor, -2)
                we_night = round(weekend_base * factor, -2)
                insert_price_snapshot(
                    conn,
                    {
                        "listing_id": item["listing_id"],
                        "snapshot_date": snap_dt,
                        "snapshot_type": "D+7_WEEKDAY",
                        "check_in": wd_in,
                        "check_out": wd_out,
                        "currency": "KRW",
                        "nightly_price": wd_night,
                        "cleaning_fee": cleaning,
                        "service_fee": round(wd_night * 0.14, -2),
                        "total_price": wd_night + cleaning + round(wd_night * 0.14, -2),
                        "is_available": 1,
                    },
                )
                insert_price_snapshot(
                    conn,
                    {
                        "listing_id": item["listing_id"],
                        "snapshot_date": snap_dt,
                        "snapshot_type": "D+14_WEEKEND",
                        "check_in": we_in,
                        "check_out": we_out,
                        "currency": "KRW",
                        "nightly_price": we_night,
                        "cleaning_fee": cleaning,
                        "service_fee": round(we_night * 0.14, -2),
                        "total_price": we_night + cleaning + round(we_night * 0.14, -2),
                        "is_available": 1,
                    },
                )

            rev_rows = [
                {
                    "listing_id": item["listing_id"],
                    "review_uid": f"{item['listing_id']}_r_{idx}",
                    "reviewer_name": f"Guest{idx + 1}",
                    "review_date": (today - timedelta(days=days_ago)).isoformat(),
                    "language_code": lang,
                    "rating": 5.0,
                    "comment": text,
                }
                for idx, (lang, text, days_ago) in enumerate(revs)
            ]
            insert_reviews(conn, rev_rows)

        log_collection_run(
            conn,
            run_type="demo_seed",
            zone_key="seomyeon",
            status="success",
            records_count=len(airbnb_samples),
            message="서면(부전·전포) 테스트용 데모 데이터 생성 완료",
            started_at=started,
        )

    run_auto_matching(zone_key="seomyeon", db_path=db_path)
    analyze_all_listings(zone_key="seomyeon", use_ai=False, db_path=db_path)
