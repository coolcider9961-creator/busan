"""좌표 변환(TM -> WGS84) 및 부산 상권 권역 매핑 유틸리티."""

from __future__ import annotations

import math
import re
from typing import Optional, Tuple

from src.config import ZONES

try:
    from pyproj import Transformer

    # LOCALDATA 표준 좌표계: 보정계수 없는 Bessel 중부원점 TM (EPSG:5174) -> WGS84 (EPSG:4326)
    _TM_TO_WGS84 = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)
except Exception:  # pragma: no cover
    _TM_TO_WGS84 = None


def tm_to_wgs84(x: Optional[float], y: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """LOCALDATA TM 좌표(x, y)를 위경도(lat, lon)로 변환한다."""
    if x is None or y is None:
        return None, None
    try:
        x_val = float(x)
        y_val = float(y)
    except (TypeError, ValueError):
        return None, None

    if x_val == 0 or y_val == 0:
        return None, None

    # 이미 위경도 범위인 경우 그대로 반환
    if 124.0 <= x_val <= 132.0 and 32.0 <= y_val <= 39.0:
        return round(y_val, 6), round(x_val, 6)
    if 32.0 <= x_val <= 39.0 and 124.0 <= y_val <= 132.0:
        return round(x_val, 6), round(y_val, 6)

    if _TM_TO_WGS84 is None:
        return None, None

    lon, lat = _TM_TO_WGS84.transform(x_val, y_val)
    if not (32.0 <= lat <= 39.0 and 124.0 <= lon <= 132.0):
        return None, None
    return round(lat, 6), round(lon, 6)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 위경도 좌표 간 직선 거리(km)를 계산한다."""
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parse_sigungu_dong(address: str) -> Tuple[Optional[str], Optional[str]]:
    """주소 문자열에서 시군구명과 동/읍/면명을 추출한다."""
    if not address:
        return None, None
    sigungu_match = re.search(r"부산(?:광역시)?\s+([가-힣]+구|[가-힣]+군)", address)
    sigungu = sigungu_match.group(1) if sigungu_match else None
    dong_match = re.search(r"([가-힣0-9]+(?:동|읍|면|가))(?:\s|\(|,|$)", address)
    dong = dong_match.group(1) if dong_match else None
    return sigungu, dong


def assign_zone(
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    address: str = "",
) -> Optional[str]:
    """좌표 또는 주소를 기준으로 부산 상권 권역 키를 반환한다."""
    if lat is not None and lon is not None:
        best_key: Optional[str] = None
        best_dist = float("inf")
        for key, zone in ZONES.items():
            dist = haversine_km(lat, lon, zone.center_lat, zone.center_lon)
            if dist <= zone.radius_km and dist < best_dist:
                best_dist = dist
                best_key = key
        if best_key:
            return best_key

    if address:
        for key, zone in ZONES.items():
            if any(dong in address for dong in zone.dong_keywords):
                return key
        for key, zone in ZONES.items():
            if zone.sigungu in address:
                return key

    return None
