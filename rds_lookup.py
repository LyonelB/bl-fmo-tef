"""
BL-FMO-LITE — rds_lookup.py
RDS station lookup depuis rds-station-db (GitHub).
2 changements vs BL-FMO : USER_AGENT et CACHE_DIR.
"""

import json
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path

DB_BASE_URL = "https://raw.githubusercontent.com/LyonelB/rds-station-db/main/data"
CACHE_DIR   = Path.home() / ".cache" / "bl-fmo-tef" / "rds-db"   # ← adapté
CACHE_TTL_SECONDS = 24 * 3600
USER_AGENT  = "bl-fmo-tef/0.1 (https://github.com/LyonelB/bl-fmo-tef)"  # ← adapté


class RDSLookup:
    """Thread-safe RDS station database lookup with local cache."""

    def __init__(self, country: str = "FR", auto_refresh: bool = True):
        self.country = country.upper()
        self._lock   = threading.Lock()
        self._by_pi: dict[str, dict]      = {}
        self._by_ps: dict[str, dict]      = {}
        self._by_pi_ps: dict[tuple, dict] = {}
        self._loaded = False

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cache_file = CACHE_DIR / f"{self.country}.json"

        self._load(force_refresh=False)

        if auto_refresh:
            threading.Thread(target=self._background_refresh, daemon=True).start()

    # ── Public API ──────────────────────────────────────────────────────

    def get_by_pi(self, pi: str) -> dict | None:
        return self._by_pi.get(pi.upper())

    def get_by_ps(self, ps: str) -> dict | None:
        return self._by_ps.get(ps.replace('_', ' ').strip().upper())

    def get_by_pi_ps(self, pi: str, ps: str) -> dict | None:
        return self._by_pi_ps.get((pi.upper(), ps.replace('_', ' ').strip().upper()))

    def get(self, pi: str | None = None, ps: str | None = None) -> dict | None:
        if pi and ps:
            result = self.get_by_pi_ps(pi, ps)
            if result:
                return result
        if pi:
            result = self.get_by_pi(pi)
            if result:
                return result
        if ps:
            return self.get_by_ps(ps)
        return None

    def station_count(self) -> int:
        return len(self._by_pi)

    def force_refresh(self):
        self._load(force_refresh=True)

    # ── Internal ────────────────────────────────────────────────────────

    def _load(self, force_refresh: bool = False):
        data = None
        if not force_refresh and self._cache_file.exists():
            age = time.time() - self._cache_file.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                try:
                    with open(self._cache_file) as f:
                        data = json.load(f)
                except Exception:
                    data = None

        if data is None:
            data = self._fetch_remote()
            if data:
                try:
                    with open(self._cache_file, "w") as f:
                        json.dump(data, f, ensure_ascii=False)
                except Exception:
                    pass

        if data:
            self._index(data)

    def _fetch_remote(self) -> dict | None:
        url = f"{DB_BASE_URL}/{self.country}.json"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404 and self._cache_file.exists():
                with open(self._cache_file) as f:
                    return json.load(f)
            return None
        except Exception:
            if self._cache_file.exists():
                try:
                    with open(self._cache_file) as f:
                        return json.load(f)
                except Exception:
                    pass
            return None

    def _index(self, data: dict):
        by_pi, by_ps, by_pi_ps = {}, {}, {}
        for station in data.get("stations", []):
            pi = station.get("pi", "").upper()
            ps = station.get("ps", "").strip().upper()
            if pi:
                if pi not in by_pi or station.get("logo_url"):
                    by_pi[pi] = station
            if ps:
                if ps not in by_ps or station.get("logo_url"):
                    by_ps[ps] = station
            if pi and ps:
                by_pi_ps[(pi, ps)] = station
        with self._lock:
            self._by_pi    = by_pi
            self._by_ps    = by_ps
            self._by_pi_ps = by_pi_ps
            self._loaded   = True

    def _background_refresh(self):
        while True:
            time.sleep(3600)
            if self._cache_file.exists():
                age = time.time() - self._cache_file.stat().st_mtime
                if age >= CACHE_TTL_SECONDS:
                    self._load(force_refresh=True)


# ── Singleton ───────────────────────────────────────────────────────────

_default_lookup: RDSLookup | None = None

def get_lookup(country: str = "FR") -> RDSLookup:
    global _default_lookup
    if _default_lookup is None or _default_lookup.country != country.upper():
        _default_lookup = RDSLookup(country=country)
    return _default_lookup


if __name__ == "__main__":
    import sys
    country = sys.argv[1] if len(sys.argv) > 1 else "FR"
    query   = sys.argv[2] if len(sys.argv) > 2 else None
    lookup  = RDSLookup(country=country)
    print(f"Loaded {lookup.station_count()} stations for {country}")
    if query:
        result = lookup.get_by_pi(query) or lookup.get_by_ps(query)
        print(json.dumps(result, indent=2, ensure_ascii=False) if result else f"Not found: {query}")
    else:
        for pi, s in sorted(lookup._by_pi.items()):
            print(f"  {pi}  {s.get('ps',''):8s}  {s.get('name','')}  {'🖼' if s.get('logo_url') else '·'}")
