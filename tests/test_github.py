from __future__ import annotations

import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from contextbridge_actions_adapter.github import AmbiguousProviderError, DefinitiveProviderError, GitHubActions


class FakeResponse:
    def __init__(self, value: Any, status: int = 201) -> None:
        self.status = status
        self.headers = Message()
        self.raw = io.BytesIO(json.dumps(value).encode())

    def read(self, size: int = -1) -> bytes:
        return self.raw.read(size)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeOpener:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: int) -> Any:
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class GitHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token = Path(self.temp.name) / "github.token"
        self.token.write_text("github_test_token_0123456789", encoding="utf-8")

    def test_comment_uses_fixed_origin_and_exact_body(self) -> None:
        opener = FakeOpener(FakeResponse({"id": 42, "html_url": "https://github.com/o/r/issues/1#issuecomment-42"}))
        backend = GitHubActions(self.token, opener=opener)
        receipt = backend.execute("o/r", "github.issue.comment", {"issue_number": 1, "body": "hello"})
        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/o/r/issues/1/comments")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"body": "hello"})
        self.assertEqual(receipt["external_id"], "42")

    def test_client_rejection_is_definitive_but_server_failure_is_ambiguous(self) -> None:
        headers = Message()
        for code, expected in ((422, DefinitiveProviderError), (500, AmbiguousProviderError)):
            with self.subTest(code=code):
                error = HTTPError("https://api.github.com/x", code, "failed", headers, io.BytesIO(b"{}"))
                backend = GitHubActions(self.token, opener=FakeOpener(error))
                with self.assertRaises(expected):
                    backend.execute("o/r", "github.issue.create", {"title": "x", "body": "y"})

    def test_malformed_success_is_ambiguous_after_mutation(self) -> None:
        backend = GitHubActions(self.token, opener=FakeOpener(FakeResponse({"ok": True})))
        with self.assertRaises(AmbiguousProviderError):
            backend.execute("o/r", "github.issue.update", {"issue_number": 1, "state": "closed"})

    def test_preflight_accepts_an_exact_issue_and_rejects_a_pull_request(self) -> None:
        issue = {"number": 1, "repository_url": "https://api.github.com/repos/o/r"}
        opener = FakeOpener(FakeResponse(issue, status=200))
        backend = GitHubActions(self.token, opener=opener)
        backend.preflight("o/r", "github.issue.comment", {"issue_number": 1, "body": "hello"})
        self.assertEqual(opener.requests[0].method, "GET")
        self.assertEqual(opener.requests[0].full_url, "https://api.github.com/repos/o/r/issues/1")

        pull_request = {**issue, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/1"}}
        backend = GitHubActions(self.token, opener=FakeOpener(FakeResponse(pull_request, status=200)))
        with self.assertRaises(DefinitiveProviderError):
            backend.preflight("o/r", "github.issue.update", {"issue_number": 1, "state": "closed"})


if __name__ == "__main__":
    unittest.main()
