"""Optional vulnerability enrichment from OSV.dev (https://osv.dev).

Used for ecosystems whose package manager has no native security metadata
(currently pip). apt and dnf already tell us which updates are security fixes.

Only package names and versions are sent. Disable with ``osv_enabled = false``.
Any network failure returns None and is logged; it never fails a run.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
CHUNK = 500


def _post(url: str, payload: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": f"smart-updater/{__version__}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - fixed https URL
        return json.loads(resp.read().decode("utf-8"))


def query_batch(
    items: List[Tuple[str, str]], ecosystem: str, post: Optional[Callable[[str, dict], dict]] = None
) -> Optional[Dict[Tuple[str, str], List[str]]]:
    """Return {(name, version): [advisory ids]}, or None if OSV could not be reached."""
    post = post or _post
    out: Dict[Tuple[str, str], List[str]] = {}
    try:
        for i in range(0, len(items), CHUNK):
            chunk = items[i : i + CHUNK]
            body = {"queries": [{"package": {"name": n, "ecosystem": ecosystem}, "version": v} for n, v in chunk]}
            results = post(OSV_BATCH_URL, body).get("results", [])
            for (n, v), res in zip(chunk, results):
                out[(n, v)] = [x["id"] for x in res.get("vulns", [])]
    except Exception:
        return None
    return out


def classify(
    ecosystem: str,
    installed: Dict[str, str],
    updates,
    post: Optional[Callable[[str, dict], dict]] = None,
):
    """Mark updates that fix a known vulnerability; list vulnerabilities with no fix.

    Returns (no_fix, ok). ``no_fix`` is a list of dicts. ``ok`` is False when OSV
    could not be reached, in which case nothing was changed.
    """
    res = query_batch(list(installed.items()), ecosystem, post)
    if res is None:
        return [], False
    vulnerable = {n: res.get((n, v), []) for n, v in installed.items() if res.get((n, v))}
    if not vulnerable:
        return [], True
    by_name = {u.name: u for u in updates}
    cand_items = [(by_name[n].name, by_name[n].candidate) for n in vulnerable if n in by_name]
    cand_res = query_batch(cand_items, ecosystem, post) if cand_items else {}
    if cand_res is None:
        return [], False
    no_fix = []
    for n, ids in vulnerable.items():
        u = by_name.get(n)
        if u is not None and not cand_res.get((u.name, u.candidate)):
            u.security, u.advisories = True, ids
        else:
            no_fix.append(
                {"name": n, "installed": installed[n], "candidate": u.candidate if u else "", "advisories": ids}
            )
    return no_fix, True
