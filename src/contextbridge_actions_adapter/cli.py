from __future__ import annotations

import argparse
import json
import os
import platform
import re
import stat
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from . import __version__
from .contracts import MAX_PAYLOAD_BYTES
from .core_client import ContextBridgeV2Client, CoreProtocolError
from .relay_client import RelayClient, RelayProtocolError
from .service import ActionsAdapterService, MutationOutcomeUnknown, inspect_profile
from .store import ActionStore, ActionStoreError


def _secret(path: str, label: str) -> str:
    candidate = Path(path).expanduser()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise CoreProtocolError(f"{label} token file is unavailable") from exc
    if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= 8_192:
        raise CoreProtocolError(f"{label} token file must be a small regular non-symlink file")
    try:
        value = candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise CoreProtocolError(f"{label} token file is unreadable") from exc
    return value


def _bounded_file(path: str) -> bytes:
    candidate = Path(path).expanduser()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ActionStoreError("payload file is unavailable") from exc
    if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= MAX_PAYLOAD_BYTES:
        raise ActionStoreError("payload file must be a bounded regular non-symlink file")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise ActionStoreError("payload file is unreadable") from exc


def _safe_instance() -> str:
    value = re.sub(r"[^a-z0-9._:-]+", "-", platform.node().lower()).strip("-.")
    return (value or "local")[:80] + "-actions"


def _add_core(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default="", help="ContextBridge HTTPS or loopback HTTP origin")
    parser.add_argument("--profile", default="", help="scoped adapter profile")
    parser.add_argument("--token-file", default="", help="private adapter-v2 credential file")
    parser.add_argument("--endpoint-id", type=int, default=1, help="positive local endpoint ID")


def _add_presence(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--relay-url", default="", help="ContextBridge relay HTTPS or loopback HTTP origin")
    parser.add_argument(
        "--presence-token-file",
        default="",
        help="producer credential with the same subject but no scheduled-action authority",
    )
    parser.add_argument("--adapter-id", default="actions-primary", help="stable lowercase presence ID")
    parser.add_argument("--instance-id", default=_safe_instance(), help="lowercase deployment instance ID")


def _add_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--relay-url", default="", help="ContextBridge relay HTTPS or loopback HTTP origin")
    parser.add_argument("--producer-token-file", required=True, help="scheduled-action producer credential file")
    parser.add_argument("--tenant-id", default="", help="allowed tenant scope; empty uses owner scope")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contextbridge-actions-adapter")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="execute confirmed scoped actions through adapter protocol v2")
    _add_core(run)
    _add_presence(run)
    run.add_argument("--once", action="store_true", help="process at most one immediately available job")

    doctor = commands.add_parser("doctor", help="verify core, relay, store, credential, and UID bindings")
    _add_core(doctor)
    _add_presence(doctor)

    presence = commands.add_parser("presence", help="register or renew the external adapter presence")
    _add_presence(presence)

    destination = commands.add_parser("destination", help="manage owner-scoped opaque destinations")
    destination_commands = destination.add_subparsers(dest="destination_command", required=True)
    destination_add = destination_commands.add_parser("add", help="register one GitHub repository")
    destination_add.add_argument("--database", required=True)
    _add_scope(destination_add)
    destination_add.add_argument("--repository", required=True)
    destination_state = destination_commands.add_parser("set-active", help="enable or disable one opaque destination")
    destination_state.add_argument("--database", required=True)
    _add_scope(destination_state)
    destination_state.add_argument("destination_ref")
    destination_state.add_argument("--active", choices=("true", "false"), required=True)
    destination_list = destination_commands.add_parser("list", help="list destinations in the authenticated scope")
    destination_list.add_argument("--database", required=True)
    _add_scope(destination_list)

    payload = commands.add_parser("payload", help="stage bounded owner-scoped mutation content")
    payload_commands = payload.add_subparsers(dest="payload_command", required=True)
    stage = payload_commands.add_parser("stage", help="stage one strict action payload")
    stage.add_argument("--database", required=True)
    _add_scope(stage)
    stage.add_argument("--destination-ref", required=True)
    stage.add_argument("--action-kind", required=True)
    stage.add_argument("--file", required=True)
    stage.add_argument("--ttl-hours", type=int, default=24)

    attempt = commands.add_parser("attempt", help="inspect one content-free occurrence state")
    attempt.add_argument("--database", required=True)
    attempt.add_argument("schedule_id")
    attempt.add_argument("occurrence", type=int)
    return parser


def _core(args: argparse.Namespace) -> ContextBridgeV2Client:
    token_file = args.token_file or os.environ.get("CONTEXTBRIDGE_ADAPTER_TOKEN_FILE", "")
    if not token_file:
        raise CoreProtocolError("--token-file or CONTEXTBRIDGE_ADAPTER_TOKEN_FILE is required")
    return ContextBridgeV2Client(
        args.url or os.environ.get("CONTEXTBRIDGE_URL", "http://127.0.0.1:32145"),
        _secret(token_file, "adapter"),
        args.profile or os.environ.get("CONTEXTBRIDGE_ADAPTER_PROFILE", "github-actions"),
        endpoint_id=args.endpoint_id,
    )


def _producer_relay(args: argparse.Namespace) -> RelayClient:
    token_file = args.producer_token_file or os.environ.get("CONTEXTBRIDGE_PRODUCER_TOKEN_FILE", "")
    if not token_file:
        raise RelayProtocolError("--producer-token-file or CONTEXTBRIDGE_PRODUCER_TOKEN_FILE is required")
    return RelayClient(
        args.relay_url or os.environ.get("CONTEXTBRIDGE_RELAY_URL", "http://127.0.0.1:32150"),
        _secret(token_file, "producer"),
    )


def _presence_relay(args: argparse.Namespace) -> RelayClient:
    token_file = args.presence_token_file or os.environ.get("CONTEXTBRIDGE_PRESENCE_TOKEN_FILE", "")
    if not token_file:
        raise RelayProtocolError("--presence-token-file or CONTEXTBRIDGE_PRESENCE_TOKEN_FILE is required")
    relay = RelayClient(
        args.relay_url or os.environ.get("CONTEXTBRIDGE_RELAY_URL", "http://127.0.0.1:32150"),
        _secret(token_file, "presence"),
    )
    identity = relay.whoami()
    if identity.scheduled_actions:
        raise RelayProtocolError("presence credential must not grant scheduled-action authority")
    return relay


def _identity_scope(args: argparse.Namespace) -> tuple[RelayClient, str, str]:
    relay = _producer_relay(args)
    identity = relay.whoami()
    if not identity.scheduled_actions:
        raise RelayProtocolError("producer credential lacks scheduled-action authority")
    tenant = args.tenant_id.strip()
    if identity.allowed_tenants:
        if tenant not in identity.allowed_tenants:
            raise RelayProtocolError("tenant_id is outside the producer credential scope")
    elif tenant:
        raise RelayProtocolError("owner-scoped producer credential cannot select a tenant_id")
    return relay, identity.subject, tenant


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"run", "doctor"}:
            core = _core(args)
            relay = _presence_relay(args)
            if args.command == "run":
                completed = ActionsAdapterService(core).run(
                    relay,
                    adapter_id=args.adapter_id,
                    instance_id=args.instance_id,
                    once=args.once,
                )
                print(json.dumps({"ok": True, "completed": completed}, separators=(",", ":")))
                return 0
            status = core.status()
            profiles = core.profiles()
            options, readiness = inspect_profile(profiles[core.profile])
            identity = relay.whoami()
            presence = relay.heartbeat(adapter_id=args.adapter_id, instance_id=args.instance_id)
            if presence.adapter_uid != options.adapter_uid:
                raise RelayProtocolError("profile adapter_uid does not match the authenticated relay presence")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "protocol": status.get("protocol"),
                        "profile": core.profile,
                        "owner_subject": identity.subject,
                        "presence_least_privilege": not identity.scheduled_actions,
                        "presence": {
                            "adapter_uid": presence.adapter_uid,
                            "enabled": presence.enabled,
                            "available": presence.available,
                        },
                        "readiness": readiness,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "presence":
            relay = _presence_relay(args)
            identity = relay.whoami()
            lease = relay.heartbeat(adapter_id=args.adapter_id, instance_id=args.instance_id)
            print(
                json.dumps(
                    {
                        "owner_subject": identity.subject,
                        "adapter_uid": lease.adapter_uid,
                        "enabled": lease.enabled,
                        "available": lease.available,
                    },
                    separators=(",", ":"),
                )
            )
            return 0

        if args.command == "destination":
            store = ActionStore(Path(args.database))
            _, owner, tenant = _identity_scope(args)
            if args.destination_command == "add":
                reference = store.add_github_destination(owner, tenant, args.repository)
                print(json.dumps({"destination_ref": reference, "owner_subject": owner, "tenant_id": tenant}))
                return 0
            if args.destination_command == "list":
                print(
                    json.dumps(
                        {
                            "owner_subject": owner,
                            "tenant_id": tenant,
                            "destinations": store.list_destinations(owner, tenant),
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            changed = store.set_destination_active(
                args.destination_ref, args.active == "true", owner, tenant
            )
            if not changed:
                raise ActionStoreError("destination was not found")
            print(json.dumps({"destination_ref": args.destination_ref, "active": args.active == "true"}))
            return 0

        if args.command == "payload":
            store = ActionStore(Path(args.database))
            _, owner, tenant = _identity_scope(args)
            reference = store.stage_payload(
                owner,
                tenant,
                args.destination_ref,
                args.action_kind,
                _bounded_file(args.file),
                ttl=timedelta(hours=args.ttl_hours),
            )
            print(json.dumps({"payload_ref": reference, "owner_subject": owner, "tenant_id": tenant}))
            return 0

        store = ActionStore(Path(args.database))
        state = store.attempt_state(args.schedule_id, args.occurrence)
        print(json.dumps({"schedule_id": args.schedule_id, "occurrence": args.occurrence, "state": state}))
        return 0
    except MutationOutcomeUnknown as exc:
        print(f"contextbridge-actions-adapter: UNKNOWN: {exc}", file=sys.stderr)
        return 2
    except (ActionStoreError, CoreProtocolError, RelayProtocolError, OSError, UnicodeError, ValueError) as exc:
        print(f"contextbridge-actions-adapter: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
