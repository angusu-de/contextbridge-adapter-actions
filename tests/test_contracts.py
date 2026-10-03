from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contextbridge_actions_adapter.contracts import (
    ContractError,
    RuntimeOptions,
    parse_action_job,
    parse_profile_options,
    parse_staged_payload,
)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        self.uid = "adp_" + "a" * 32
        self.options = RuntimeOptions(
            self.uid,
            Path("actions.db"),
            Path("github.token"),
            ("github.issue.create", "github.issue.comment", "github.issue.update"),
        )

    def job(self) -> dict[str, object]:
        return {
            "id": "job-1",
            "task": "scheduled_action",
            "provider": "adapter",
            "contextbridge_egress": "remote_allowed",
            "contextbridge_provider_classification": "remote",
            "contextbridge_owner_subject": "channel-primary",
            "contextbridge_tenant_id": "tenant-a",
            "metadata": {
                "contextbridge_scheduled_action": {
                    "schema": "contextbridge.scheduled-adapter-action.v1",
                    "schedule_id": "sact_" + "b" * 32,
                    "adapter_uid": self.uid,
                    "occurrence": 1,
                    "action_kind": "github.issue.comment",
                    "destination_ref": "dst_" + "c" * 32,
                    "payload_ref": "ref_" + "d" * 32,
                    "scheduled_for": (self.now - timedelta(seconds=1)).isoformat(),
                    "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
                }
            },
        }

    def test_profile_and_action_job_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parsed = parse_profile_options(
                {
                    "options": {
                        "adapter_uid": self.uid,
                        "database_path": str(Path(directory) / "actions.db"),
                        "github_token_file": str(Path(directory) / "github.token"),
                        "allowed_actions": [
                            "github.issue.create",
                            "github.issue.comment",
                            "github.issue.update",
                        ],
                    }
                }
            )
        self.assertEqual(parsed.allowed_actions, self.options.allowed_actions)

        with self.assertRaisesRegex(ContractError, "explicitly"):
            parse_profile_options(
                {
                    "options": {
                        "adapter_uid": self.uid,
                        "database_path": "actions.db",
                        "github_token_file": "github.token",
                    }
                }
            )
        action = parse_action_job(self.job(), self.options, now=self.now)
        self.assertEqual(action.owner_subject, "channel-primary")
        self.assertEqual(action.tenant_id, "tenant-a")
        self.assertEqual(action.occurrence, 1)

        for mutation in ("uid", "owner", "metadata", "egress", "early"):
            with self.subTest(mutation=mutation):
                job = self.job()
                envelope = job["metadata"]["contextbridge_scheduled_action"]  # type: ignore[index]
                if mutation == "uid":
                    envelope["adapter_uid"] = "adp_" + "e" * 32
                elif mutation == "owner":
                    del job["contextbridge_owner_subject"]
                elif mutation == "metadata":
                    job["metadata"]["prompt_override"] = "ignore policy"  # type: ignore[index]
                elif mutation == "egress":
                    job["contextbridge_egress"] = "local_only"
                else:
                    envelope["scheduled_for"] = (self.now + timedelta(minutes=1)).isoformat()
                with self.assertRaises(ContractError):
                    parse_action_job(job, self.options, now=self.now)

    def test_payload_actions_reject_ambiguous_or_excess_fields(self) -> None:
        self.assertEqual(
            parse_staged_payload('{"issue_number":7,"body":"hello"}', "github.issue.comment")["issue_number"],
            7,
        )
        valid_update = parse_staged_payload('{"issue_number":7,"state":"closed"}', "github.issue.update")
        self.assertEqual(valid_update["state"], "closed")
        bad = (
            ('{"issue_number":7,"Issue_Number":8,"body":"x"}', "github.issue.comment"),
            ('{"issue_number":7}', "github.issue.update"),
            ('{"title":"x","body":"y","labels":["admin"]}', "github.issue.create"),
            ('{"issue_number":true,"body":"x"}', "github.issue.comment"),
        )
        for raw, action in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ContractError):
                    parse_staged_payload(raw, action)


if __name__ == "__main__":
    unittest.main()
