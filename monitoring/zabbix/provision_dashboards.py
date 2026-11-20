"""Idempotent provisioning of the SCUD Zabbix dashboards (stdlib only).

``configuration.import`` does not carry global dashboards, so they are created
through the JSON-RPC ``dashboard.create`` / ``dashboard.update`` APIs, reusing the
``api_call`` / ``_get_id`` helpers from :mod:`provision`. Every widget is an
``svggraph`` bound to concrete ``itemid``s resolved from the live hosts
(``scud-<svc>`` / ``scud-agent2``) and the ``scud.metric.*`` / ``scud.sli.*`` /
``scud.dbprobe[*]`` keys, so the dashboards overlay real series.

Run as the last step of :func:`provision.main` (hosts/items must already exist).
Idempotent: a dashboard present by name is updated in place, never duplicated; a
widget whose item cannot be resolved is skipped with a warning so the rest of the
dashboard still builds.
"""
import logging

import provision

_log = logging.getLogger(__name__)

# Dashboard names (idempotency keys — do not rename casually).
DASH_SERVICE_OVERVIEW = "SCUD Service Overview"
DASH_INFRA = "SCUD Infra"
DASH_SLO = "SCUD SLO & Burn Rate"
DASH_GATEWAY = "SCUD Gateway"
DASH_OUTBOX = "SCUD Outbox"

# Zabbix 7.0 dashboard widget field type codes.
_FIELD_TYPE_INT = 0
_FIELD_TYPE_STR = 1
_FIELD_TYPE_ITEM = 4
# svggraph item-list data set: dataset_type 0 = explicit item list (itemids),
# 1 = host/item pattern. We bind concrete itemids → 0.
_DATASET_TYPE_ITEM_LIST = 0
# Default visible time window for every graph.
_TIME_PERIOD_FROM = "now-1h"
_TIME_PERIOD_TO = "now"
# svggraph widgets require a unique per-dashboard "reference" (5 upper-alnum) for
# time-period broadcasting; assigned in _ensure_dashboard.
_REFERENCE_FIELD = "reference"

# Grid: Zabbix 7.0 dashboards are 72 columns wide; lay widgets out 3 per row.
_GRID_W = 24
_GRID_H = 5
_COLS = 3

# Stock Zabbix series palette (hex without '#'), cycled per series in a widget.
_SERIES_COLORS = (
    "1A7C11", "2774A4", "F63100", "A54F10", "FC6EA3", "6C59DC", "AC8C14",
)

# Dashboard sharing: 0 = public (visible to every operator), 1 = private.
_DASHBOARD_PUBLIC = 0

_SERVICES = (
    "user_api", "locker_api", "history_api", "auth_api",
    "acm", "notification_api", "reports_api",
)
# DB instances behind PgBouncer / PostgreSQL (mirrors template-scud-infra.yaml).
_DB_INSTANCES = ("user", "locker", "history", "auth", "notification", "reports")
_INFRA_HOST = "scud-agent2"


def _resolve_itemid(url, token, host, key):
    """Return the itemid for (host, key), or None (logged) if absent."""
    host_id = provision._get_id(url, token, "host.get", {"host": host}, "hostid")
    if host_id is None:
        _log.warning("dashboard: host %s not found — skipping its widgets", host)
        return None
    result = provision.api_call(
        "item.get",
        {"output": ["itemid"], "hostids": host_id, "filter": {"key_": key}},
        url,
        auth=token,
    )
    rows = result if isinstance(result, list) else []
    if not rows:
        _log.warning("dashboard: item %s not found on %s — skipping widget series", key, host)
        return None
    return str(rows[0]["itemid"])


def _svggraph_dataset_fields(itemids):
    """Build the svggraph field list for one explicit-item data set.

    Kept in one place so the exact 7.0 field schema can be confirmed against a
    live widget and adjusted without touching the dashboard layout logic.
    """
    fields = [
        {"type": _FIELD_TYPE_INT, "name": "ds.0.dataset_type", "value": _DATASET_TYPE_ITEM_LIST},
        {"type": _FIELD_TYPE_STR, "name": "time_period.from", "value": _TIME_PERIOD_FROM},
        {"type": _FIELD_TYPE_STR, "name": "time_period.to", "value": _TIME_PERIOD_TO},
    ]
    for idx, itemid in enumerate(itemids):
        color = _SERIES_COLORS[idx % len(_SERIES_COLORS)]
        fields.append({"type": _FIELD_TYPE_ITEM, "name": f"ds.0.itemids.{idx}", "value": itemid})
        fields.append({"type": _FIELD_TYPE_STR, "name": f"ds.0.color.{idx}", "value": color})
    return fields


def _widget(title, pos, itemids):
    """One svggraph widget. ``pos`` is the zero-based slot index in the page grid."""
    col = pos % _COLS
    row = pos // _COLS
    return {
        "type": "svggraph",
        "name": title,
        "x": col * _GRID_W,
        "y": row * _GRID_H,
        "width": _GRID_W,
        "height": _GRID_H,
        "fields": _svggraph_dataset_fields(itemids),
    }


def _page(url, token, name, series_specs):
    """Build a page from [(title, [(host, key), ...]), ...]; drop empty widgets."""
    widgets = []
    for title, series in series_specs:
        itemids = [
            iid for host, key in series
            if (iid := _resolve_itemid(url, token, host, key)) is not None
        ]
        if not itemids:
            _log.warning("dashboard page %s: widget %s has no resolvable items — skipped", name, title)
            continue
        widgets.append(_widget(title, len(widgets), itemids))
    return {"name": name, "widgets": widgets}


def _assign_references(pages):
    """Give every widget a unique 5-char reference (required by svggraph for
    time-period broadcasting); uniqueness is per dashboard."""
    n = 0
    for page in pages:
        for widget in page["widgets"]:
            widget["fields"] = [
                f for f in widget["fields"] if f["name"] != _REFERENCE_FIELD
            ]
            widget["fields"].insert(
                0, {"type": _FIELD_TYPE_STR, "name": _REFERENCE_FIELD, "value": f"W{n:04d}"}
            )
            n += 1


def _ensure_dashboard(url, token, name, pages):
    """Create the dashboard, or update it in place if one with this name exists."""
    pages = [p for p in pages if p["widgets"]]
    if not pages:
        _log.warning("dashboard %s has no widgets with data — skipping", name)
        return
    _assign_references(pages)
    dash_id = provision._get_id(
        url, token, "dashboard.get", {"name": name}, "dashboardid"
    )
    if dash_id is None:
        provision.api_call(
            "dashboard.create",
            {"name": name, "display_period": 60, "auto_start": 1,
             "private": _DASHBOARD_PUBLIC, "pages": pages},
            url,
            auth=token,
        )
        _log.info("created dashboard %s (%d pages)", name, len(pages))
    else:
        provision.api_call(
            "dashboard.update",
            {"dashboardid": dash_id, "display_period": 60, "auto_start": 1,
             "private": _DASHBOARD_PUBLIC, "pages": pages},
            url,
            auth=token,
        )
        _log.info("updated dashboard %s (%d pages)", name, len(pages))


def _service_overview_pages(url, token):
    def metric(svc, name):
        return (f"scud-{svc}", f"scud.metric.{svc}.{name}")

    overview = [
        ("HTTP 5xx — all services", [metric(s, "scud_http_requests_5xx") for s in _SERVICES]),
        ("DB pool waiters — all services", [metric(s, "scud_db_pool_connections_waiters") for s in _SERVICES]),
        ("DB errors — all services", [metric(s, "scud_db_errors_total") for s in _SERVICES]),
        ("Interservice errors — all services", [metric(s, "scud_interservice_errors") for s in _SERVICES]),
    ]
    pages = [_page(url, token, "Overview", overview)]
    for svc in _SERVICES:
        per_svc = [
            ("HTTP requests total", [metric(svc, "scud_http_requests_total")]),
            ("HTTP 5xx", [metric(svc, "scud_http_requests_5xx")]),
            ("HTTP duration sum", [metric(svc, "scud_http_request_duration_seconds")]),
            ("DB pool waiters", [metric(svc, "scud_db_pool_connections_waiters")]),
            ("DB errors", [metric(svc, "scud_db_errors_total")]),
            ("Security events", [metric(svc, "scud_security_events_total")]),
            ("Login attempts", [metric(svc, "scud_login_attempts_total")]),
        ]
        pages.append(_page(url, token, svc, per_svc))
    return pages


def _infra_pages(url, token):
    def pgb(metric):
        return [(_INFRA_HOST, f"scud.dbprobe[pgb,pgbouncer_{i},{metric}]") for i in _DB_INSTANCES]

    def pg(metric):
        return [(_INFRA_HOST, f"scud.dbprobe[pg,postgres_{i},{metric}]") for i in _DB_INSTANCES]

    infra = [
        ("Host CPU utilisation", [(_INFRA_HOST, "system.cpu.util")]),
        ("Host memory available %", [(_INFRA_HOST, "vm.memory.size[pavailable]")]),
        ("Host disk used % (/)", [(_INFRA_HOST, "vfs.fs.size[/hostfs,pused]")]),
        ("PgBouncer clients waiting", pgb("cl_waiting")),
        ("PgBouncer server active", pgb("sv_active")),
        ("Postgres deadlocks", pg("deadlocks")),
        ("Postgres xact rollback", pg("xact_rollback")),
        ("Postgres backends", pg("numbackends")),
        ("Redis up", [(_INFRA_HOST, "scud.infra.redis.up")]),
    ]
    return [_page(url, token, "Infra", infra)]


def _slo_pages(url, token):
    def sli(svc, name):
        return (f"scud-{svc}", f"scud.sli.{svc}.{name}")

    slo = [
        ("Login error ratio", [sli("auth_api", "login_error_ratio_5m"), sli("auth_api", "login_error_ratio_1h")]),
        ("Login burn rate", [sli("auth_api", "login_burn_fast"), sli("auth_api", "login_burn_slow")]),
        ("OTP error ratio", [sli("auth_api", "otp_error_ratio_5m"), sli("auth_api", "otp_error_ratio_1h")]),
        ("OTP burn rate", [sli("auth_api", "otp_burn_fast"), sli("auth_api", "otp_burn_slow")]),
        ("Command p99 fraction", [sli("locker_api", "cmd_p99_fraction_5m"), sli("locker_api", "cmd_p99_fraction_1h")]),
        ("Command latency burn", [sli("locker_api", "cmd_latency_burn_fast"), sli("locker_api", "cmd_latency_burn_slow")]),
    ]
    return [_page(url, token, "SLO", slo)]


def _gateway_pages(url, token):
    """Detailed Apache gateway dashboard. All series come from the mod_status
    items on the infra host (``scud.gateway.*`` parsed from server-status?auto).
    """
    def gw(name):
        return (_INFRA_HOST, f"scud.gateway.{name}")

    # Worker pool / saturation — the prefork "one process per connection" ceiling
    # (MaxRequestWorkers=slots_total) is the WebSocket-fleet bottleneck.
    workers = [
        ("Worker utilisation %", [gw("worker_util_pct")]),
        ("Busy vs idle workers", [gw("busy_workers"), gw("idle_workers")]),
        ("Busy workers vs slot ceiling", [gw("busy_workers"), gw("slots_total")]),
        ("Open slots (headroom)", [gw("sb_open")]),
        ("Graceful workers", [gw("graceful_workers")]),
        ("Gateway reachable (:80)", [(_INFRA_HOST, "net.tcp.service[http,apache_gateway,80]")]),
    ]
    # Scoreboard breakdown — where the workers actually are.
    scoreboard = [
        ("Sending reply / proxying (W)", [gw("sb_sending")]),
        ("Waiting for connection (_)", [gw("sb_waiting")]),
        ("Reading request (R)", [gw("sb_reading")]),
        ("Keepalive (K)", [gw("sb_keepalive")]),
        ("Closing (C)", [gw("sb_closing")]),
    ]
    # Throughput / latency / process.
    throughput = [
        ("Requests/sec (live)", [gw("req_rate")]),
        ("Requests/sec (lifetime avg)", [gw("req_per_sec")]),
        ("Bytes/sec", [gw("bytes_per_sec")]),
        ("Avg request duration", [gw("duration_per_req")]),
        ("Bytes/request", [gw("bytes_per_req")]),
        ("Total accesses", [gw("total_accesses")]),
        ("Apache CPU load", [gw("cpu_load")]),
        ("Host load average (1m)", [gw("load1")]),
        ("Uptime", [gw("uptime")]),
    ]
    return [
        _page(url, token, "Workers & saturation", workers),
        _page(url, token, "Scoreboard", scoreboard),
        _page(url, token, "Throughput & process", throughput),
    ]


def _outbox_pages(url, token):
    """Outbox (outbound_history_events → history_api) queue health. Series come
    from the dbprobe items on the infra host (outbox_* on postgres_locker). This
    makes visible the failure class that froze delivery: a growing backlog, a
    stalled oldest event, ballooning pending bytes (the OOM precursor that
    choked the dispatcher), and events parked in dead-letter.
    """
    def ob(metric):
        return (_INFRA_HOST, f"scud.dbprobe[pg,postgres_locker,{metric}]")

    outbox = [
        ("Pending events", [ob("outbox_pending")]),
        ("Oldest pending age", [ob("outbox_oldest_age_s")]),
        ("Pending payload bytes (OOM precursor)", [ob("outbox_pending_bytes")]),
        ("Retrying events", [ob("outbox_retrying")]),
        ("Dead-lettered events", [ob("outbox_deadletter")]),
        ("Pending vs retrying", [ob("outbox_pending"), ob("outbox_retrying")]),
    ]
    return [_page(url, token, "Outbox", outbox)]


def provision_dashboards(url, token):
    """Provision the SCUD dashboards. Idempotent."""
    _ensure_dashboard(url, token, DASH_SERVICE_OVERVIEW, _service_overview_pages(url, token))
    _ensure_dashboard(url, token, DASH_INFRA, _infra_pages(url, token))
    _ensure_dashboard(url, token, DASH_SLO, _slo_pages(url, token))
    _ensure_dashboard(url, token, DASH_GATEWAY, _gateway_pages(url, token))
    _ensure_dashboard(url, token, DASH_OUTBOX, _outbox_pages(url, token))
