"""Version-bump classification.

Package versions come in many schemes. We only need a coarse answer, "how big
is this jump?", so we strip the packaging-specific parts (epoch, distro
revision) and compare the leading numeric release components.
"""
from __future__ import annotations

import re
from typing import Tuple

from .models import Bump

_RELEASE = re.compile(r"^[vV]?(\d+(?:[._]\d+)*)")


def upstream(version: str, scheme: str = "generic") -> str:
    """Strip epoch and packaging revision, leaving the upstream version."""
    v = version.strip()
    v = re.sub(r"^\d+:", "", v)  # deb/rpm epoch
    if scheme == "pep440":
        v = re.sub(r"^\d+!", "", v)  # PEP 440 epoch
    elif scheme == "deb" and "-" in v:
        v = v.rsplit("-", 1)[0]  # debian revision
    elif scheme == "rpm" and "-" in v:
        v = v.split("-", 1)[0]  # rpm release
    return v


def release_tuple(version: str) -> Tuple[int, ...]:
    m = _RELEASE.match(version)
    if not m:
        return ()
    return tuple(int(p) for p in re.split(r"[._]", m.group(1)))


def classify_bump(old: str, new: str, scheme: str = "generic") -> Bump:
    """Classify the jump from ``old`` to ``new``.

    A change only in the packaging revision (same upstream version) counts as a
    patch. Anything we cannot parse, or a version going backwards, is UNKNOWN.
    """
    a = release_tuple(upstream(old, scheme))
    b = release_tuple(upstream(new, scheme))
    if not a or not b:
        return Bump.UNKNOWN
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    if a == b:
        return Bump.PATCH
    if b < a:
        return Bump.UNKNOWN
    if a[0] != b[0]:
        return Bump.MAJOR
    if n > 1 and a[1] != b[1]:
        return Bump.MINOR
    return Bump.PATCH
