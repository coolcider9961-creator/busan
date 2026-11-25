"""자동 스케줄링 및 터미널 실행용 CLI."""

from __future__ import annotations

import argparse
from typing import Optional

from src.collectors.airbnb_apify import sync_airbnb_via_apify
from src.collectors.public_data import (
    import_localdata_csv,
    sync_public_data_from_api,
    sync_public_data_from_localdata_portal,
)
from src.config import ZONES, get_env
from src.db.database import clear_demo_data, init_db
from src.demo_seed import seed_demo_data
from src.processing.matcher import run_auto_matching
from src.processing.review_analyzer import analyze_all_listings


def run_full_update(
    zone_key: str,
    max_listings: int = 80,
    max_reviews: int = 30,
    hostel_only: bool = True,
    skip_public_api: bool = False,
    skip_airbnb: bool = False,
    use_ai: bool = True,
) -> None:
    """공공데이터 + 에어비앤비 + 매칭 + 리뷰 AI 분석을 일괄 실행한다."""
    init_db()
    if not skip_public_api:
        if get_env("DATA_GO_KR_API_KEY"):
            res = sync_public_data_from_api(zone_filter=zone_key)
            print(f"[공공데이터 OpenAPI] 동기화 완료: {res}")
        else:
            res = sync_public_data_from_localdata_portal(zone_filter=None)
            print(f"[공공데이터 LOCALDATA 공식파일] 부산 전체 자동 동기화 완료: {res}")
    else:
        print("[공공데이터] 건너뛰기 선택 — 기존 DB 데이터 유지")

    if not skip_airbnb and get_env("APIFY_API_TOKEN"):
        res_air = sync_airbnb_via_apify(
            zone_key=zone_key,
            max_listings=max_listings,
            max_reviews=max_reviews,
            hostel_only=hostel_only,
        )
        print(f"[에어비앤비] Apify 스냅샷 수집 완료: {res_air}")
    else:
        print("[에어비앤비] Apify 토큰 미설정 또는 건너뛰기 선택 — 기존 DB 데이터 유지")

    matched = run_auto_matching(zone_key=zone_key)
    print(f"[매칭] 공공-에어비앤비 자동 매칭 완료: {matched}건")

    analyzed = analyze_all_listings(zone_key=zone_key, use_ai=use_ai)
    print(f"[리뷰분석] 고객층 분석 완료: {analyzed}")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="부산 호스텔·외도민 상권분석 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_update = sub.add_parser("update", help="정기 데이터 업데이트 전체 파이프라인 실행")
    p_update.add_argument("--zone", default="seomyeon", choices=list(ZONES.keys()))
    p_update.add_argument("--max-listings", type=int, default=80)
    p_update.add_argument("--max-reviews", type=int, default=30)
    p_update.add_argument("--include-entire-home", action="store_true")
    p_update.add_argument("--no-ai", action="store_true")

    p_csv = sub.add_parser("import-csv", help="LOCALDATA CSV 파일 임포트")
    p_csv.add_argument("path", help="CSV 파일 경로")
    p_csv.add_argument("--industry", choices=["호스텔업", "외국인관광도시민박업"], default=None)
    p_csv.add_argument("--zone", default="seomyeon")

    p_analyze = sub.add_parser("analyze", help="저장된 리뷰 재분석 및 재매칭")
    p_analyze.add_argument("--zone", default="seomyeon")
    p_analyze.add_argument("--no-ai", action="store_true")

    sub.add_parser("seed-demo", help="서면 테스트용 데모 데이터 생성")
    sub.add_parser("clear-demo", help="데모 데이터 일괄 삭제")

    args = parser.parse_args(argv)
    init_db()

    if args.cmd == "update":
        run_full_update(
            zone_key=args.zone,
            max_listings=args.max_listings,
            max_reviews=args.max_reviews,
            hostel_only=not args.include_entire_home,
            use_ai=not args.no_ai,
        )
    elif args.cmd == "import-csv":
        from pathlib import Path

        res = import_localdata_csv(Path(args.path), forced_industry=args.industry, zone_filter=args.zone)
        print(f"CSV 임포트 완료: {res}")
    elif args.cmd == "analyze":
        matched = run_auto_matching(zone_key=args.zone)
        analyzed = analyze_all_listings(zone_key=args.zone, use_ai=not args.no_ai)
        print(f"매칭 {matched}건, 리뷰분석 {analyzed}")
    elif args.cmd == "seed-demo":
        seed_demo_data()
        print("데모 데이터 생성 완료")
    elif args.cmd == "clear-demo":
        clear_demo_data()
        print("데모 데이터 삭제 완료")


if __name__ == "__main__":
    main()
