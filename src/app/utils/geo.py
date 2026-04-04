import math

EARTH_RADIUS_METERS = 6_371_000


def haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Calculate distance between two points in meters using Haversine formula."""
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS_METERS * c


def is_within_radius(
    lat1: float, lon1: float, lat2: float, lon2: float, radius_meters: int,
) -> bool:
    """Check if distance between two points is within given radius."""
    return haversine_distance(lat1, lon1, lat2, lon2) <= radius_meters
