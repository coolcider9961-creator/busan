"""전역 설정 및 부산 주요 상권(권역) 정의."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "busan_hostel_intel.db"
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

INDUSTRY_HOSTEL = "호스텔업"
INDUSTRY_URBAN_MINBAK = "외국인관광도시민박업"
TARGET_INDUSTRIES = (INDUSTRY_HOSTEL, INDUSTRY_URBAN_MINBAK)


@dataclass(frozen=True)
class ZoneConfig:
    """부산 상권 권역 설정."""

    key: str
    name: str
    sigungu: str
    center_lat: float
    center_lon: float
    radius_km: float
    airbnb_query: str
    dong_keywords: tuple[str, ...]


ZONES: Dict[str, ZoneConfig] = {
    "seomyeon": ZoneConfig(
        key="seomyeon",
        name="서면 (부전·전포)",
        sigungu="부산진구",
        center_lat=35.1578,
        center_lon=129.0591,
        radius_km=1.8,
        airbnb_query="Seomyeon, Busanjin-gu, Busan, South Korea",
        dong_keywords=("부전동", "전포동", "부전1동", "부전2동", "전포1동", "전포2동", "범천동", "양정동"),
    ),
    "busan_station": ZoneConfig(
        key="busan_station",
        name="부산역·초량",
        sigungu="동구",
        center_lat=35.1152,
        center_lon=129.0422,
        radius_km=1.8,
        airbnb_query="Busan Station, Dong-gu, Busan, South Korea",
        dong_keywords=("초량동", "수정동", "범일동", "좌천동"),
    ),
    "nampo": ZoneConfig(
        key="nampo",
        name="남포·자갈치",
        sigungu="중구",
        center_lat=35.0985,
        center_lon=129.0303,
        radius_km=1.8,
        airbnb_query="Nampo-dong, Jung-gu, Busan, South Korea",
        dong_keywords=("남포동", "광복동", "중앙동", "부평동", "보수동", "대청동", "동광동", "영주동"),
    ),
    "gwangalli": ZoneConfig(
        key="gwangalli",
        name="광안리",
        sigungu="수영구",
        center_lat=35.1532,
        center_lon=129.1187,
        radius_km=2.0,
        airbnb_query="Gwangalli Beach, Suyeong-gu, Busan, South Korea",
        dong_keywords=("광안동", "민락동", "남천동", "수영동"),
    ),
    "haeundae": ZoneConfig(
        key="haeundae",
        name="해운대",
        sigungu="해운대구",
        center_lat=35.1587,
        center_lon=129.1604,
        radius_km=2.2,
        airbnb_query="Haeundae Beach, Busan, South Korea",
        dong_keywords=("우동", "중동", "좌동", "송정동"),
    ),
}


def get_env(name: str, default: str = "") -> str:
    """환경변수를 최신 .env(또는 Streamlit Cloud secrets) 기준으로 조회한다."""
    load_dotenv(ENV_PATH, override=True)
    val = os.getenv(name, "").strip()
    if val:
        return val
    try:
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name]).strip()
    except Exception:
        pass
    return default


def verify_access_password(input_pw: str) -> Optional[str]:
    """입력한 비밀번호를 검증해 권한('admin' | 'viewer' | None)을 반환한다."""
    pw = (input_pw or "").strip()
    admin_pw = get_env("ADMIN_PASSWORD", "1234")
    viewer_pw = get_env("VIEWER_PASSWORD", "")

    if admin_pw and pw == admin_pw:
        return "admin"
    if viewer_pw and pw == viewer_pw:
        return "viewer"
    return None


def save_env_values(updates: Dict[str, str]) -> None:
    """사용자 설정 화면에서 입력한 환경변수를 .env 파일에 저장한다."""
    existing: Dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            existing[k.strip()] = v.strip()

    for k, v in updates.items():
        existing[k] = v.strip()

    lines: List[str] = [f"{k}={v}" for k, v in existing.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    load_dotenv(ENV_PATH, override=True)
