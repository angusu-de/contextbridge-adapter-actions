from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from contextbridge_actions_adapter.contracts import RuntimeOptions
from contextbridge_actions_adapter.core_client import CoreProtocolError, WorkLease
from contextbridge_actions_adapter.github import (
    AmbiguousProviderError,
    DefinitiveProviderError,
    PreflightProviderError,
)
from contextbridge_actions_adapter.relay_client import PresenceLease
from contextbridge_actions_adapter.service import ActionsAdapterService, MutationOutcomeUnknown
from contextbridge_actions_adapter.store import ActionStore


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.outputs: list[dict[str, Any]] = []
        self.fail_check = False
        self.fail_claim = False

    def progress(self, _work: WorkLease, sequence: int, _text: str, _percent: int) -> None:
        self.calls.append(f"progress:{sequence}")

    def check_lease(self, _work: WorkLease) -> None:
        self.calls.append("check")
        if self.fail_check:
            raise CoreProtocolError("lost")

    def claim_mutation(self, _work: WorkLease) -> None:
        self.calls.append("claim")
        if self.fail_claim:
            raise CoreProtocolError("unknown claim")

    def complete(self, _work: WorkLease, output: dict[str, Any]) -> None:
        self.calls.append("complete")
        self.outputs.append(output)


class FakeProvider:
    def __init__(self, callback: Any = None, error: BaseException | None = None) -> None:
        self.calls = 0
        self.callback = callback
        self.error = error
        self.preflight_error: BaseException | None = None

    def preflight(self, _repository: str, _action_kind: str, _payload: dict[str, Any]) -> None:
        if self.preflight_error:
            raise self.preflight_error

    def execute(self, repository: str, action_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.callback:
            self.callback(repository, action_kind, payload)
        if self.error:
            raise self.error
        return {"provider": "github", "external_id": "42", "url": "https://github.com/o/r/issues/42"}


class FakeRelay:
    def __init__(self, uid: str, *, enabled: bool = True) -> None:
        self.uid = uid
        self.enabled = enabled
        self.calls = 0

    def heartbeat(self, **_kwargs: Any) -> PresenceLease:
        self.calls += 1
        return PresenceLease(self.uid, self.enabled, self.enabled, 20)


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.uid = "adp_" + "a" * 32
        self.options = RuntimeOptions(
            self.uid,
            Path(self.temp.name) / "actions.db",
            Path(self.temp.name) / "github.token",
            ("github.issue.comment",),
        )
        self.store = ActionStore(self.options.database_path)
        self.owner = "channel-primary"
        self.tenant = "tenant-a"
        self.destination = self.store.add_github_destination(self.owner, self.tenant, "IamAngusU/ContextBridge")
        self.payload = self.store.stage_payload(
            self.owner,
            self.tenant,
            self.destination,
            "github.issue.comment",
            b'{"issue_number":126,"body":"bounded"}',
            ttl=timedelta(hours=1),
        )
        self.schedule = "sact_" + "b" * 32

    def work(self, *, observation_only: bool = False) -> WorkLease:
        now = datetime.now(timezone.utc)
        return WorkLease(
            {
                "id": "job-1",
                "task": "scheduled_action",
                "provider": "adapter",
                "contextbridge_egress": "remote_allowed",
                "contextbridge_provider_classification": "remote",
                "contextbridge_owner_subject": self.owner,
                "contextbridge_tenant_id": self.tenant,
                "metadata": {
                    "contextbridge_scheduled_action": {
                        "schema": "contextbridge.scheduled-adapter-action.v1",
                        "schedule_id": self.schedule,
                        "adapter_uid": self.uid,
                        "occurrence": 1,
                        "action_kind": "github.issue.comment",
                        "destination_ref": self.destination,
                        "payload_ref": self.payload,
                        "scheduled_for": (now - timedelta(seconds=1)).isoformat(),
                        "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    }
                },
            },
            1,
            "c" * 43,
            observation_only,
        )

    def test_mutation_is_fenced_before_provider_and_receipt_is_cached(self) -> None:
        client = FakeClient()

        def assert_fenced(_repository: str, _action: str, _payload: dict[str, Any]) -> None:
            self.assertEqual(self.store.attempt_state(self.schedule, 1), "mutating")

        provider = FakeProvider(assert_fenced)
        service = ActionsAdapterService(client)  # type: ignore[arg-type]
        service.process(self.work(), self.options, self.store, provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(self.store.attempt_state(self.schedule, 1), "completed")
        self.assertEqual(client.calls, ["check", "progress:1", "check", "claim", "progress:2", "complete"])

        service.process(self.work(observation_only=True), self.options, self.store, provider)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(client.calls[-2:], ["check", "complete"])

    def test_ambiguous_provider_is_never_replayed(self) -> None:
        client = FakeClient()
        provider = FakeProvider(error=AmbiguousProviderError("timeout"))
        service = ActionsAdapterService(client)  # type: ignore[arg-type]
        with self.assertRaises(MutationOutcomeUnknown):
            service.process(self.work(), self.options, self.store, provider)
        self.assertEqual(self.store.attempt_state(self.schedule, 1), "unknown")
        with self.assertRaises(MutationOutcomeUnknown):
            service.process(self.work(observation_only=True), self.options, self.store, provider)
        self.assertEqual(provider.calls, 1)
        self.assertNotIn("complete", client.calls)

    def test_claim_failure_is_unknown_and_does_not_call_provider(self) -> None:
        client = FakeClient()
        client.fail_claim = True
        provider = FakeProvider()
        service = ActionsAdapterService(client)  # type: ignore[arg-type]
        with self.assertRaises(MutationOutcomeUnknown):
            service.process(self.work(), self.options, self.store, provider)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.store.attempt_state(self.schedule, 1), "unknown")

    def test_definitive_rejection_completes_a_bounded_failure(self) -> None:
        client = FakeClient()
        provider = FakeProvider(error=DefinitiveProviderError("unprocessable"))
        service = ActionsAdapterService(client)  # type: ignore[arg-type]
        service.process(self.work(), self.options, self.store, provider)
        self.assertEqual(self.store.attempt_state(self.schedule, 1), "failed")
        self.assertEqual(client.outputs[-1]["error"], "adapter_provider_rejected")

    def test_disabled_presence_stops_before_mutation_claim(self) -> None:
        client = FakeClient()
        provider = FakeProvider()
        relay = FakeRelay(self.uid, enabled=False)
        service = ActionsAdapterService(client)  # type: ignore[arg-type]
        service.process(self.work(), self.options, self.store, provider, relay=relay)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.store.attempt_state(self.schedule, 1), "prepared")
        self.assertNotIn("claim", client.calls)
        self.assertEqual(client.outputs[-1]["error"], "adapter_presence_denied")

    def test_failed_read_only_preflight_never_reaches_mutation_boundary(self) -> None:
        client = FakeClient()
        provider = FakeProvider()
        provider.preflight_error = PreflightProviderError("temporary read failure")
        service = ActionsAdapterService(client)  # type: ignore[arg-type]
        service.process(self.work(), self.options, self.store, provider)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(self.store.attempt_state(self.schedule, 1), "prepared")
        self.assertNotIn("claim", client.calls)
        self.assertEqual(client.outputs[-1]["error"], "adapter_target_unavailable")


if __name__ == "__main__":
    unittest.main()
