from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from contextbridge_actions_adapter.core_client import ContextBridgeV2Client


class _RecordingClient(ContextBridgeV2Client):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:32145", "a" * 32, "github-actions")
        self.body: Any = None

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        allow_no_content: bool = False,
    ) -> tuple[int, Any]:
        del method, headers, allow_no_content
        self.body = body
        return 200, {
            "endpoints": [
                {
                    "profile": self.profile,
                    "endpoint_id": self.endpoint_id,
                    "endpoint_capability": "c" * 43,
                }
            ]
        }


class CoreClientTests(unittest.TestCase):
    def test_available_endpoint_reports_waiting_not_display_only_idle(self) -> None:
        client = _RecordingClient()
        client.heartbeat(actions=("github.issue.comment",))

        self.assertEqual(client.body["state"], "waiting")
        self.assertEqual(client.body["endpoints"][0]["state"], "waiting")

    def test_busy_endpoint_is_never_advertised_as_schedulable(self) -> None:
        client = _RecordingClient()
        client.heartbeat(busy=True, actions=("github.issue.comment",))

        self.assertEqual(client.body["state"], "busy")
        self.assertEqual(client.body["endpoints"][0]["state"], "busy")


if __name__ == "__main__":
    unittest.main()
