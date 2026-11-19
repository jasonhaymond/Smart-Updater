import pytest

from smart_updater.models import Bump
from smart_updater.version import classify_bump


@pytest.mark.parametrize(
    "old,new,scheme,expected",
    [
        ("3.0.13-0ubuntu3.4", "3.0.13-0ubuntu3.5", "deb", Bump.PATCH),  # distro revision only
        ("1:3.0.13-0ubuntu3.4", "1:3.0.14-0ubuntu1", "deb", Bump.PATCH),
        ("2.39-0ubuntu8", "2.40-1", "deb", Bump.MINOR),
        ("115.2.0esr-1", "128.0esr-1", "deb", Bump.MAJOR),
        ("8.9p1-3ubuntu0.10", "8.9p1-3ubuntu0.11", "deb", Bump.PATCH),
        ("2024a-0ubuntu0.24.04", "2024b-0ubuntu0.24.04", "deb", Bump.PATCH),
        ("2024b-1", "2025a-1", "deb", Bump.MAJOR),
        ("3.2.1-1.fc40", "3.2.2-1.fc40", "rpm", Bump.PATCH),
        ("1:3.1.0-1.fc40", "1:3.2.0-1.fc40", "rpm", Bump.MINOR),
        ("2.31.0", "2.32.3", "pep440", Bump.MINOR),
        ("1!2.0.0", "1!3.0.0", "pep440", Bump.MAJOR),
        ("23.01", "24.08", "generic", Bump.MAJOR),
        ("2.44.0", "2.44.1", "generic", Bump.PATCH),
        ("1.0", "1.0.1", "generic", Bump.PATCH),
        ("2.0", "1.0", "generic", Bump.UNKNOWN),  # going backwards
        ("abc", "def", "generic", Bump.UNKNOWN),
    ],
)
def test_classify(old, new, scheme, expected):
    assert classify_bump(old, new, scheme) == expected
