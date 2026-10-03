from __future__ import annotations

import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .strictjson import StrictJSONError, loads

MAX_CORE_RESPONSE_BYTES = 20 * 1024 * 1024
_SAFE_OPAQUE_MIN = 32
_SAFE_OPAQUE_MAX = 256


class CoreProtocolError(RuntimeError):
    """ContextBridge rejected or violated adapter protocol v2."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


@dataclass(frozen=True)
class WorkLease:
    job: dict[str, Any]
    generation: int
    capability: str
    observation_only: bool = False


def _validate_base_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise CoreProtocolError("ContextBridge URL is invalid") from exc
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and not loopback):
        raise CoreProtocolError("ContextBridge URL must use HTTPS or loopback HTTP")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise CoreProtocolError("ContextBridge URL must be a credential-free origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise CoreProtocolError("ContextBridge URL must not contain a path, query, or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _read_bounded(response: Any, maximum: int = MAX_CORE_RESPONSE_BYTES) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > maximum:
                raise CoreProtocolError("ContextBridge response exceeds the byte limit")
        except ValueError as exc:
            raise CoreProtocolError("ContextBridge returned an invalid Content-Length") from exc
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, maximum + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise CoreProtocolError("ContextBridge response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


class ContextBridgeV2Client:
    def __init__(
        self,
        base_url: str,
        token: str,
        profile: str,
        *,
        endpoint_id: int = 1,
        timeout_seconds: int = 35,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        clean_token = token.strip()
        if not 32 <= len(clean_token) <= 4_096 or any(character in clean_token for character in "\r\n\x00"):
            raise CoreProtocolError("adapter credential must contain one 32-4096 character secret")
        safe_profile = profile and len(profile) <= 80 and all(
            character.isalnum() or character in "._-" for character in profile
        )
        if not safe_profile:
            raise CoreProtocolError("profile must be a bounded ContextBridge identifier")
        if endpoint_id < 1:
            raise CoreProtocolError("endpoint_id must be positive")
        self.token = clean_token
        self.profile = profile
        self.endpoint_id = endpoint_id
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        allow_no_content: bool = False,
    ) -> tuple[int, Any]:
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(  # noqa: S310  # nosec B310
            urljoin(self.base_url + "/", path.lstrip("/")),
            data=encoded,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if encoded is not None else {}),
                **(dict(headers) if headers else {}),
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = response.status
                raw = _read_bounded(response)
        except HTTPError as exc:
            raw = _read_bounded(exc, 64 * 1024)
            detail = ""
            try:
                parsed = loads(raw, max_bytes=64 * 1024)
                if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                    detail = ": " + parsed["error"][:240]
            except StrictJSONError:
                pass
            raise CoreProtocolError(f"ContextBridge rejected the request (HTTP {exc.code}{detail})") from exc
        except (OSError, URLError) as exc:
            raise CoreProtocolError("ContextBridge connection failed") from exc
        if status == 204 and allow_no_content:
            return status, None
        if not raw:
            return status, None
        try:
            return status, loads(raw, max_bytes=MAX_CORE_RESPONSE_BYTES)
        except StrictJSONError as exc:
            raise CoreProtocolError("ContextBridge returned invalid JSON") from exc

    def status(self) -> dict[str, Any]:
        _, payload = self._request("v2/adapter/status")
        if not isinstance(payload, dict) or payload.get("protocol") != "contextbridge.adapter.v2":
            raise CoreProtocolError("ContextBridge did not advertise adapter protocol v2")
        profiles = payload.get("allowed_profiles")
        if not isinstance(profiles, list) or self.profile not in profiles:
            raise CoreProtocolError("adapter principal is not allowed to use the requested profile")
        return payload

    def profiles(self) -> dict[str, Any]:
        _, payload = self._request("v2/adapter/profiles")
        if not isinstance(payload, dict) or not isinstance(payload.get(self.profile), dict):
            raise CoreProtocolError("requested adapter profile is missing")
        return payload

    def heartbeat(self, capability: str = "", *, busy: bool = False, actions: tuple[str, ...] = ()) -> str:
        endpoint: dict[str, Any] = {
            "id": self.endpoint_id,
            "profile": self.profile,
            # Core only schedules work onto an endpoint that explicitly reports
            # waiting.  "idle" is a display state, not v2 routing evidence.
            "state": "busy" if busy else "waiting",
            "models": list(actions)[:20],
        }
        if capability:
            endpoint["endpoint_capability"] = capability
        _, payload = self._request(
            "v2/adapter/heartbeat",
            method="POST",
            body={
                "connected": True,
                "ready": not busy,
                "state": "busy" if busy else "waiting",
                "adapter": "actions",
                "adapter_version": "0.1.0",
                "active_endpoints": 1,
                "busy_endpoints": 1 if busy else 0,
                "endpoints": [endpoint],
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("endpoints"), list):
            raise CoreProtocolError("heartbeat response is malformed")
        for item in payload["endpoints"]:
            if (
                isinstance(item, dict)
                and item.get("profile") == self.profile
                and item.get("endpoint_id") == self.endpoint_id
            ):
                returned = item.get("endpoint_capability")
                if isinstance(returned, str) and _SAFE_OPAQUE_MIN <= len(returned) <= _SAFE_OPAQUE_MAX:
                    return returned
        raise CoreProtocolError("heartbeat omitted the endpoint capability")

    def next(self, endpoint_capability: str, *, wait: bool = True) -> WorkLease | None:
        query = urlencode({"profile": self.profile, "endpoint_id": self.endpoint_id, "wait": "1" if wait else "0"})
        status, payload = self._request(
            f"v2/adapter/jobs/next?{query}",
            headers={"X-ContextBridge-Endpoint-Capability": endpoint_capability},
            allow_no_content=True,
        )
        if status == 204:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("job"), dict):
            raise CoreProtocolError("adapter lease is malformed")
        generation = payload.get("lease_generation")
        capability = payload.get("lease_capability")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise CoreProtocolError("adapter lease generation is invalid")
        if not isinstance(capability, str) or not _SAFE_OPAQUE_MIN <= len(capability) <= _SAFE_OPAQUE_MAX:
            raise CoreProtocolError("adapter lease capability is invalid")
        observation_only = payload.get("observation_only", False)
        if not isinstance(observation_only, bool):
            raise CoreProtocolError("adapter lease observation state is invalid")
        return WorkLease(
            job=payload["job"],
            generation=generation,
            capability=capability,
            observation_only=observation_only,
        )

    @staticmethod
    def _lease_headers(work: WorkLease) -> dict[str, str]:
        return {
            "X-ContextBridge-Lease-Generation": str(work.generation),
            "X-ContextBridge-Lease-Capability": work.capability,
        }

    @staticmethod
    def _job_id(work: WorkLease) -> str:
        raw = work.job.get("id")
        if not isinstance(raw, str) or not raw or len(raw) > 256 or any(character in raw for character in "\r\n\x00"):
            raise CoreProtocolError("adapter lease job ID is invalid")
        return quote(raw, safe="")

    def claim_mutation(self, work: WorkLease) -> None:
        self._request(
            f"v2/adapter/jobs/{self._job_id(work)}/claim",
            method="POST",
            headers=self._lease_headers(work),
            body={"action": "mutate"},
        )

    def progress(self, work: WorkLease, sequence: int, text: str, percent: int, *, busy: bool = True) -> None:
        self._request(
            f"v2/adapter/jobs/{self._job_id(work)}/progress",
            method="POST",
            headers=self._lease_headers(work),
            body={"sequence": sequence, "text": text[:2_000], "phase": "submitting", "percent": percent, "busy": busy},
        )

    def check_lease(self, work: WorkLease) -> None:
        _, payload = self._request(
            f"v2/adapter/jobs/{self._job_id(work)}/lease",
            headers=self._lease_headers(work),
        )
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise CoreProtocolError("ContextBridge returned malformed lease status")
        expires_at = payload.get("lease_expires_at")
        if not isinstance(expires_at, str):
            raise CoreProtocolError("ContextBridge lease status omitted its expiry")
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CoreProtocolError("ContextBridge lease expiry is invalid") from exc
        if parsed.tzinfo is None:
            raise CoreProtocolError("ContextBridge lease expiry must include a timezone")

    def complete(self, work: WorkLease, output: dict[str, Any]) -> None:
        self._request(
            f"v2/adapter/jobs/{self._job_id(work)}/complete",
            method="POST",
            headers=self._lease_headers(work),
            body=output,
        )
