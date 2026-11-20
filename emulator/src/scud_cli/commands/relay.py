"""`scud relay ...` — headless relay-board devices over MQTT."""
from __future__ import annotations

import asyncio

import typer

from scud_relay.fleet import run_fleet
from scud_relay.mqtt_transport import (
    DEFAULT_PHOTOS_PER_INCIDENT,
    MAX_PHOTOS_PER_INCIDENT,
    build_tls_context,
)
from scud_relay.provisioning import GatewaySession, provision_fleet

app = typer.Typer(name="relay", help="Headless relay-board MQTT devices.")


@app.command("run")
def run(count: int = typer.Option(1, "--count", "-n"),
        host: str = typer.Option("localhost", "--host"),
        port: int | None = typer.Option(
            None, "--port",
            help="Broker port; defaults to 1883, or 8883 when --tls"),
        base_id: int = typer.Option(1, "--base-id"),
        version: int = typer.Option(2, "--version"),
        ramp_up: float = typer.Option(
            0.0, "--ramp-up",
            help="Spread connections over N seconds (avoids a connect thundering herd)"),
        events: bool = typer.Option(
            False, "--events/--no-events",
            help="Cycle each device through stat mutations (power-loss/ERROR/offline) "
                 "so the backend emits device-asserted security events"),
        events_interval: float = typer.Option(
            30.0, "--events-interval",
            help="Seconds between security-event scenario steps (with --events)"),
        photos: bool = typer.Option(
            False, "--photos/--no-photos",
            help="Attach a synthetic tamper photo series (image/jpeg) to each error "
                 "incident. Implies --events (photos ride the error incidents)"),
        photos_per_incident: int = typer.Option(
            DEFAULT_PHOTOS_PER_INCIDENT, "--photos-per-incident",
            min=1, max=MAX_PHOTOS_PER_INCIDENT,
            help="Frames per photo series (with --photos); each frame is its own JPEG"),
        tls: bool = typer.Option(
            False, "--tls/--no-tls",
            help="Connect over TLS 1.2 (protocol §4 port 8883) instead of plaintext"),
        cafile: str | None = typer.Option(
            None, "--cafile", envvar="SCUD_MQTT_CAFILE",
            help="CA certificate that signed the broker certificate (with --tls)"),
        tls_insecure: bool = typer.Option(
            False, "--tls-insecure",
            help="Skip certificate verification — reachability probe only"),
        per_id_topics: bool = typer.Option(
            False, "--per-id-topics/--shared-topics",
            help="Publish to stat/{id}, rep/{id}, photos/{id} (ПИВ v2) instead of "
                 "the shared topics")):
    """Run a fleet of relay devices until interrupted."""
    # Photos ride the error incidents, so --photos is meaningless without the scenario;
    # enable it implicitly rather than silently sending nothing.
    if photos:
        events = True
    tls_context = build_tls_context(cafile, insecure=tls_insecure) if tls else None
    resolved_port = port if port is not None else (8883 if tls else 1883)
    asyncio.run(run_fleet(count, host, resolved_port, base_id, version, ramp_up_seconds=ramp_up,
                          events=events, events_interval_s=events_interval, photos=photos,
                          tls_context=tls_context, per_id_topics=per_id_topics,
                          photos_per_incident=photos_per_incident))


@app.command("provision")
def provision(
        count: int = typer.Option(1, "--count", "-n"),
        base_id: int = typer.Option(1, "--base-id"),
        gateway_url: str = typer.Option(
            "http://localhost", "--gateway-url", envvar="SCUD_GATEWAY_URL",
            help="Apache gateway base URL (e.g. http://localhost or https://stand)"),
        phone: str = typer.Option(
            "+79000000000", "--phone", envvar="SCUD_ADMIN_PHONE",
            help="Administrator/operator phone (2FA must be off for this account)"),
        password: str | None = typer.Option(
            None, "--password", envvar="SCUD_ADMIN_PASSWORD",
            help="Password for --phone; omit if using --access-token"),
        access_token: str | None = typer.Option(
            None, "--access-token", envvar="SCUD_ACCESS_TOKEN",
            help="Existing access_token cookie value (skips login)"),
        index_start: int = typer.Option(
            0, "--index-start",
            help="Offset into the address book — varies the generated addresses")):
    """Create N mqtt lockers (шкафы) with generated addresses in the backend.

    Relay ids run base_id..base_id+count-1 so a matching `scud relay run --base-id`
    fleet lines up. Each locker gets a distinct location_address + coordinates.
    """
    session = GatewaySession(gateway_url)
    if access_token:
        session.set_access_token(access_token)
    elif password:
        session.login(phone, password)
    else:
        typer.echo("provide --password (administrator/operator) or --access-token",
                   err=True)
        raise typer.Exit(2)

    outcomes = provision_fleet(session, count, base_id, index_start=index_start)
    created = 0
    for outcome in outcomes:
        if outcome.created:
            created += 1
            typer.echo(f"relay-{outcome.relay_id}: created {outcome.locker_id} "
                       f"@ {outcome.address}")
        else:
            typer.echo(f"relay-{outcome.relay_id}: skipped (status={outcome.status}) "
                       f"@ {outcome.address}")
    typer.echo(f"provisioned {created}/{count} lockers "
               f"(relay ids {base_id}..{base_id + count - 1})")
    if created == 0:
        raise typer.Exit(1)
