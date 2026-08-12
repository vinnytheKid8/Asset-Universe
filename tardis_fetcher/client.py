"""Low-level Tardis HTTP clients: metadata API + CSV dataset streaming.

Only the *fetch* concerns live here: URL construction, auth, retry/backoff,
streaming gzip decode, on-disk cache. No business logic.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from .config import API_ENDPOINT, DATASETS_ENDPOINT

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 6


class TardisError(RuntimeError):
    pass


class TardisAuthError(TardisError):
    """401/403 – key missing, invalid, or plan does not cover the request."""


class TardisNotSubscribed(TardisError):
    """Endpoint requires a higher plan (e.g. instruments metadata = pro/business)."""


@dataclass
class TardisConfig:
    api_key: str = ""
    cache_dir: Path = Path("./tardis_cache")
    api_endpoint: str = API_ENDPOINT
    datasets_endpoint: str = DATASETS_ENDPOINT
    timeout: int = 900
    keep_raw: bool = False   # True = persist .csv.gz to cache_dir, False = stream only

    @classmethod
    def from_env(cls, **kw) -> "TardisConfig":
        return cls(api_key=os.environ.get("TARDIS_API_KEY", ""), **kw)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


# --------------------------------------------------------------------------
# metadata API
# --------------------------------------------------------------------------
class TardisMetadata:
    """Wraps GET /v1/exchanges, /v1/exchanges/{id}, /v1/instruments/{id}."""

    def __init__(self, cfg: TardisConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.s = session or requests.Session()

    def _get(self, path: str, params: dict | None = None, auth: bool = False):
        url = f"{self.cfg.api_endpoint}{path}"
        r = self.s.get(url, params=params, headers=self.cfg.headers if auth else {}, timeout=60)
        if r.status_code == 200:
            return r.json()
        body = _safe_json(r)
        msg = body.get("message", r.text[:300]) if isinstance(body, dict) else r.text[:300]
        if "subscription" in msg.lower():
            raise TardisNotSubscribed(f"{url}: {msg}")
        if r.status_code in (401, 403):
            raise TardisAuthError(f"{url}: {msg}")
        raise TardisError(f"{url}: HTTP {r.status_code} {msg}")

    def exchanges(self) -> list[dict]:
        return self._get("/exchanges")

    def exchange_details(self, exchange: str) -> dict:
        """Authoritative per-symbol dataset coverage.

        details["datasets"] = {formats, exportedFrom, exportedUntil,
                               symbols: [{id, type, availableSince, availableTo, dataTypes[]}]}
        """
        return self._get(f"/exchanges/{exchange}")

    def dataset_symbols(self, exchange: str, data_type: str | None = None,
                        include_grouped: bool = False) -> list[dict]:
        d = self.exchange_details(exchange).get("datasets", {}) or {}
        out = []
        for s in d.get("symbols", []):
            if not include_grouped and s["id"] in {
                "SPOT", "FUTURES", "PERPETUALS", "OPTIONS", "COMBOS", "PREDICTIONS"}:
                continue
            if data_type and data_type not in s.get("dataTypes", []):
                continue
            out.append(s)
        return out

    def exported_until(self, exchange: str) -> str | None:
        return (self.exchange_details(exchange).get("datasets") or {}).get("exportedUntil")

    # --- pro/business plans only -----------------------------------------
    def instruments(self, exchange: str, filter_: dict | None = None) -> list[dict]:
        params = {"filter": json.dumps(filter_, separators=(",", ":"))} if filter_ else None
        return self._get(f"/instruments/{exchange}", params=params, auth=True)

    def instrument(self, exchange: str, symbol: str) -> dict:
        return self._get(f"/instruments/{exchange}/{symbol}", auth=True)

    def api_key_info(self) -> list[dict]:
        return self._get("/api-key-info", auth=True)


def _safe_json(r: requests.Response):
    try:
        return r.json()
    except Exception:
        return None


# --------------------------------------------------------------------------
# CSV datasets
# --------------------------------------------------------------------------
class TardisDatasets:
    """Downloader / streamer for https://datasets.tardis.dev/v1/... .csv.gz"""

    def __init__(self, cfg: TardisConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self.s = session or requests.Session()
        self.cfg.cache_dir = Path(self.cfg.cache_dir)

    def url(self, exchange: str, data_type: str, day: date, symbol: str) -> str:
        return (f"{self.cfg.datasets_endpoint}/{exchange}/{data_type}/"
                f"{day:%Y/%m/%d}/{symbol}.csv.gz")

    def cache_path(self, exchange: str, data_type: str, day: date, symbol: str) -> Path:
        return self.cfg.cache_dir / exchange / data_type / f"{day:%Y-%m-%d}" / f"{symbol}.csv.gz"

    # -- raw byte fetch ----------------------------------------------------
    def _request(self, url: str) -> requests.Response:
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                r = self.s.get(url, headers=self.cfg.headers, stream=True,
                               timeout=self.cfg.timeout)
            except requests.RequestException as e:      # network flake
                last = e
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 200:
                return r
            if r.status_code in RETRY_STATUS:
                wait = float(r.headers.get("Retry-After", min(2 ** attempt, 30)))
                log.warning("tardis %s -> %s, retry in %.1fs", url, r.status_code, wait)
                r.close()
                time.sleep(wait)
                continue
            body = _safe_json(r)
            msg = body.get("message", r.text[:300]) if isinstance(body, dict) else r.text[:300]
            r.close()
            if r.status_code in (401, 403):
                raise TardisAuthError(f"{url}: {msg}")
            if r.status_code == 404:
                raise TardisError(f"{url}: 404 (bad exchange/dataType/symbol) {msg}")
            raise TardisError(f"{url}: HTTP {r.status_code} {msg}")
        raise TardisError(f"{url}: exhausted retries ({last})")

    def download(self, exchange: str, data_type: str, day: date, symbol: str,
                 skip_if_exists: bool = True) -> Path:
        """Persist one daily .csv.gz to the local cache and return its path."""
        path = self.cache_path(exchange, data_type, day, symbol)
        if skip_if_exists and path.exists() and path.stat().st_size > 0:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".gz.part")
        r = self._request(self.url(exchange, data_type, day, symbol))
        with r, open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        tmp.replace(path)
        return path

    def stream_rows(self, exchange: str, data_type: str, day: date, symbol: str
                    ) -> Iterator[dict[str, str]]:
        """Yield CSV rows as dicts without ever holding the file in memory.

        Grouped files (PERPETUALS / SPOT) are 100 MB - 2 GB gzipped per day, so
        this is the only viable access pattern for whole-exchange pulls.
        An empty gzip (no data for that symbol/day) yields nothing.
        """
        if self.cfg.keep_raw:
            path = self.download(exchange, data_type, day, symbol)
            with gzip.open(path, "rt", newline="") as fh:
                yield from csv.DictReader(fh)
            return
        r = self._request(self.url(exchange, data_type, day, symbol))
        with r:
            raw = r.raw
            raw.decode_content = False          # server sends real .gz bytes
            with gzip.GzipFile(fileobj=raw) as gz:
                text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
                yield from csv.DictReader(text)

    def probe(self, exchange: str, data_type: str, day: date, symbol: str,
              nbytes: int = 1 << 16) -> tuple[int, bool]:
        """Cheap availability check: (http_status, has_bytes).

        NOTE: the datasets host sits behind Cloudflare, which rejects both HEAD
        and `Range:` requests with 403. The only reliable probe is a normal GET
        that we abort after the first chunk.
        """
        url = self.url(exchange, data_type, day, symbol)
        try:
            r = self.s.get(url, headers=self.cfg.headers, stream=True, timeout=120)
        except requests.RequestException:
            return 0, False
        with r:
            if r.status_code != 200:
                return r.status_code, False
            chunk = next(r.iter_content(nbytes), b"")
        # an "empty" gzip (no data that day) is ~20-30 bytes
        return 200, len(chunk) > 64

    def exists(self, exchange: str, data_type: str, day: date, symbol: str) -> bool:
        return self.probe(exchange, data_type, day, symbol)[1]
