from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from contextbridge_actions_adapter.contracts import RuntimeOptions
from contextbridge_actions_adapter.core_client import ContextBridgeV2Client, WorkLease
from contextbridge_actions_adapter.relay_client import RelayClient
from contextbridge_actions_adapter.service import ActionsAdapterService
from contextbridge_actions_adapter.store import ActionStore


def _free_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{listener.getsockname()[1]}"


def _request(base: str, path: str, *, token: str = "", method: str = "GET", body: Any = None) -> Any:
    encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=encoded, method=method, headers=headers)  # noqa: S310
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310
            raw = response.read(256 * 1024 + 1)
    except HTTPError as exc:
        detail = exc.read(64 * 1024).decode("utf-8", "replace")
        raise AssertionError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    if len(raw) > 256 * 1024:
        raise AssertionError(f"{method} {path} exceeded the test response bound")
    return json.loads(raw) if raw else None


def _wait_health(base: str, process: subprocess.Popen[bytes], label: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"ContextBridge exited before its {label} became healthy")
        try:
            health = _request(base, "/health")
            if isinstance(health, dict) and health.get("ok") is True:
                return
        except (AssertionError, OSError, URLError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"ContextBridge {label} did not become healthy")


class _FakeGitHub:
    def __init__(self) -> None:
        self.calls = 0

    def preflight(self, repository: str, action_kind: str, payload: dict[str, Any]) -> None:
        if repository != "IamAngusU/ContextBridge" or action_kind != "github.issue.comment":
            raise AssertionError("unexpected staged provider target")
        if payload != {"issue_number": 126, "body": "bounded public-core proof"}:
            raise AssertionError("unexpected staged provider payload")

    def execute(self, _repository: str, _action_kind: str, _payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {
            "provider": "github",
            "external_id": "126#proof",
            "url": "https://github.com/IamAngusU/ContextBridge/issues/126",
        }


class PublicCoreBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CB_TEST_CORE_BINARY"), "set CB_TEST_CORE_BINARY to a public Core binary")
    def test_confirmed_action_crosses_real_core_once_with_authenticated_scope(self) -> None:
        binary = os.environ["CB_TEST_CORE_BINARY"]
        owner = "actions-e2e-owner"
        tenant = "tenant-a"
        action_kind = "github.issue.comment"
        operator = "o" * 32
        admin = "d" * 32
        adapter_token = "a" * 32

        with tempfile.TemporaryDirectory(prefix="contextbridge-actions-e2e-") as directory:
            root = Path(directory)
            local_address, relay_address = _free_address(), _free_address()
            local_url, relay_url = f"http://{local_address}", f"http://{relay_address}"
            config = root / "core.yml"
            config.write_text(
                f"""version: 1
server:
  listen: {local_address}
  token: {operator}
storage:
  directory: "{(root / 'data').as_posix()}"
  inbox: "{(root / 'inbox').as_posix()}"
  models: "{(root / 'models').as_posix()}"
routes:
  default:
    provider: adapter
    adapter_profile: github-actions
    task: scheduled_action
    timeout_seconds: 30
providers:
  adapter:
    auth_mode: scoped
    lease_seconds: 30
    principals:
      actions:
        token: {adapter_token}
        allowed_profiles: [github-actions]
adapter_profiles:
  github-actions:
    label: GitHub actions E2E
    driver: contextbridge-actions
cluster:
  relay:
    enabled: true
    listen: {relay_address}
    public_url: {relay_url}
    database: "{(root / 'relay.db').as_posix()}"
    admin_token: {admin}
  worker:
    enabled: true
    relay_url: {relay_url}
    identity_file: "{(root / 'worker.json').as_posix()}"
    node_name: actions-e2e
    local_url: {local_url}
    local_token: {operator}
    heartbeat_seconds: 1
    allowed_tasks: [scheduled_action]
    allowed_providers: [adapter]
  policies:
    allowed_tasks: [scheduled_action]
    execution:
      enabled: true
      tenant_mode: open
      local_providers: []
      remote_providers: []
      adapter_profile_classifications:
        github-actions: remote
      default:
        egress: any
        allowed_providers: [adapter]
""",
                encoding="utf-8",
            )
            paired = subprocess.run(  # noqa: S603
                [binary, "pair", "--config", str(config)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(paired.returncode, 0, paired.stderr.decode("utf-8", "replace"))

            log_path = root / "core.log"
            with log_path.open("wb") as log:
                environment = dict(os.environ)
                environment["CONTEXTBRIDGE_UPDATES_EXTERNAL"] = "1"
                process = subprocess.Popen(  # noqa: S603
                    [binary, "run", "--config", str(config)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                )
                try:
                    _wait_health(relay_url, process, "relay")
                    _wait_health(local_url, process, "local bridge")
                    presence_token = self._create_producer(relay_url, admin, owner, tenant)
                    presence_relay = RelayClient(relay_url, presence_token)
                    presence = presence_relay.heartbeat(adapter_id="actions-e2e", instance_id="test")

                    store = ActionStore(root / "actions.db")
                    destination = store.add_github_destination(owner, tenant, "IamAngusU/ContextBridge")
                    payload = store.stage_payload(
                        owner,
                        tenant,
                        destination,
                        action_kind,
                        b'{"issue_number":126,"body":"bounded public-core proof"}',
                        ttl=timedelta(hours=1),
                    )
                    approval_token = self._create_producer(
                        relay_url,
                        admin,
                        owner,
                        tenant,
                        scheduled={
                            "schema": "contextbridge.scheduled-action-policy.v1",
                            "targets": [
                                {
                                    "adapter_uid": presence.adapter_uid,
                                    "adapter_profile": "github-actions",
                                    "adapter_principal": "actions",
                                    "action_kinds": [action_kind],
                                    "destination_refs": [destination],
                                }
                            ],
                        },
                    )
                    due = datetime.now(timezone.utc) + timedelta(seconds=4)
                    preview = _request(
                        relay_url,
                        "/v1/cluster/scheduled-actions/preview",
                        token=approval_token,
                        method="POST",
                        body={
                            "schema": "contextbridge.scheduled-action-request.v1",
                            "adapter_uid": presence.adapter_uid,
                            "action_kind": action_kind,
                            "destination_ref": destination,
                            "payload_ref": payload,
                            "tenant_id": tenant,
                            "start_at": due.isoformat().replace("+00:00", "Z"),
                            "timezone": "UTC",
                            "delivery_window_seconds": 300,
                        },
                    )
                    schedule_id = preview["id"]
                    _request(
                        relay_url,
                        f"/v1/cluster/scheduled-actions/{schedule_id}/confirm",
                        token=approval_token,
                        method="POST",
                        body={},
                    )

                    client = ContextBridgeV2Client(local_url, adapter_token, "github-actions")
                    self.assertEqual(client.status()["protocol"], "contextbridge.adapter.v2")
                    endpoint_capability = client.heartbeat(actions=(action_kind,))
                    work = self._wait_work(
                        client,
                        endpoint_capability,
                        relay_url=relay_url,
                        approval_token=approval_token,
                        schedule_id=schedule_id,
                    )
                    self.assertEqual(work.job.get("contextbridge_owner_subject"), owner)
                    self.assertEqual(work.job.get("contextbridge_tenant_id"), tenant)

                    provider = _FakeGitHub()
                    options = RuntimeOptions(
                        presence.adapter_uid,
                        store.path,
                        root / "unused-github.token",
                        (action_kind,),
                    )
                    ActionsAdapterService(client).process(
                        work,
                        options,
                        store,
                        provider,
                        relay=presence_relay,
                        adapter_id="actions-e2e",
                        instance_id="test",
                    )
                    self.assertEqual(provider.calls, 1)
                    self.assertEqual(store.attempt_state(schedule_id, 1), "completed")
                    self._wait_schedule(relay_url, approval_token, schedule_id)
                except BaseException as exc:
                    log.flush()
                    diagnostic = log_path.read_text(encoding="utf-8", errors="replace")[-16_000:]
                    for secret in (operator, admin, adapter_token):
                        diagnostic = diagnostic.replace(secret, "[redacted]")
                    raise AssertionError(f"public Core boundary failed; Core log follows:\n{diagnostic}") from exc
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

    @staticmethod
    def _create_producer(
        relay_url: str,
        admin: str,
        subject: str,
        tenant: str,
        *,
        scheduled: dict[str, Any] | None = None,
    ) -> str:
        limits: dict[str, Any] = {"allowed_tenants": [tenant], "providers": ["adapter"]}
        if scheduled is not None:
            limits["scheduled_actions"] = scheduled
        response = _request(
            relay_url,
            "/v1/cluster/tokens",
            token=admin,
            method="POST",
            body={
                "role": "producer",
                "subject": subject,
                "lifetime_hours": 1,
                "producer_limits": limits,
            },
        )
        token = response.get("token") if isinstance(response, dict) else None
        if not isinstance(token, str):
            raise AssertionError("relay did not return a producer token")
        return token

    @staticmethod
    def _wait_work(
        client: ContextBridgeV2Client,
        capability: str,
        *,
        relay_url: str,
        approval_token: str,
        schedule_id: str,
    ) -> WorkLease:
        deadline = time.monotonic() + 20
        schedule: Any = None
        while time.monotonic() < deadline:
            work = client.next(capability, wait=False)
            if work is not None:
                return work
            schedule = _request(
                relay_url,
                f"/v1/cluster/scheduled-actions/{schedule_id}",
                token=approval_token,
            )
            if isinstance(schedule, dict) and schedule.get("status") in {
                "failed",
                "unknown",
                "cancelled",
                "expired",
            }:
                raise AssertionError(f"scheduled action ended before dispatch: {schedule}")
            time.sleep(0.1)
        route: Any = None
        job: Any = None
        if isinstance(schedule, dict) and isinstance(schedule.get("current_job_id"), str):
            job_id = schedule["current_job_id"]
            job = _request(relay_url, f"/v1/cluster/jobs/{job_id}", token=approval_token)
            try:
                route = _request(
                    relay_url,
                    f"/v1/cluster/jobs/{job_id}/route",
                    token=approval_token,
                )
            except AssertionError as exc:
                route = str(exc)
        nodes = _request(relay_url, "/v1/cluster/nodes", token=approval_token)
        node_values = nodes.get("nodes", []) if isinstance(nodes, dict) else nodes
        node_summary = []
        if isinstance(node_values, list):
            for node in node_values:
                if not isinstance(node, dict):
                    continue
                capabilities = node.get("capabilities", {})
                if not isinstance(capabilities, dict):
                    capabilities = {}
                node_summary.append(
                    {
                        "id": node.get("id"),
                        "connected": node.get("connected"),
                        "state": node.get("state"),
                        "tasks": capabilities.get("tasks"),
                        "providers": capabilities.get("providers"),
                        "adapter_sessions": capabilities.get("adapter_sessions"),
                        "adapter_endpoints": capabilities.get("adapter_endpoints"),
                    }
                )
        raise AssertionError(
            "public Core did not dispatch the confirmed action: "
            f"schedule={schedule}, job={job}, route={route}, nodes={node_summary}"
        )

    @staticmethod
    def _wait_schedule(relay_url: str, token: str, schedule_id: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = _request(relay_url, f"/v1/cluster/scheduled-actions/{schedule_id}", token=token)
            if isinstance(value, dict) and value.get("status") == "completed":
                return
            if isinstance(value, dict) and value.get("status") in {"failed", "unknown", "cancelled", "expired"}:
                raise AssertionError(f"scheduled action ended as {value.get('status')}")
            time.sleep(0.1)
        raise AssertionError("public Core did not reconcile the completed action")


if __name__ == "__main__":
    unittest.main()
