"""Provision mqtt lockers (шкафы) in the backend, each with a generated address.

The relay device double (:mod:`scud_relay.device`) only speaks the broker protocol;
it never creates locker records. Before a device can connect, a matching locker must
exist in ``locker_api`` (``POST /api/v1/lockers`` with ``transport=mqtt`` + a unique
``relay_id``). This module fills that gap for whole fleets: it authenticates against
the Apache gateway and creates ``count`` lockers, each carrying a realistic
``location_address`` + coordinates so they show up on a map instead of as blank rows.

stdlib-only (``urllib`` + a tiny cookie store) — the emulator keeps its runtime
dependency surface minimal (``gmqtt`` is the sole hard dep); provisioning needs no
extra package.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_LOGIN_PATH = "/api/v1/auth/login"
_LOCKERS_PATH = "/api/v1/lockers"

_HTTP_OK = 200
_HTTP_CREATED = 201
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_CONFLICT = 409

_ACCESS_COOKIE = "access_token"
_REQUEST_TIMEOUT_S = 30


@dataclass(frozen=True, slots=True)
class Location:
    """A generated шкаф location: human address + map coordinates."""

    address: str
    lat: float
    lon: float


# Curated (city, street, base-lat, base-lon) anchor points across several Russian
# cities. The fleet index selects an anchor, derives a building number, and adds a
# small deterministic coordinate offset so every generated шкаф lands at a distinct,
# plausible address and map marker (no two devices stack on the exact same pin).
_PLACES: tuple[tuple[str, str, float, float], ...] = (
    ("Москва", "ул. Тверская", 55.7649, 37.6055),
    ("Москва", "Ленинградский просп.", 55.7900, 37.5470),
    ("Москва", "Кутузовский просп.", 55.7400, 37.5340),
    ("Москва", "Профсоюзная ул.", 55.6720, 37.5620),
    ("Санкт-Петербург", "Невский просп.", 59.9320, 30.3480),
    ("Санкт-Петербург", "Московский просп.", 59.8880, 30.3190),
    ("Санкт-Петербург", "просп. Стачек", 59.8790, 30.2680),
    ("Екатеринбург", "просп. Ленина", 56.8380, 60.5970),
    ("Новосибирск", "Красный просп.", 55.0410, 82.9180),
    ("Казань", "ул. Баумана", 55.7940, 49.1090),
    ("Нижний Новгород", "Большая Покровская ул.", 56.3220, 44.0020),
    ("Ростов-на-Дону", "Большая Садовая ул.", 47.2220, 39.7180),
)


def generate_location(index: int) -> Location:
    """Deterministically map a fleet index → a distinct, realistic шкаф location.

    Same index always yields the same location (so re-runs are reproducible), while
    distinct indices spread across streets, building numbers and map pins.
    """
    if index < 0:
        raise ValueError("index must be non-negative")
    city, street, lat0, lon0 = _PLACES[index % len(_PLACES)]
    building = index // len(_PLACES) + 1
    # Sub-degree jitter (~±0.01° ≈ ±1 km), derived from the index (no RNG → stable
    # across runs). Coprime multipliers keep lat/lon offsets from moving in lockstep.
    lat = round(lat0 + ((index * 37) % 201 - 100) / 10000.0, 6)
    lon = round(lon0 + ((index * 53) % 201 - 100) / 10000.0, 6)
    address = f"{city}, {street}, д. {building}"
    return Location(address=address, lat=lat, lon=lon)


def build_locker_payload(relay_id: int, index: int) -> dict[str, Any]:
    """Build the ``POST /api/v1/lockers`` body for one mqtt шкаф with an address."""
    loc = generate_location(index)
    return {
        "inventory_number": f"RELAY-{relay_id}",
        "transport": "mqtt",
        "relay_id": relay_id,
        "location_address": loc.address,
        "location_lat": loc.lat,
        "location_lon": loc.lon,
    }


def _safe_json(raw: bytes | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class GatewaySession:
    """Minimal cookie-authenticated HTTP client for the SCUD gateway (stdlib only).

    Either :meth:`login` (phone + password of a 2FA-free administrator/operator) or
    :meth:`set_access_token` (an existing ``access_token`` cookie value) primes the
    session; both leave the ``access_token`` cookie set for subsequent calls.
    """

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self._cookies: dict[str, str] = {}

    def _request(self, method: str, path: str,
                 body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        req = urllib.request.Request(  # noqa: S310 - fixed http(s) gateway URL
            f"{self._base}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:  # noqa: S310
                self._store_cookies(resp)
                return resp.status, _safe_json(resp.read())
        except urllib.error.HTTPError as exc:
            self._store_cookies(exc)
            return exc.code, _safe_json(exc.read())

    def _store_cookies(self, resp: Any) -> None:
        for header in resp.headers.get_all("Set-Cookie") or []:
            pair = header.split(";", 1)[0]
            if "=" in pair:
                name, value = pair.split("=", 1)
                self._cookies[name.strip()] = value.strip()

    def login(self, phone: str, password: str) -> None:
        status, body = self._request("POST", _LOGIN_PATH,
                                     {"phone": phone, "password": password})
        if status != _HTTP_OK or _ACCESS_COOKIE not in self._cookies:
            raise RuntimeError(
                f"gateway login failed (status={status}); provisioning needs a "
                f"2FA-free administrator/operator account: {body}")

    def set_access_token(self, token: str) -> None:
        self._cookies[_ACCESS_COOKIE] = token

    def create_locker(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._request("POST", _LOCKERS_PATH, payload)


@dataclass(slots=True)
class ProvisionOutcome:
    relay_id: int
    address: str
    status: int
    created: bool
    locker_id: str | None


def provision_fleet(session: GatewaySession, count: int, base_id: int,
                    index_start: int = 0) -> list[ProvisionOutcome]:
    """Create ``count`` mqtt lockers (relay ids ``base_id..base_id+count-1``).

    Existing lockers (409 on duplicate inventory_number/relay_id) are treated as an
    idempotent skip. A 401/403 aborts immediately — it means the session lacks the
    role to create lockers, so every subsequent call would fail identically.
    """
    outcomes: list[ProvisionOutcome] = []
    for i in range(count):
        relay_id = base_id + i
        payload = build_locker_payload(relay_id, index_start + i)
        address = str(payload["location_address"])
        status, body = session.create_locker(payload)
        if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise RuntimeError(
                f"not authorised to create lockers (status={status}); the account "
                f"must be administrator/operator: {body}")
        if status == _HTTP_CREATED:
            outcomes.append(ProvisionOutcome(
                relay_id, address, status, created=True, locker_id=body.get("id")))
        elif status == _HTTP_CONFLICT:
            logger.warning("relay %s already provisioned (409) — skipping", relay_id)
            outcomes.append(ProvisionOutcome(
                relay_id, address, status, created=False, locker_id=None))
        else:
            logger.error("failed to create locker relay_id=%s: status=%s %s",
                         relay_id, status, body)
            outcomes.append(ProvisionOutcome(
                relay_id, address, status, created=False, locker_id=None))
    return outcomes
