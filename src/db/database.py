"""SQLite 시계열 데이터베이스 스키마 및 CRUD 헬퍼."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import requests

from src.config import DATA_DIR, DB_PATH, get_env

LAST_SYNC_META_PATH = DATA_DIR / "last_sync.json"
_LAST_GITHUB_PULL_CHECK_TS: float = 0.0


def utc_now_iso() -> str:
    """현재 UTC ISO-8601 문자열을 반환한다."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    """SQLite 연결 컨텍스트 매니저."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    """전체 테이블과 인덱스를 초기화한다."""
    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS public_businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                mng_no TEXT,
                industry_type TEXT NOT NULL,
                detail_industry TEXT,
                business_name TEXT NOT NULL,
                status_name TEXT NOT NULL,
                detail_status_name TEXT,
                license_date TEXT,
                close_date TEXT,
                road_address TEXT,
                jibun_address TEXT,
                phone TEXT,
                site_area REAL,
                sigungu TEXT,
                dong_name TEXT,
                zone_key TEXT,
                lat REAL,
                lon REAL,
                raw_x REAL,
                raw_y REAL,
                is_demo INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_public_zone_ind
                ON public_businesses(zone_key, industry_type, status_name);

            CREATE TABLE IF NOT EXISTS airbnb_listings (
                listing_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                room_type TEXT,
                property_type TEXT,
                is_hostel_like INTEGER NOT NULL DEFAULT 1,
                host_name TEXT,
                is_superhost INTEGER DEFAULT 0,
                capacity INTEGER,
                bedrooms INTEGER,
                beds REAL,
                bathrooms REAL,
                rating REAL,
                reviews_count INTEGER DEFAULT 0,
                recent_review_date TEXT,
                operating_status TEXT,
                registration_number TEXT,
                address_hint TEXT,
                zone_key TEXT,
                lat REAL,
                lon REAL,
                is_demo INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_airbnb_zone
                ON airbnb_listings(zone_key, is_hostel_like, operating_status);

            CREATE TABLE IF NOT EXISTS business_listing_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id INTEGER NOT NULL REFERENCES public_businesses(id) ON DELETE CASCADE,
                listing_id TEXT NOT NULL REFERENCES airbnb_listings(listing_id) ON DELETE CASCADE,
                match_score REAL NOT NULL,
                match_reason TEXT,
                is_manual INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(public_id, listing_id)
            );

            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL REFERENCES airbnb_listings(listing_id) ON DELETE CASCADE,
                snapshot_date TEXT NOT NULL,
                snapshot_type TEXT NOT NULL,
                check_in TEXT NOT NULL,
                check_out TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'KRW',
                nightly_price REAL,
                cleaning_fee REAL DEFAULT 0,
                service_fee REAL DEFAULT 0,
                total_price REAL,
                is_available INTEGER DEFAULT 1,
                collected_at TEXT NOT NULL,
                UNIQUE(listing_id, snapshot_date, snapshot_type)
            );

            CREATE INDEX IF NOT EXISTS idx_price_listing_date
                ON price_snapshots(listing_id, snapshot_date, snapshot_type);

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL REFERENCES airbnb_listings(listing_id) ON DELETE CASCADE,
                review_uid TEXT NOT NULL,
                reviewer_name TEXT,
                review_date TEXT,
                language_code TEXT,
                rating REAL,
                comment TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(listing_id, review_uid)
            );

            CREATE INDEX IF NOT EXISTS idx_reviews_listing
                ON reviews(listing_id, review_date);

            CREATE TABLE IF NOT EXISTS customer_insights (
                listing_id TEXT PRIMARY KEY REFERENCES airbnb_listings(listing_id) ON DELETE CASCADE,
                analyzed_at TEXT NOT NULL,
                analysis_method TEXT NOT NULL,
                review_sample_count INTEGER NOT NULL DEFAULT 0,
                language_breakdown_json TEXT NOT NULL,
                guest_segments_json TEXT NOT NULL,
                positioning_tags_json TEXT NOT NULL,
                strengths_json TEXT NOT NULL,
                weaknesses_json TEXT NOT NULL,
                benchmark_note TEXT
            );

            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_type TEXT NOT NULL,
                zone_key TEXT,
                status TEXT NOT NULL,
                records_count INTEGER DEFAULT 0,
                message TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tracked_airbnb_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                public_id INTEGER REFERENCES public_businesses(id) ON DELETE SET NULL,
                zone_key TEXT,
                memo TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS calendar_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id TEXT NOT NULL REFERENCES airbnb_listings(listing_id) ON DELETE CASCADE,
                snapshot_date TEXT NOT NULL,
                occ_14d_pct REAL,
                occ_30d_pct REAL,
                booked_14d_days INTEGER,
                booked_30d_days INTEGER,
                raw_days_json TEXT,
                collected_at TEXT NOT NULL,
                UNIQUE(listing_id, snapshot_date)
            );

            CREATE TABLE IF NOT EXISTS tourism_visitor_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_key TEXT NOT NULL,
                sigungu_code TEXT NOT NULL,
                sigungu_name TEXT NOT NULL,
                base_ym TEXT NOT NULL,
                base_ymd TEXT NOT NULL,
                visitor_type TEXT NOT NULL,
                visitor_count REAL NOT NULL DEFAULT 0,
                is_baseline INTEGER NOT NULL DEFAULT 0,
                collected_at TEXT NOT NULL,
                UNIQUE(sigungu_code, base_ymd, visitor_type)
            );

            CREATE INDEX IF NOT EXISTS idx_tourism_zone_ym
                ON tourism_visitor_stats(zone_key, base_ym, visitor_type);

            CREATE TABLE IF NOT EXISTS tourism_nationality_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_key TEXT NOT NULL,
                base_ym TEXT NOT NULL,
                nationality TEXT NOT NULL,
                visitor_share_pct REAL NOT NULL DEFAULT 0,
                lodging_spend_krw REAL,
                is_baseline INTEGER NOT NULL DEFAULT 0,
                collected_at TEXT NOT NULL,
                UNIQUE(zone_key, base_ym, nationality)
            );
            """
        )
        # 기존 DB 호환을 위한 신규 컬럼 마이그레이션
        existing_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(airbnb_listings);").fetchall()
        }
        new_cols = {
            "extra_guest_fee_note": "TEXT",
            "occ_14d_pct": "REAL",
            "occ_30d_pct": "REAL",
            "booked_14d_days": "INTEGER",
            "booked_30d_days": "INTEGER",
            "calendar_updated_at": "TEXT",
            "thumbnail_url": "TEXT",
        }
        for col_name, col_type in new_cols.items():
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE airbnb_listings ADD COLUMN {col_name} {col_type};")


def upsert_public_business(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """공공 인허가 숙박업소 데이터를 저장 또는 갱신한다."""
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO public_businesses (
            source_key, mng_no, industry_type, detail_industry, business_name,
            status_name, detail_status_name, license_date, close_date,
            road_address, jibun_address, phone, site_area, sigungu, dong_name,
            zone_key, lat, lon, raw_x, raw_y, is_demo, updated_at
        ) VALUES (
            :source_key, :mng_no, :industry_type, :detail_industry, :business_name,
            :status_name, :detail_status_name, :license_date, :close_date,
            :road_address, :jibun_address, :phone, :site_area, :sigungu, :dong_name,
            :zone_key, :lat, :lon, :raw_x, :raw_y, :is_demo, :updated_at
        )
        ON CONFLICT(source_key) DO UPDATE SET
            mng_no = COALESCE(excluded.mng_no, public_businesses.mng_no),
            industry_type = excluded.industry_type,
            detail_industry = COALESCE(excluded.detail_industry, public_businesses.detail_industry),
            business_name = excluded.business_name,
            status_name = excluded.status_name,
            detail_status_name = excluded.detail_status_name,
            license_date = COALESCE(excluded.license_date, public_businesses.license_date),
            close_date = excluded.close_date,
            road_address = COALESCE(excluded.road_address, public_businesses.road_address),
            jibun_address = COALESCE(excluded.jibun_address, public_businesses.jibun_address),
            phone = COALESCE(excluded.phone, public_businesses.phone),
            site_area = COALESCE(excluded.site_area, public_businesses.site_area),
            sigungu = COALESCE(excluded.sigungu, public_businesses.sigungu),
            dong_name = COALESCE(excluded.dong_name, public_businesses.dong_name),
            zone_key = COALESCE(excluded.zone_key, public_businesses.zone_key),
            lat = COALESCE(excluded.lat, public_businesses.lat),
            lon = COALESCE(excluded.lon, public_businesses.lon),
            raw_x = COALESCE(excluded.raw_x, public_businesses.raw_x),
            raw_y = COALESCE(excluded.raw_y, public_businesses.raw_y),
            is_demo = excluded.is_demo,
            updated_at = excluded.updated_at
        """,
        {**row, "updated_at": now},
    )


def upsert_airbnb_listing(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """에어비앤비 숙소 정보를 저장 또는 갱신한다."""
    now = utc_now_iso()
    payload = {
        "extra_guest_fee_note": None,
        "occ_14d_pct": None,
        "occ_30d_pct": None,
        "booked_14d_days": None,
        "booked_30d_days": None,
        "calendar_updated_at": None,
        "thumbnail_url": None,
        **row,
        "first_seen_at": now,
        "last_seen_at": now,
    }
    conn.execute(
        """
        INSERT INTO airbnb_listings (
            listing_id, title, url, room_type, property_type, is_hostel_like,
            host_name, is_superhost, capacity, bedrooms, beds, bathrooms,
            rating, reviews_count, recent_review_date, operating_status,
            registration_number, address_hint, zone_key, lat, lon, is_demo,
            extra_guest_fee_note, occ_14d_pct, occ_30d_pct, booked_14d_days, booked_30d_days,
            calendar_updated_at, thumbnail_url, first_seen_at, last_seen_at
        ) VALUES (
            :listing_id, :title, :url, :room_type, :property_type, :is_hostel_like,
            :host_name, :is_superhost, :capacity, :bedrooms, :beds, :bathrooms,
            :rating, :reviews_count, :recent_review_date, :operating_status,
            :registration_number, :address_hint, :zone_key, :lat, :lon, :is_demo,
            :extra_guest_fee_note, :occ_14d_pct, :occ_30d_pct, :booked_14d_days, :booked_30d_days,
            :calendar_updated_at, :thumbnail_url, :first_seen_at, :last_seen_at
        )
        ON CONFLICT(listing_id) DO UPDATE SET
            title = excluded.title,
            url = excluded.url,
            room_type = COALESCE(excluded.room_type, airbnb_listings.room_type),
            property_type = COALESCE(excluded.property_type, airbnb_listings.property_type),
            is_hostel_like = excluded.is_hostel_like,
            host_name = COALESCE(excluded.host_name, airbnb_listings.host_name),
            is_superhost = excluded.is_superhost,
            capacity = COALESCE(excluded.capacity, airbnb_listings.capacity),
            bedrooms = COALESCE(excluded.bedrooms, airbnb_listings.bedrooms),
            beds = COALESCE(excluded.beds, airbnb_listings.beds),
            bathrooms = COALESCE(excluded.bathrooms, airbnb_listings.bathrooms),
            rating = COALESCE(excluded.rating, airbnb_listings.rating),
            reviews_count = COALESCE(excluded.reviews_count, airbnb_listings.reviews_count),
            recent_review_date = COALESCE(excluded.recent_review_date, airbnb_listings.recent_review_date),
            operating_status = COALESCE(excluded.operating_status, airbnb_listings.operating_status),
            registration_number = COALESCE(excluded.registration_number, airbnb_listings.registration_number),
            address_hint = COALESCE(excluded.address_hint, airbnb_listings.address_hint),
            zone_key = COALESCE(excluded.zone_key, airbnb_listings.zone_key),
            lat = COALESCE(excluded.lat, airbnb_listings.lat),
            lon = COALESCE(excluded.lon, airbnb_listings.lon),
            is_demo = excluded.is_demo,
            extra_guest_fee_note = COALESCE(excluded.extra_guest_fee_note, airbnb_listings.extra_guest_fee_note),
            occ_14d_pct = COALESCE(excluded.occ_14d_pct, airbnb_listings.occ_14d_pct),
            occ_30d_pct = COALESCE(excluded.occ_30d_pct, airbnb_listings.occ_30d_pct),
            booked_14d_days = COALESCE(excluded.booked_14d_days, airbnb_listings.booked_14d_days),
            booked_30d_days = COALESCE(excluded.booked_30d_days, airbnb_listings.booked_30d_days),
            calendar_updated_at = COALESCE(excluded.calendar_updated_at, airbnb_listings.calendar_updated_at),
            thumbnail_url = COALESCE(excluded.thumbnail_url, airbnb_listings.thumbnail_url),
            last_seen_at = excluded.last_seen_at
        """,
        payload,
    )


def insert_price_snapshot(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    """객단가 스냅샷을 저장한다."""
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO price_snapshots (
            listing_id, snapshot_date, snapshot_type, check_in, check_out,
            currency, nightly_price, cleaning_fee, service_fee, total_price,
            is_available, collected_at
        ) VALUES (
            :listing_id, :snapshot_date, :snapshot_type, :check_in, :check_out,
            :currency, :nightly_price, :cleaning_fee, :service_fee, :total_price,
            :is_available, :collected_at
        )
        ON CONFLICT(listing_id, snapshot_date, snapshot_type) DO UPDATE SET
            check_in = excluded.check_in,
            check_out = excluded.check_out,
            currency = excluded.currency,
            nightly_price = COALESCE(excluded.nightly_price, price_snapshots.nightly_price),
            cleaning_fee = COALESCE(excluded.cleaning_fee, price_snapshots.cleaning_fee),
            service_fee = COALESCE(excluded.service_fee, price_snapshots.service_fee),
            total_price = COALESCE(excluded.total_price, price_snapshots.total_price),
            is_available = excluded.is_available,
            collected_at = excluded.collected_at
        """,
        {**row, "collected_at": now},
    )


def insert_reviews(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    """리뷰 목록을 중복 무시로 적재하고 추가된 건수를 반환한다."""
    now = utc_now_iso()
    inserted = 0
    for row in rows:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO reviews (
                listing_id, review_uid, reviewer_name, review_date,
                language_code, rating, comment, collected_at
            ) VALUES (
                :listing_id, :review_uid, :reviewer_name, :review_date,
                :language_code, :rating, :comment, :collected_at
            )
            """,
            {**row, "collected_at": now},
        )
        inserted += cur.rowcount
    return inserted


def upsert_customer_insight(conn: sqlite3.Connection, insight: Dict[str, Any]) -> None:
    """숙소별 고객층·리뷰 분석 결과를 저장한다."""
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO customer_insights (
            listing_id, analyzed_at, analysis_method, review_sample_count,
            language_breakdown_json, guest_segments_json, positioning_tags_json,
            strengths_json, weaknesses_json, benchmark_note
        ) VALUES (
            :listing_id, :analyzed_at, :analysis_method, :review_sample_count,
            :language_breakdown_json, :guest_segments_json, :positioning_tags_json,
            :strengths_json, :weaknesses_json, :benchmark_note
        )
        ON CONFLICT(listing_id) DO UPDATE SET
            analyzed_at = excluded.analyzed_at,
            analysis_method = excluded.analysis_method,
            review_sample_count = excluded.review_sample_count,
            language_breakdown_json = excluded.language_breakdown_json,
            guest_segments_json = excluded.guest_segments_json,
            positioning_tags_json = excluded.positioning_tags_json,
            strengths_json = excluded.strengths_json,
            weaknesses_json = excluded.weaknesses_json,
            benchmark_note = excluded.benchmark_note
        """,
        {
            "listing_id": insight["listing_id"],
            "analyzed_at": now,
            "analysis_method": insight["analysis_method"],
            "review_sample_count": insight["review_sample_count"],
            "language_breakdown_json": json.dumps(insight["language_breakdown"], ensure_ascii=False),
            "guest_segments_json": json.dumps(insight["guest_segments"], ensure_ascii=False),
            "positioning_tags_json": json.dumps(insight["positioning_tags"], ensure_ascii=False),
            "strengths_json": json.dumps(insight["strengths"], ensure_ascii=False),
            "weaknesses_json": json.dumps(insight["weaknesses"], ensure_ascii=False),
            "benchmark_note": insight.get("benchmark_note", ""),
        },
    )


def log_collection_run(
    conn: sqlite3.Connection,
    run_type: str,
    zone_key: Optional[str],
    status: str,
    records_count: int = 0,
    message: str = "",
    started_at: Optional[str] = None,
) -> None:
    """수집 실행 로그를 기록한다."""
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO collection_runs (
            run_type, zone_key, status, records_count, message, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_type, zone_key, status, records_count, message, started_at or now, now),
    )


def clear_demo_data(db_path: Path = DB_PATH) -> None:
    """데모용 샘플 데이터를 모두 제거한다."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM airbnb_listings WHERE is_demo = 1;")
        conn.execute("DELETE FROM public_businesses WHERE is_demo = 1;")
        conn.execute("DELETE FROM collection_runs WHERE run_type LIKE 'demo%';")


def flush_db_wal(db_path: Path = DB_PATH) -> None:
    """SQLite WAL 트랜잭션을 메인 DB 파일(.db)로 완전 병합(TRUNCATE)한다."""
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def push_files_to_github(
    files_map: Dict[str, bytes],
    commit_message: str = "데이터베이스 및 설정 자동 동기화",
    repo: Optional[str] = None,
    branch: str = "main",
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """GitHub Git Data REST API를 통해 대용량 바이너리(.db 포함) 및 파일들을 원격 저장소에 즉시 커밋·푸시한다."""
    gh_token = (token or get_env("GITHUB_TOKEN", "")).strip()
    gh_repo = (repo or get_env("GITHUB_REPO", "coolcider9961-creator/busan")).strip()
    if not gh_token:
        return {"synced": False, "reason": "GITHUB_TOKEN 미설정"}
    if not gh_repo or "/" not in gh_repo:
        return {"synced": False, "reason": f"유효하지 않은 GITHUB_REPO: {gh_repo}"}

    api_base = f"https://api.github.com/repos/{gh_repo}"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        ref_resp = requests.get(f"{api_base}/git/refs/heads/{branch}", headers=headers, timeout=15)
        ref_resp.raise_for_status()
        latest_commit_sha = ref_resp.json()["object"]["sha"]

        commit_resp = requests.get(
            f"{api_base}/git/commits/{latest_commit_sha}", headers=headers, timeout=15
        )
        commit_resp.raise_for_status()
        base_tree_sha = commit_resp.json()["tree"]["sha"]

        tree_items: List[Dict[str, Any]] = []
        blob_shas: Dict[str, str] = {}
        for rel_path, content_bytes in files_map.items():
            b64_content = base64.b64encode(content_bytes).decode("ascii")
            blob_resp = requests.post(
                f"{api_base}/git/blobs",
                headers=headers,
                json={"content": b64_content, "encoding": "base64"},
                timeout=60,
            )
            blob_resp.raise_for_status()
            blob_sha = blob_resp.json()["sha"]
            blob_shas[rel_path] = blob_sha
            tree_items.append(
                {
                    "path": rel_path.replace("\\", "/"),
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_sha,
                }
            )

        tree_resp = requests.post(
            f"{api_base}/git/trees",
            headers=headers,
            json={"base_tree": base_tree_sha, "tree": tree_items},
            timeout=30,
        )
        tree_resp.raise_for_status()
        new_tree_sha = tree_resp.json()["sha"]

        new_commit_resp = requests.post(
            f"{api_base}/git/commits",
            headers=headers,
            json={
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [latest_commit_sha],
            },
            timeout=20,
        )
        new_commit_resp.raise_for_status()
        new_commit_sha = new_commit_resp.json()["sha"]

        update_ref_resp = requests.patch(
            f"{api_base}/git/refs/heads/{branch}",
            headers=headers,
            json={"sha": new_commit_sha, "force": True},
            timeout=15,
        )
        update_ref_resp.raise_for_status()

        return {
            "synced": True,
            "repo": gh_repo,
            "branch": branch,
            "commit_sha": new_commit_sha,
            "blob_shas": blob_shas,
        }
    except Exception as exc:
        return {"synced": False, "reason": str(exc)}


def sync_db_to_github(
    commit_message: str = "데이터베이스 자동 동기화",
    db_path: Path = DB_PATH,
) -> Dict[str, Any]:
    """로컬/클라우드 SQLite 변경사항을 WAL 병합 후 GitHub 원격 저장소(data/busan_hostel_intel.db)에 실시간 반영한다."""
    if not db_path.exists():
        return {"synced": False, "reason": "DB 파일 없음"}

    flush_db_wal(db_path)
    db_bytes = db_path.read_bytes()
    db_sha256 = hashlib.sha256(db_bytes).hexdigest()
    now_iso = utc_now_iso()

    meta_payload: Dict[str, Any] = {
        "synced_at": now_iso,
        "db_sha256": db_sha256,
        "db_size": len(db_bytes),
        "message": commit_message,
    }
    try:
        LAST_SYNC_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_SYNC_META_PATH.write_text(
            json.dumps(meta_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    gh_token = get_env("GITHUB_TOKEN", "").strip()
    gh_repo = get_env("GITHUB_REPO", "coolcider9961-creator/busan").strip()
    if not gh_token:
        return {"synced": False, "reason": "GITHUB_TOKEN 미설정 (로컬 DB만 저장됨)"}

    # 1차로 DB Blob을 업로드해 blob_sha를 메타에 기록하고, 단일 커밋으로 DB와 last_sync.json을 함께 푸시
    api_base = f"https://api.github.com/repos/{gh_repo}"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        ref_resp = requests.get(f"{api_base}/git/refs/heads/main", headers=headers, timeout=15)
        ref_resp.raise_for_status()
        latest_commit_sha = ref_resp.json()["object"]["sha"]

        commit_resp = requests.get(
            f"{api_base}/git/commits/{latest_commit_sha}", headers=headers, timeout=15
        )
        commit_resp.raise_for_status()
        base_tree_sha = commit_resp.json()["tree"]["sha"]

        b64_db = base64.b64encode(db_bytes).decode("ascii")
        db_blob_resp = requests.post(
            f"{api_base}/git/blobs",
            headers=headers,
            json={"content": b64_db, "encoding": "base64"},
            timeout=60,
        )
        db_blob_resp.raise_for_status()
        db_blob_sha = db_blob_resp.json()["sha"]

        meta_payload["db_blob_sha"] = db_blob_sha
        meta_bytes = json.dumps(meta_payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            LAST_SYNC_META_PATH.write_bytes(meta_bytes)
        except Exception:
            pass

        b64_meta = base64.b64encode(meta_bytes).decode("ascii")
        meta_blob_resp = requests.post(
            f"{api_base}/git/blobs",
            headers=headers,
            json={"content": b64_meta, "encoding": "base64"},
            timeout=20,
        )
        meta_blob_resp.raise_for_status()
        meta_blob_sha = meta_blob_resp.json()["sha"]

        tree_resp = requests.post(
            f"{api_base}/git/trees",
            headers=headers,
            json={
                "base_tree": base_tree_sha,
                "tree": [
                    {
                        "path": "data/busan_hostel_intel.db",
                        "mode": "100644",
                        "type": "blob",
                        "sha": db_blob_sha,
                    },
                    {
                        "path": "data/last_sync.json",
                        "mode": "100644",
                        "type": "blob",
                        "sha": meta_blob_sha,
                    },
                ],
            },
            timeout=25,
        )
        tree_resp.raise_for_status()
        new_tree_sha = tree_resp.json()["sha"]

        new_commit_resp = requests.post(
            f"{api_base}/git/commits",
            headers=headers,
            json={
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [latest_commit_sha],
            },
            timeout=20,
        )
        new_commit_resp.raise_for_status()
        new_commit_sha = new_commit_resp.json()["sha"]

        update_ref_resp = requests.patch(
            f"{api_base}/git/refs/heads/main",
            headers=headers,
            json={"sha": new_commit_sha, "force": True},
            timeout=15,
        )
        update_ref_resp.raise_for_status()

        return {
            "synced": True,
            "commit_sha": new_commit_sha,
            "db_sha256": db_sha256,
            "synced_at": now_iso,
        }
    except Exception as exc:
        return {"synced": False, "reason": str(exc)}


def pull_latest_db_from_github_if_newer(
    db_path: Path = DB_PATH,
    force: bool = False,
) -> Dict[str, Any]:
    """원격 GitHub 저장소의 최신 DB 메타데이터(data/last_sync.json)를 확인해 더 최신이면 즉시 로컬/클라우드 DB를 갱신한다."""
    global _LAST_GITHUB_PULL_CHECK_TS

    if db_path != DB_PATH:
        return {"updated": False, "reason": "non-default db"}

    now_ts = time.time()
    if not force and (now_ts - _LAST_GITHUB_PULL_CHECK_TS) < 12.0:
        return {"updated": False, "reason": "throttled"}
    _LAST_GITHUB_PULL_CHECK_TS = now_ts

    gh_repo = get_env("GITHUB_REPO", "coolcider9961-creator/busan").strip()
    gh_token = get_env("GITHUB_TOKEN", "").strip()
    if not gh_repo or "/" not in gh_repo:
        return {"updated": False, "reason": "invalid repo"}

    headers: Dict[str, str] = {
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    api_base = f"https://api.github.com/repos/{gh_repo}"
    try:
        meta_resp = requests.get(
            f"{api_base}/contents/data/last_sync.json?ref=main",
            headers=headers,
            timeout=3.5,
        )
        if meta_resp.status_code != 200:
            return {"updated": False, "reason": f"meta status {meta_resp.status_code}"}
        remote_meta = meta_resp.json()
        remote_sha = str(remote_meta.get("db_sha256", "")).strip()
        remote_blob_sha = str(remote_meta.get("db_blob_sha", "")).strip()
        remote_synced_at = str(remote_meta.get("synced_at", "")).strip()
        if not remote_sha or not remote_blob_sha:
            return {"updated": False, "reason": "incomplete remote meta"}

        local_synced_at = ""
        local_sha = ""
        if LAST_SYNC_META_PATH.exists():
            try:
                local_meta = json.loads(LAST_SYNC_META_PATH.read_text(encoding="utf-8"))
                local_synced_at = str(local_meta.get("synced_at", "")).strip()
                local_sha = str(local_meta.get("db_sha256", "")).strip()
            except Exception:
                pass
        if not local_sha and db_path.exists():
            flush_db_wal(db_path)
            local_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()

        if remote_sha == local_sha:
            return {"updated": False, "reason": "already up-to-date", "synced_at": remote_synced_at}
        if local_synced_at and remote_synced_at and remote_synced_at <= local_synced_at:
            return {"updated": False, "reason": "local is newer or equal", "synced_at": local_synced_at}

        blob_resp = requests.get(
            f"{api_base}/git/blobs/{remote_blob_sha}",
            headers=headers,
            timeout=30.0,
        )
        blob_resp.raise_for_status()
        new_db_bytes = blob_resp.content
        if hashlib.sha256(new_db_bytes).hexdigest() != remote_sha:
            return {"updated": False, "reason": "sha256 mismatch"}

        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 기존 WAL/SHM 임시파일이 있으면 정리 후 원자적 교체
        for suffix in ("-wal", "-shm"):
            wal_file = db_path.with_name(db_path.name + suffix)
            if wal_file.exists():
                try:
                    wal_file.unlink()
                except Exception:
                    pass
        tmp_path = db_path.with_suffix(".db.tmp")
        tmp_path.write_bytes(new_db_bytes)
        tmp_path.replace(db_path)
        LAST_SYNC_META_PATH.write_text(
            json.dumps(remote_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"updated": True, "synced_at": remote_synced_at, "db_sha256": remote_sha}
    except Exception as exc:
        return {"updated": False, "reason": str(exc)}
