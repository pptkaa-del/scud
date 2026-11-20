# SCUD Zabbix monitoring

Zabbix 7.0.10 template + idempotent provisioner for the ScudDocs СКУД backend.

## Files

| File | Purpose |
|---|---|
| `template-scud-services.yaml` | Zabbix 7.0 export — 7 service templates, 10 items each, 42 top-level triggers, LLD rules |
| `template-scud-infra.yaml` | Zabbix 7.0 export — infra template: Redis reachability, PgBouncer ×6, PostgreSQL ×6 (LLD), MQTT gauges |
| `provision.py` | Idempotent stdlib-only provisioner; imports the template via Zabbix JSON-RPC API |
| `tests/test_provision.py` | Unit tests for the provisioner (mock HTTP) |
| `tests/test_template_yaml.py` | Structural tests for the YAML template |
| `tests/test_triggers.py` | Trigger count and expression coverage tests |
| `tests/test_infra_template_yaml.py` | Structural tests for the infra YAML template |
| `tests/test_infra_triggers.py` | Trigger count and expression coverage tests for the infra template |
| `alert-delivery.yaml` | Zabbix 7.0 export — Max Messenger webhook media type (the only entity `configuration.import` accepts). The SCUD On-Call usergroup, on-call user, and HIGH+ trigger action are created by `provision.py` via the JSON-RPC API. |
| `tests/test_alert_delivery.py` | Structural tests for alert-delivery.yaml and provisioner unit tests for import_alert_delivery / provision_alert_routing / provision_max_token |

## Running the provisioner

```bash
ZABBIX_URL=http://127.0.0.1:8090 \
ZABBIX_USER=Admin \
ZABBIX_PASSWORD=zabbix \
python monitoring/zabbix/provision.py
```

Environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `ZABBIX_URL` | yes | — | Base URL of the Zabbix web frontend (no trailing slash) |
| `ZABBIX_USER` | no | `Admin` | Zabbix user with configuration import rights |
| `ZABBIX_PASSWORD` | yes | — | Password for the Zabbix user |
| `ZABBIX_TEMPLATE` | no | `template-scud-services.yaml` next to `provision.py` | Path to the YAML template to import |
| `MAX_BOT_TOKEN` | no | — | Max bot token → global macro `{$MAX.BOT.TOKEN}`. Never logged. Required for real alert delivery. |
| `ONCALL_MAX_USER_ID` | no | — | Numeric Max `user_id` of the on-call recipient. When set, the `scud_oncall` media is enabled and addressed to it; when unset the media is left **disabled** with a placeholder. |

Re-running is safe — `configuration.import` is called with `createMissing/updateExisting` rules for all object types.

## Starting the monitoring stack

```bash
# From the repo root
cd backend
bash scripts/seed-dev-secrets.sh
docker compose --profile monitoring up -d
```

The Zabbix web UI is then available at `http://127.0.0.1:8090` (default credentials `Admin` / `zabbix`).  
The JSON-RPC API endpoint is `http://127.0.0.1:8090/api_jsonrpc.php`.

Wait until `zabbix_server` logs show `server #N started [...]` before running the provisioner.

## Running the tests

```bash
# From the repo root
docker run --rm \
  -v "$(pwd)/monitoring/zabbix:/w" \
  -w /w \
  python:3.13-slim \
  sh -c "pip install pytest pyyaml -q && python -m pytest -vv tests/"
```

## Manual UI fallback

If `provision.py` is unavailable (no Python on the host), import the template
through the Zabbix web UI:

1. Open `http://127.0.0.1:8090`, log in as `Admin`.
2. **Data collection → Templates → Import**.
3. Choose `template-scud-services.yaml`, tick *Create new* and *Update existing*
   for Templates / Items / Discovery rules / Triggers, click **Import**.
4. **Data collection → Hosts → Create host**, link a `SCUD Service <svc>`
   template, and ensure the host sits on `backend_net` so
   `http://<svc>:<port>/metrics` resolves.

## Infra template (Этап 2)

`template-scud-infra.yaml` is provisioned the same way, pointing `ZABBIX_TEMPLATE` at it:

```bash
ZABBIX_TEMPLATE=monitoring/zabbix/template-scud-infra.yaml \
ZABBIX_URL=http://127.0.0.1:8090 ZABBIX_PASSWORD=zabbix \
python monitoring/zabbix/provision.py
```

It collects signals the host/server knows but the application does not — gathered by the
`zabbix_agent2` container (on `backend_net`, unprivileged, active mode pushing to
`zabbix_server:10051`):

### Infra metric catalogue

| Component | Source | Key signals |
|---|---|---|
| Redis | `scud_redis_up` gauge via HTTP item `scud.infra.redis.up` | Reachability only (0/1); the acm service pings Redis every 15 s and writes this gauge to its `/metrics`. The agent2 built-in redis plugin is absent in the Astra 7.0.10 build — see `monitoring/CERT_GATE.md` §A2-2 for the fallback decision. Full Redis internals (used_memory, connected_clients, ops/sec) are NOT available under this fallback. |
| PgBouncer ×6 | `scud.dbprobe[pgb,<host>,<metric>]` → `dbprobe.sh` runs `SHOW STATS`/`SHOW POOLS` on `:6432 -U pgb_admin` (explicit per-instance) | `cl_waiting` (queue depth invisible to app-side `scud_db_pool_*`), `sv_active`, `maxwait`, `avg_query_time` |
| PostgreSQL ×6 | `scud.dbprobe[pg,<host>,<metric>]` → `pg_stat_*` queries via `psql` (explicit per-instance) | `numbackends` vs `max_connections`, `deadlocks`, `xact_rollback`, `longest_txn_seconds`; `history_partition_bytes` and `history_orphan_media` (history DB only — relay photo frames whose intrusion event has not arrived for 10+ min; WARNING trigger after 30m); `outbox_*` (locker DB only — see below) |
| Outbox (history delivery) | `scud.dbprobe[pg,postgres_locker,outbox_*]` → counts/sizes over `outbound_history_events` (locker DB only) | `outbox_pending` backlog, `outbox_oldest_age_s` (delivery stalled), `outbox_pending_bytes` (dispatcher OOM precursor), `outbox_retrying`, `outbox_deadletter` (parked after exhausting attempts). Dashboard **SCUD Outbox**; 4 triggers macro-driven (`{$OUTBOX.*}`). Makes visible the failure class — a backlog of large base64-media events loaded into a single dispatcher batch — that previously OOM-froze delivery. |
| Gateway (Apache) | `net.tcp.service[http,apache_gateway,80]` (up/down) + `scud.gateway.*` dependent items off the HTTP master `scud.gateway.status.raw` (`server-status?auto`) | `busy_workers` / `idle_workers` / `slots_total` / `worker_util_pct`, scoreboard breakdown (`sb_sending`/`sb_waiting`/…), `req_rate`, `bytes_per_sec`, `duration_per_req`, `cpu_load`, `load1`, `uptime` |

The postgres superuser the probe connects as is **`admin`** (`POSTGRES_USER=admin`), not `postgres`.

#### Gateway mod_status (internal :81)

The gateway runs **prefork MPM** — one process per connection — so `BusyWorkers`
approaching `slots_total` (= `MaxRequestWorkers`, 150 by default) is the hard
ceiling on concurrent REST/HTTP connections through the gateway. Devices connect
to the MQTT broker directly (bypassing Apache), so this ceiling bounds client and
inter-service REST traffic rather than device transport.

`mod_status` itself leaks worker-state, in-flight request URLs and client IPs, so it
is **not** exposed on the public `:80` vhost. Instead `gateway/status.conf` serves
`server-status?auto` on an **unpublished port 81** (a second `VirtualHost`, default-deny
except the status handler). Port 81 is reachable only container-to-container on
`backend_net` — never published in `docker-compose.yaml` — mirroring how the backend
`/metrics` endpoints are internal-by-isolation. `zabbix_server` (also on `backend_net`)
fetches it directly with the `HTTP_AGENT` master item; dependent items parse each field
with `REGEX` / `JAVASCRIPT` preprocessing (on-fail → `0`, so a missing field never goes
UNSUPPORTED).

Triggers: Redis unreachable; PgBouncer `cl_waiting > 0` sustained 5 m; Postgres backends
near `max_connections`; Postgres deadlocks; **Gateway down** (`:80`); **gateway worker
pool saturating** (`worker_util_pct > {$GATEWAY.WORKERS.HIGH.PCT}` = 85 % for 5 m) and
**exhausted** (> 95 % for 2 m); **server-status scrape failing** (`:81` no data 5 m).

**MQTT broker signals** (`scud_mqtt_connected`, `scud_mqtt_relays_online`,
`scud_mqtt_broker_*`) are NOT in the infra template — `locker_api` re-exposes
them on its own `/metrics` by mirroring `$SYS/broker/#` topics, and the services
template already scrapes them.

### Agent2 container

`zabbix_agent2` runs as part of the `monitoring` compose profile:

```bash
cd backend && docker compose --profile monitoring up -d
```

It has no published ports and communicates in active mode
(`ServerActive=zabbix_server`). It mounts `pgb_admin_password` and the six
`postgres_<svc>_password` secrets (no Redis credential — the redis plugin is absent).

**Host OS metrics** (`scud-infra-host` template) need the agent to see the VM, not
its own container, so the service runs with `pid: host` and the host root mounted
read-only at `/hostfs`. The built-in `system.cpu.*` / `vm.memory.*` keys then read
the host `/proc`, and `vfs.fs.size[/hostfs,…]` reads the host disk. **Security note:**
`pid: host` lets the agent see all host processes — acceptable for a monitoring agent
on this stand; revisit if the trust boundary tightens. (Per-container CPU/memory is a
separate concern — the certified agent2 build has no Docker plugin, so it needs a
Docker-socket reader rather than `docker.*` keys.)

**Probe batching + timeout.** A cold `psql` connection per dbprobe item (~10 s each)
both exceeds the default 3 s budget and, ×52, saturates the DB connection pools.
`dbprobe.sh` therefore **batches**: the first item for a `(source, host)` opens ONE
connection per cache TTL (25 s, below the 30 s item delay) that fetches all of that
host's metrics into `/tmp/scud-dbprobe/`; the rest of the host's items serve from
that cache, and a `flock` serialises the rebuild so concurrent items don't stampede.
`zabbix_agent2.timeout.conf` still sets `Timeout=30` (the Zabbix 7.0 max) and each
item carries `timeout: 30s` so the single cold connect fits. Each `psql` redirects
stderr to `/dev/null` (the pgbouncer admin console always emits a benign "Could not
determine access control version" warning that agent2 would otherwise merge into the
value); a real connection failure still exits non-zero → NOTSUPPORTED.

## Этап 3 — Max messenger alert delivery

`alert-delivery.yaml` wires existing Zabbix triggers to the on-call engineer via Max.
The file itself ships only the `Max Messenger` webhook media type, because Zabbix 7.0
`configuration.import` does not accept user groups, users or actions in its export
schema. `provision.py` imports that media type, then creates the `SCUD On-Call`
usergroup, the on-call user, and a trigger action that fires on severity
**HIGH or above** through the dedicated `usergroup.create` / `user.create` /
`action.create` APIs (idempotent — re-running creates nothing new and patches the
on-call user's media in place via `user.update`).

The webhook media type's `max_user_id` parameter is `{ALERT.SENDTO}`, so each
recipient is taken from their own media **Send to** value. **For alerts to reach
anyone, both must be set: `{$MAX.BOT.TOKEN}` (auth) and a real `Send to` recipient.**

### One-time operator setup (per deployment)

#### 1. Set the Max bot token

The webhook needs the bot token to authenticate against the Max API.
Store it as the Zabbix **global macro** `{$MAX.BOT.TOKEN}`:

**Option A — via provision.py (recommended)**

```bash
MAX_BOT_TOKEN=<token-from-MasterBot> \
ZABBIX_URL=http://127.0.0.1:8090 \
ZABBIX_PASSWORD=zabbix \
python monitoring/zabbix/provision.py
```

provision.py sets `{$MAX.BOT.TOKEN}` using the Zabbix `usermacro.createglobal` API.
If `MAX_BOT_TOKEN` is unset or empty the step is skipped silently (safe for CI).
The token is **never written to any log line or file**.

**Option B — via Zabbix UI**

Administration → General → Macros → create `{$MAX.BOT.TOKEN}` with the token value.
Mark its type as **Secret text** to hide the value in the UI.

#### 2. Set the per-recipient Max user ID

Each on-call person has a numeric Max `user_id`, carried in their media **Send to**
field and substituted into the webhook via `{ALERT.SENDTO}`.

**Recommended — via `provision.py`:** pass `ONCALL_MAX_USER_ID=<numeric-id>`. The
`scud_oncall` media is then created/updated **enabled** and addressed to that id.
Without it the media is left **disabled** with the `000000000` placeholder (safe —
no failed sends in the action log) and a warning is logged. `user.update` replaces
the media list, so any media added manually in the UI is overwritten on re-run.

```bash
MAX_BOT_TOKEN=<token-from-MasterBot> \
ONCALL_MAX_USER_ID=<numeric-max-user-id> \
ZABBIX_URL=http://127.0.0.1:8091 ZABBIX_PASSWORD=zabbix \
python monitoring/zabbix/provision.py
```

**Or via the UI:** Administration → Users → `scud_oncall` → Media → edit the
**Max Messenger** entry, set **Send to** to the real id, enable it, save. To add
more recipients, add members to the **SCUD On-Call** usergroup.

#### 3. Verify delivery

After setup, manually trigger a test notification:
Administration → Media types → Max Messenger → Test.
Fill in `token` (leave as `{$MAX.BOT.TOKEN}` or paste the value), `max_user_id`, a test subject and message, then click **Test**.

### Alert behaviour

| Trigger severity | Action |
|---|---|
| **HIGH** or **DISASTER** | On-call user receives a Max message |
| Recovery | Recovery message sent to the same user |
| AVERAGE or below | No message (filtered out by action condition) |

The alert action is named **"SCUD Alert: Notify On-Call via Max"** and can be
disabled temporarily from Alerts → Actions → Trigger actions.

## Этап 1 — Domain metrics and additional triggers (locker_api)

Commit `e2a4693` extended `template-scud-services.yaml` with 9 locker_api-specific
dependent items and 4 extra triggers, plus `scud_interservice_errors` on all 7 services
(7 more triggers). Total trigger count: **59** (42 baseline + 17 new).

### Этап 1 domain items (locker_api only)

| Zabbix item key | Prometheus source | Description |
|---|---|---|
| `scud.metric.locker_api.scud_command_pending_total` | `scud_command_pending_total` gauge | Command backlog depth (unacknowledged commands queued for devices) |
| `scud.metric.locker_api.scud_command_lifecycle_failed` | `scud_command_lifecycle_total{transition="failed"}` | Failed command transitions (rate via SIMPLE_CHANGE) |
| `scud.metric.locker_api.scud_ws_events_supersession` | `scud_ws_events_total{event="supersession"}` | Relay supersession disconnects (duplicate device reconnect) |
| `scud.metric.locker_api.scud_mqtt_relay_messages_dropped` | `scud_mqtt_relay_messages_total{outcome=~"dropped.*\|handler_error"}` | MQTT relay messages that could not be dispatched |

**Note on inter-service latency histogram:** `scud_interservice_request_duration_seconds`
(histogram) is exposed on every service's `/metrics`, but is **not** given a dedicated Zabbix
item. Histogram quantiles (`histogram_quantile`) require aggregation across `_bucket` series
which is not expressible as a single JSONPath dependent item in Zabbix 7.0. Alerting on
inter-service error rates (`scud_interservice_errors`) covers the actionable failure signal.

### scud_interservice_errors (all 7 services)

Each service also gains a `scud.metric.<svc>.scud_interservice_errors` dependent item
(filtering `scud_interservice_requests_total{outcome=~"error|exhausted"}`). This was
already present for locker_api in the baseline but is now uniformly applied to all services.

### Этап 1 triggers (17 new, top-level `zabbix_export.triggers`)

| Trigger name | Condition | Priority |
|---|---|---|
| `locker_api command failure rate high` | `scud_command_lifecycle_failed > 5` or `scud_command_lifecycle_total > 5` sustained 5 m | AVERAGE |
| `locker_api relay drop spike` | `scud_mqtt_relay_messages_dropped > 20` sustained 5 m | AVERAGE |
| `locker_api command backlog saturation` | `scud_command_pending_total > 50` sustained 10 m | AVERAGE |
| `<svc> interservice error rate high` ×7 | `scud_interservice_errors > 5` per service sustained 5 m | AVERAGE |
| `<svc> DB error rate high` ×7 | `scud_db_errors_total > 5` per service sustained 5 m | AVERAGE |

The 7 interservice + 7 DB-error triggers replace the per-service baseline (previously these
were absent from the 6-per-service baseline set). All 60 triggers are idempotently imported
by `provision.py` (`createMissing/updateExisting/deleteMissing`).

## Template design notes

- Each of the 7 backend services gets one HTTP_AGENT master item that fetches `/metrics`
  and preprocesses it with `PROMETHEUS_TO_JSON`, plus dependent items and one LLD rule.
  locker_api has 19 dependent items (9 baseline + 10 Этап 1 domain items including
  `scud_interservice_errors`); all other services have 10 dependent items each.
- Triggers (60 total: 42 baseline + 18 Этап 1) are placed at the top-level
  `zabbix_export.triggers` key as required by the Zabbix 7.0 YAML schema — NOT inside
  `templates[].triggers` (which is invalid in 7.0 and causes an API error on import).
- All UUIDs are valid UUIDv4 (version nibble `4`, variant `8/9/a/b`).
- JSONPath label filters use `=~` inside Zabbix's own JSONPath dialect
  (applied after `PROMETHEUS_TO_JSON`; standard RFC 9535 syntax).

## Centralised logs (syslog-ng)

The `syslog_ng` service (monitoring profile) tails every container's Docker
json-file logs, keeps only valid JSON lines from the 7 SCUD backend services
(allowlist filter `f_scud_services` on the app's own `service` field), and writes
them to `/var/log/scud/scud-all.log` inside the `syslog_ng_logs` volume.

**Rotation.** `logrotate` (Astra `main`) rotates the aggregate **daily, keep 14,
compressed**; a small `sleep`-loop in the image entrypoint drives it (no cron), and
`postrotate` runs `syslog-ng-ctl reopen` so syslog-ng writes to the fresh file
without losing lines. There is no certified log-search UI (Loki/ELK are not on the
Astra stack), so search is via `ssh` + `docker exec`:

```bash
# tail the aggregate
docker exec backend-syslog_ng-1 tail -f /var/log/scud/scud-all.log

# all ERROR lines from auth_api in the last file
docker exec backend-syslog_ng-1 sh -c \
  "grep '\"service\": \"auth_api\"' /var/log/scud/scud-all.log | grep '\"level\": \"ERROR\"'"

# follow one request across services by request_id (pretty-printed)
docker exec backend-syslog_ng-1 sh -c \
  "grep '\"request_id\": \"<rid>\"' /var/log/scud/scud-all.log" | python3 -m json.tool

# search a rotated/compressed slice
docker exec backend-syslog_ng-1 sh -c "zgrep '<pattern>' /var/log/scud/scud-all.log.*.gz"
```

## Dashboards

`provision_dashboards.py` (run as the last step of `provision.py`, after the
hosts exist) creates four global dashboards via `dashboard.create`, each an
`svggraph` overlay bound to the live `scud-<svc>` / `scud-agent2` hosts. It is
idempotent — a dashboard present by name is updated in place, and a widget whose
item cannot be resolved is skipped with a warning so the rest still builds.

| Dashboard | Pages / widgets |
|---|---|
| **SCUD Service Overview** | Overview page overlaying all 7 services (HTTP 5xx, DB pool waiters, DB errors, interservice errors) + one page per service (requests, 5xx, duration, pool waiters, DB errors, security events, login attempts) |
| **SCUD Infra** | PgBouncer clients-waiting / server-active, Postgres deadlocks / xact-rollback / backends, Redis up, host CPU/mem/disk — all from `scud.dbprobe[*]` / `scud.infra.redis.up` / host keys on `scud-agent2` |
| **SCUD SLO & Burn Rate** | auth_api login & OTP error-ratio + burn-rate, locker_api command p99 fraction + latency burn (`scud.sli.*`) |
| **SCUD Gateway** | Apache `mod_status` detail (`scud.gateway.*` on `scud-agent2`): **Workers & saturation** (utilisation %, busy vs idle, busy vs slot ceiling, open-slot headroom, :80 reachable), **Scoreboard** (sending/waiting/reading/keepalive/closing), **Throughput & process** (live & lifetime req/s, bytes/s, avg duration, total accesses, CPU load, host load1, uptime) |

The exact `svggraph` widget field schema lives in `_svggraph_dataset_fields`; if a
future Zabbix build rejects it, confirm the field codes against a real widget
(`dashboard.get` with `selectPages`/`selectWidgets`) and adjust there.

## Tearing down

```bash
cd backend
docker compose --profile monitoring down
# To remove volumes (DB data):
docker compose --profile monitoring down -v
```
