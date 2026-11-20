"""Idempotent Zabbix template provisioner (stdlib only; curl absent from Astra main)."""
import json
import logging
import os
import sys
import urllib.error
import urllib.request

API_PATH = "/api_jsonrpc.php"
_RPC_ID = 0

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_log = logging.getLogger(__name__)


class ZabbixApiError(RuntimeError):
    pass


def api_call(
    method: str,
    params: dict,
    url: str,
    auth: str | None = None,
) -> object:
    global _RPC_ID
    _RPC_ID += 1
    payload: dict = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": _RPC_ID,
    }
    if auth is not None:
        payload["auth"] = auth
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json-rpc"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "error" in body:
        err = body["error"]
        raise ZabbixApiError(f"{err.get('message')}: {err.get('data')}")
    return body["result"]


def login(url: str, user: str, password: str) -> str:
    token = api_call("user.login", {"username": user, "password": password}, url)
    return str(token)


def import_template(url: str, token: str, yaml_text: str) -> object:
    rules = {
        "templates": {"createMissing": True, "updateExisting": True},
        "items": {"createMissing": True, "updateExisting": True, "deleteMissing": True},
        "discoveryRules": {
            "createMissing": True,
            "updateExisting": True,
            "deleteMissing": True,
        },
        "triggers": {
            "createMissing": True,
            "updateExisting": True,
            "deleteMissing": True,
        },
        "template_groups": {"createMissing": True, "updateExisting": True},
    }
    params = {"format": "yaml", "rules": rules, "source": yaml_text}
    return api_call("configuration.import", params, url, auth=token)


def import_alert_delivery(url: str, token: str, yaml_text: str) -> object:
    """Import the Max Messenger media type from alert-delivery.yaml.

    Zabbix 7.0 ``configuration.import`` only accepts the ``media_types`` export
    section (rules key ``mediaTypes``); user groups, users and trigger actions
    are not part of the import schema and are provisioned separately via
    :func:`provision_alert_routing`.
    """
    rules = {"mediaTypes": {"createMissing": True, "updateExisting": True}}
    params = {"format": "yaml", "rules": rules, "source": yaml_text}
    return api_call("configuration.import", params, url, auth=token)


# On-call routing constants (created via JSON-RPC, not configuration.import).
ONCALL_GROUP_NAME = "SCUD On-Call"
ONCALL_USER_NAME = "scud_oncall"
ALERT_ACTION_NAME = "SCUD Alert: Notify On-Call via Max"
MEDIA_TYPE_NAME = "Max Messenger"
# Zabbix numeric enums (stable across 7.0): user role 1 = "User";
# operationtype 0 = send message; conditiontype 4 = trigger severity;
# operator 5 = ">="; severity value 4 = "High".
_USER_ROLE_ID = "1"
_OP_SEND_MESSAGE = "0"
_COND_TRIGGER_SEVERITY = "4"
_OPERATOR_GE = "5"
_SEVERITY_HIGH = "4"
# Placeholder password for the stub on-call user. Internal auth requires a
# password >= 8 chars; operators replace this account (or add their own to the
# group) with a real Max user id via the UI. Not a real secret.
_ONCALL_PLACEHOLDER_PASSWD = "ChangeMe-OnCall-1"  # noqa: S105
# Bitmask для поля severity у медиа пользователя: High(16)+Disaster(32)=48.
# "32" (Disaster only) — неверно: HIGH-алерты не доходят до получателя.
_MEDIA_SEVERITY_HIGH_AND_ABOVE = "48"  # High(16)+Disaster(32)
# Расписание медиа: круглосуточно все дни.
_MEDIA_PERIOD_ALWAYS = "1-7,00:00-24:00"
# Поле "active" у медиа ИНВЕРТИРОВано в Zabbix: 0 = enabled, 1 = disabled
# (в отличие от action "status", где 0 = enabled). Держим оба значения явно.
_MEDIA_STATUS_ENABLED = "0"
_MEDIA_STATUS_DISABLED = "1"
# Получатель оставлен заглушкой, пока не задан реальный Max user id: с ним медиа
# создаётся ВЫКЛЮЧЕННОЙ, чтобы не плодить failed-отправки в action log.
_ONCALL_PLACEHOLDER_SENDTO = "000000000"
# Имя env с числовым Max user_id получателя on-call. Не секрет (идентификатор),
# поэтому read_secret не нужен; но в логи значение не выводим без нужды.
ONCALL_MAX_USER_ID_ENV = "ONCALL_MAX_USER_ID"


def _build_oncall_media(media_id: str, sendto: str, active: str) -> dict:
    """Единый конструктор медиа on-call юзера (общий для create/update путей)."""
    return {
        "mediatypeid": media_id,
        "sendto": sendto,
        "active": active,
        "severity": _MEDIA_SEVERITY_HIGH_AND_ABOVE,
        "period": _MEDIA_PERIOD_ALWAYS,
    }


def _get_id(url: str, token: str, method: str, filt: dict, id_field: str) -> str | None:
    result = api_call(method, {"output": [id_field], "filter": filt}, url, auth=token)
    rows = result if isinstance(result, list) else []
    if rows:
        return str(rows[0][id_field])  # type: ignore[index]
    return None


def provision_alert_routing(url: str, token: str) -> None:
    """Idempotently create the on-call group, stub user and severity>=HIGH action.

    Each entity is created only when absent (get-by-name first); re-running is a
    no-op. The Max media type must already be imported.
    """
    media_id = _get_id(
        url, token, "mediatype.get", {"name": MEDIA_TYPE_NAME}, "mediatypeid"
    )
    if media_id is None:
        _log.warning("media type %s absent — skipping alert routing", MEDIA_TYPE_NAME)
        return

    group_id = _get_id(
        url, token, "usergroup.get", {"name": ONCALL_GROUP_NAME}, "usrgrpid"
    )
    if group_id is None:
        created = api_call(
            "usergroup.create", {"name": ONCALL_GROUP_NAME}, url, auth=token
        )
        group_id = str(created["usrgrpids"][0])  # type: ignore[index,call-overload]
        _log.info("created user group %s", ONCALL_GROUP_NAME)

    # Получатель и статус медиа: реальный Max user_id → ВКЛючена; иначе заглушка
    # + ВЫКЛючена (чтобы не плодить failed-отправки), с предупреждением.
    oncall_id = os.environ.get(ONCALL_MAX_USER_ID_ENV, "").strip()
    if oncall_id:
        media = _build_oncall_media(media_id, oncall_id, _MEDIA_STATUS_ENABLED)
    else:
        media = _build_oncall_media(
            media_id, _ONCALL_PLACEHOLDER_SENDTO, _MEDIA_STATUS_DISABLED
        )
        _log.warning(
            "%s not set — on-call media left disabled with placeholder; set it "
            "and re-run to enable Max alert delivery",
            ONCALL_MAX_USER_ID_ENV,
        )

    user_id = _get_id(
        url, token, "user.get", {"username": ONCALL_USER_NAME}, "userid"
    )
    if user_id is None:
        api_call(
            "user.create",
            {
                "username": ONCALL_USER_NAME,
                "name": "SCUD",
                "surname": "On-Call",
                "passwd": _ONCALL_PLACEHOLDER_PASSWD,
                "roleid": _USER_ROLE_ID,
                "usrgrps": [{"usrgrpid": group_id}],
                "medias": [media],
            },
            url,
            auth=token,
        )
        _log.info("created on-call user %s", ONCALL_USER_NAME)
    else:
        # Идемпотентный патч живого юзера: user.update с ключом "medias" ПОЛНОСТЬЮ
        # заменяет список медиа. Перепрогон с заданным ONCALL_MAX_USER_ID включает
        # доставку, не пересоздавая юзера. passwd/roleid/usrgrps не трогаем.
        api_call(
            "user.update",
            {"userid": user_id, "medias": [media]},
            url,
            auth=token,
        )
        _log.info("on-call user %s media ensured", ONCALL_USER_NAME)

    action_id = _get_id(
        url, token, "action.get", {"name": ALERT_ACTION_NAME}, "actionid"
    )
    if action_id is None:
        api_call(
            "action.create",
            {
                "name": ALERT_ACTION_NAME,
                "eventsource": "0",
                "status": "0",
                "esc_period": "1h",
                "filter": {
                    "evaltype": "0",
                    "conditions": [
                        {
                            "conditiontype": _COND_TRIGGER_SEVERITY,
                            "operator": _OPERATOR_GE,
                            "value": _SEVERITY_HIGH,
                        }
                    ],
                },
                "operations": [
                    {
                        "operationtype": _OP_SEND_MESSAGE,
                        "opmessage": {"default_msg": "1", "mediatypeid": media_id},
                        "opmessage_grp": [{"usrgrpid": group_id}],
                    }
                ],
                "recovery_operations": [
                    {
                        "operationtype": _OP_SEND_MESSAGE,
                        "opmessage": {"default_msg": "1", "mediatypeid": media_id},
                        "opmessage_grp": [{"usrgrpid": group_id}],
                    }
                ],
            },
            url,
            auth=token,
        )
        _log.info("created alert action %s", ALERT_ACTION_NAME)


def provision_max_token(url: str, token: str) -> None:
    """Set the {$MAX.BOT.TOKEN} global macro from MAX_BOT_TOKEN env.

    Logs only presence/absence — never logs the token value itself.
    Skips silently when the env var is empty or unset.
    """
    bot_token = os.environ.get("MAX_BOT_TOKEN", "")
    if not bot_token:
        _log.info("MAX_BOT_TOKEN not set — skipping macro provisioning")
        return

    macro_name = "{$MAX.BOT.TOKEN}"
    # Try create; if it already exists, update instead.
    try:
        api_call(
            "usermacro.createglobal",
            {"macro": macro_name, "value": bot_token},
            url,
            auth=token,
        )
        _log.info("global macro %s set", macro_name)
    except ZabbixApiError:
        try:
            # Fetch the existing macro id first.
            macros = api_call(
                "usermacro.get",
                {"globalmacro": True, "filter": {"macro": macro_name}},
                url,
                auth=token,
            )
            if macros:
                macro_id = macros[0]["globalmacroid"]  # type: ignore[index]
                api_call(
                    "usermacro.updateglobal",
                    {"globalmacroid": macro_id, "value": bot_token},
                    url,
                    auth=token,
                )
                _log.info("global macro %s updated", macro_name)
        except ZabbixApiError as exc:
            _log.warning("could not set global macro %s (%s)", macro_name, type(exc).__name__)


# Host provisioning. ``configuration.import`` loads templates only; without a
# host linked to each template nothing is scraped. Service hosts use the
# HTTP-agent master items' literal service URLs (e.g. http://locker_api:8001/
# metrics), so they need no host interface.
SERVICE_HOST_GROUP = "SCUD Services"
INFRA_HOST_GROUP = "SCUD Infra"
_SERVICES = (
    "user_api", "locker_api", "history_api", "auth_api",
    "acm", "notification_api", "reports_api",
)
# The infra host name must match the zabbix_agent2 ``Hostname`` so its active
# checks (dbprobe LLD, scud.backup.status) land on this host.
INFRA_HOST_NAME = "scud-agent2"
_INFRA_TEMPLATES = (
    "scud-infra-redis", "scud-infra-pgbouncer", "scud-infra-postgres",
    "scud-infra-gateway", "scud-infra-backup", "scud-infra-host",
)
_AGENT_IFACE = [
    {
        "type": "1",  # Zabbix agent
        "main": "1",
        "useip": "0",
        "ip": "127.0.0.1",
        "dns": "zabbix_agent2",
        "port": "10050",
    }
]


def _ensure_host_group(url: str, token: str, name: str) -> str:
    gid = _get_id(url, token, "hostgroup.get", {"name": name}, "groupid")
    if gid is not None:
        return gid
    created = api_call("hostgroup.create", {"name": name}, url, auth=token)
    _log.info("created host group %s", name)
    return str(created["groupids"][0])  # type: ignore[index,call-overload]


def _ensure_host(
    url: str,
    token: str,
    host: str,
    group_id: str,
    template_names: list,
    interfaces: list | None,
) -> None:
    tids = []
    for name in template_names:
        tid = _get_id(url, token, "template.get", {"host": name}, "templateid")
        if tid is not None:
            tids.append({"templateid": tid})
    if not tids:
        _log.warning("no templates present for host %s — skipping", host)
        return
    existing = _get_id(url, token, "host.get", {"host": host}, "hostid")
    if existing is not None:
        try:
            api_call(
                "host.massadd",
                {"hosts": [{"hostid": existing}], "templates": tids},
                url,
                auth=token,
            )
        except ZabbixApiError as exc:
            # Usually "template already linked" — an idempotent no-op. Log at
            # debug so a genuine failure is still discoverable on re-run.
            _log.debug("host.massadd for %s: %s", host, exc)
        _log.info("host %s already exists (templates ensured)", host)
        return
    params: dict = {
        "host": host,
        "groups": [{"groupid": group_id}],
        "templates": tids,
    }
    if interfaces:
        params["interfaces"] = interfaces
    api_call("host.create", params, url, auth=token)
    _log.info("created host %s", host)


def provision_hosts(url: str, token: str) -> None:
    """Create one host per service + one infra host, linked to the imported
    templates. Idempotent (get-before-create). Hosts whose templates are not yet
    imported are skipped with a warning.
    """
    svc_group = _ensure_host_group(url, token, SERVICE_HOST_GROUP)
    for svc in _SERVICES:
        _ensure_host(
            url, token, f"scud-{svc}", svc_group, [f"scud-service-{svc}"], None
        )
    infra_group = _ensure_host_group(url, token, INFRA_HOST_GROUP)
    _ensure_host(
        url, token, INFRA_HOST_NAME, infra_group, list(_INFRA_TEMPLATES), _AGENT_IFACE
    )


def main() -> int:
    base = os.environ["ZABBIX_URL"].rstrip("/")
    url = base + API_PATH
    user = os.environ.get("ZABBIX_USER", "Admin")
    password = os.environ["ZABBIX_PASSWORD"]
    tpl_path = os.environ.get(
        "ZABBIX_TEMPLATE",
        os.path.join(os.path.dirname(__file__), "template-scud-services.yaml"),
    )
    alert_path = os.environ.get(
        "ZABBIX_ALERT_DELIVERY",
        os.path.join(os.path.dirname(__file__), "alert-delivery.yaml"),
    )
    infra_path = os.environ.get(
        "ZABBIX_INFRA_TEMPLATE",
        os.path.join(os.path.dirname(__file__), "template-scud-infra.yaml"),
    )
    with open(tpl_path, encoding="utf-8") as fh:
        yaml_text = fh.read()
    _log.info("logging in to %s as %s", base, user)
    token = login(url, user, password)
    _log.info("importing %s", tpl_path)
    import_template(url, token, yaml_text)
    _log.info("imported %s into %s", tpl_path, base)

    # Infra template (skipped if it IS the primary template, e.g. operator
    # overrode ZABBIX_TEMPLATE to point at it) — makes a single run one-shot.
    if os.path.exists(infra_path) and os.path.abspath(infra_path) != os.path.abspath(
        tpl_path
    ):
        with open(infra_path, encoding="utf-8") as fh:
            infra_yaml = fh.read()
        _log.info("importing %s", infra_path)
        import_template(url, token, infra_yaml)
        _log.info("imported %s into %s", infra_path, base)

    with open(alert_path, encoding="utf-8") as fh:
        alert_yaml = fh.read()
    _log.info("importing %s", alert_path)
    import_alert_delivery(url, token, alert_yaml)
    _log.info("imported %s into %s", alert_path, base)

    provision_alert_routing(url, token)
    provision_max_token(url, token)
    provision_hosts(url, token)

    # Dashboards last — they resolve itemids on the hosts created above. Imported
    # here (not at module top) so provision_dashboards can import this module
    # without a cycle.
    import provision_dashboards

    provision_dashboards.provision_dashboards(url, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
