from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .strictjson import StrictJSONError, loads

SCHEDULED_ACTION_SCHEMA = "contextbridge.scheduled-adapter-action.v1"
SUPPORTED_ACTIONS = ("github.issue.create", "github.issue.comment", "github.issue.update")
MAX_PAYLOAD_BYTES = 128 * 1024
_ADAPTER_UID = re.compile(r"^adp_[0-9a-f]{32}$")
_SCHEDULE_ID = re.compile(r"^sact_[0-9a-f]{32}$")
_DESTINATION_REF = re.compile(r"^dst_[0-9a-f]{32}$")
_PAYLOAD_REF = re.compile(r"^ref_[0-9a-f]{32}$")
_ACTION_KIND = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,79}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


class ContractError(ValueError):
    """A stable, content-minimizing contract failure."""


@dataclass(frozen=True)
class RuntimeOptions:
    adapter_uid: str
    database_path: Path
    github_token_file: Path
    allowed_actions: tuple[str, ...]
    require_bound_egress: bool = True


@dataclass(frozen=True)
class ActionEnvelope:
    schedule_id: str
    adapter_uid: str
    occurrence: int
    action_kind: str
    destination_ref: str
    payload_ref: str
    scheduled_for: datetime
    expires_at: datetime
    owner_subject: str
    tenant_id: str


def _exact_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"unknown {label} properties: {', '.join(unknown)}")


def _required_string(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ContractError(f"{name} must contain 1..{maximum} UTF-8 bytes")
    if any(ord(character) < 32 for character in value):
        raise ContractError(f"{name} must not contain control characters")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ContractError(f"{name} must be a bounded RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} must be a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_profile_options(profile: Any) -> RuntimeOptions:
    if not isinstance(profile, dict):
        raise ContractError("adapter profile must be an object")
    raw = profile.get("options")
    if not isinstance(raw, dict):
        raise ContractError("adapter profile options must be an object")
    _exact_keys(
        raw,
        {"adapter_uid", "database_path", "github_token_file", "allowed_actions", "require_bound_egress"},
        "profile option",
    )
    adapter_uid = _required_string(raw.get("adapter_uid"), "adapter_uid", 80)
    if not _ADAPTER_UID.fullmatch(adapter_uid):
        raise ContractError("adapter_uid must be a ContextBridge adp_ identifier")
    database_path = Path(_required_string(raw.get("database_path"), "database_path", 4_096)).expanduser()
    token_path = Path(_required_string(raw.get("github_token_file"), "github_token_file", 4_096)).expanduser()
    actions = raw.get("allowed_actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= len(SUPPORTED_ACTIONS):
        raise ContractError("allowed_actions must explicitly contain 1..3 action kinds")
    normalized: list[str] = []
    for action in actions:
        if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
            raise ContractError("allowed_actions contains an unsupported action")
        if action not in normalized:
            normalized.append(action)
    require_bound = raw.get("require_bound_egress", True)
    if not isinstance(require_bound, bool):
        raise ContractError("require_bound_egress must be true or false")
    return RuntimeOptions(adapter_uid, database_path, token_path, tuple(normalized), require_bound)


def parse_action_job(job: Any, options: RuntimeOptions, *, now: datetime | None = None) -> ActionEnvelope:
    if not isinstance(job, dict):
        raise ContractError("leased job must be an object")
    if job.get("task") != "scheduled_action" or job.get("provider") != "adapter":
        raise ContractError("job is not a reserved scheduled adapter action")
    if options.require_bound_egress and (
        job.get("contextbridge_egress") != "remote_allowed"
        or job.get("contextbridge_provider_classification") != "remote"
    ):
        raise ContractError("job lacks a relay-bound remote egress decision")
    owner = _required_string(job.get("contextbridge_owner_subject"), "contextbridge_owner_subject", 120)
    tenant = job.get("contextbridge_tenant_id")
    if (
        not isinstance(tenant, str)
        or len(tenant.encode("utf-8")) > 200
        or any(ord(character) < 32 for character in tenant)
    ):
        raise ContractError("contextbridge_tenant_id must contain at most 200 UTF-8 bytes without controls")
    metadata = job.get("metadata")
    if not isinstance(metadata, dict):
        raise ContractError("job.metadata must be an object")
    _exact_keys(metadata, {"contextbridge_scheduled_action"}, "metadata")
    action = metadata.get("contextbridge_scheduled_action")
    if not isinstance(action, dict):
        raise ContractError("scheduled action envelope must be an object")
    expected = {
        "schema",
        "schedule_id",
        "adapter_uid",
        "occurrence",
        "action_kind",
        "destination_ref",
        "payload_ref",
        "scheduled_for",
        "expires_at",
    }
    _exact_keys(action, expected, "scheduled action")
    if set(action) != expected or action.get("schema") != SCHEDULED_ACTION_SCHEMA:
        raise ContractError("scheduled action envelope is incomplete or has the wrong schema")
    schedule_id = _required_string(action.get("schedule_id"), "schedule_id", 80)
    adapter_uid = _required_string(action.get("adapter_uid"), "adapter_uid", 80)
    action_kind = _required_string(action.get("action_kind"), "action_kind", 80)
    destination_ref = _required_string(action.get("destination_ref"), "destination_ref", 80)
    payload_ref = _required_string(action.get("payload_ref"), "payload_ref", 80)
    occurrence = action.get("occurrence")
    if not _SCHEDULE_ID.fullmatch(schedule_id):
        raise ContractError("schedule_id is invalid")
    if adapter_uid != options.adapter_uid or not _ADAPTER_UID.fullmatch(adapter_uid):
        raise ContractError("scheduled action targets another adapter UID")
    if not _ACTION_KIND.fullmatch(action_kind) or action_kind not in options.allowed_actions:
        raise ContractError("scheduled action kind is not allowed")
    if not _DESTINATION_REF.fullmatch(destination_ref) or not _PAYLOAD_REF.fullmatch(payload_ref):
        raise ContractError("scheduled action references are invalid")
    if isinstance(occurrence, bool) or not isinstance(occurrence, int) or not 1 <= occurrence <= 366:
        raise ContractError("occurrence must be an integer between 1 and 366")
    scheduled_for = _timestamp(action.get("scheduled_for"), "scheduled_for")
    expires_at = _timestamp(action.get("expires_at"), "expires_at")
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= scheduled_for or instant >= expires_at:
        raise ContractError("scheduled action delivery window has expired")
    if scheduled_for > instant + timedelta(seconds=30):
        raise ContractError("scheduled action was delivered before its due time")
    return ActionEnvelope(
        schedule_id,
        adapter_uid,
        occurrence,
        action_kind,
        destination_ref,
        payload_ref,
        scheduled_for,
        expires_at,
        owner,
        tenant,
    )


def validate_repository(repository: str) -> str:
    value = repository.strip()
    if not _REPOSITORY.fullmatch(value) or ".." in value:
        raise ContractError("repository must use bounded GitHub owner/name syntax")
    return value


def parse_staged_payload(raw: str | bytes, action_kind: str) -> dict[str, Any]:
    try:
        value = loads(raw, max_bytes=MAX_PAYLOAD_BYTES)
    except StrictJSONError as exc:
        raise ContractError(str(exc)) from exc
    if not isinstance(value, dict):
        raise ContractError("payload root must be an object")
    if action_kind == "github.issue.create":
        _exact_keys(value, {"title", "body"}, "payload")
        if set(value) != {"title", "body"}:
            raise ContractError("issue creation requires title and body")
        _required_string(value.get("title"), "title", 256)
        _required_string(value.get("body"), "body", 64 * 1024)
        return value
    if action_kind == "github.issue.comment":
        _exact_keys(value, {"issue_number", "body"}, "payload")
        if set(value) != {"issue_number", "body"}:
            raise ContractError("issue comment requires issue_number and body")
        _issue_number(value.get("issue_number"))
        _required_string(value.get("body"), "body", 64 * 1024)
        return value
    if action_kind == "github.issue.update":
        _exact_keys(value, {"issue_number", "title", "body", "state"}, "payload")
        _issue_number(value.get("issue_number"))
        changes = set(value) - {"issue_number"}
        if not changes:
            raise ContractError("issue update requires title, body, or state")
        if "title" in value:
            _required_string(value["title"], "title", 256)
        if "body" in value:
            _required_string(value["body"], "body", 64 * 1024)
        if "state" in value and value["state"] not in {"open", "closed"}:
            raise ContractError("state must be open or closed")
        return value
    raise ContractError("action kind is unsupported")


def _issue_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2_147_483_647:
        raise ContractError("issue_number must be a positive integer")
    return value
