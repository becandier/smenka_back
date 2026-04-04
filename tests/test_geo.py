import pytest

from src.app.utils.geo import haversine_distance, is_within_radius


class TestHaversineDistance:
    def test_same_point(self):
        assert haversine_distance(55.7558, 37.6173, 55.7558, 37.6173) == 0.0

    def test_moscow_to_spb(self):
        # Moscow (55.7558, 37.6173) to St Petersburg (59.9343, 30.3351)
        # Expected ~634 km
        dist = haversine_distance(55.7558, 37.6173, 59.9343, 30.3351)
        assert 630_000 < dist < 640_000  # meters

    def test_short_distance(self):
        # Two points ~100m apart in Moscow
        lat1, lon1 = 55.7558, 37.6173
        lat2, lon2 = 55.7567, 37.6173  # ~100m north
        dist = haversine_distance(lat1, lon1, lat2, lon2)
        assert 90 < dist < 110


class TestIsWithinRadius:
    def test_within_radius(self):
        assert is_within_radius(55.7558, 37.6173, 55.7558, 37.6173, 100) is True

    def test_outside_radius(self):
        assert is_within_radius(55.7558, 37.6173, 55.7600, 37.6173, 100) is False

    def test_on_boundary(self):
        # ~100m apart, radius 150m — should be within
        assert is_within_radius(55.7558, 37.6173, 55.7567, 37.6173, 150) is True
