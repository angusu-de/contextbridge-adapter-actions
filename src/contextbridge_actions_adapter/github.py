from __future__ import annotations

import json
import ssl
import stat
from pathlib import Path
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, OpenerDirector, Request, build_opener

from .strictjson import StrictJSONError, loads

GITHUB_API_ORIGIN = "https://api.github.com"
MAX_RESPONSE_BYTES = 1024 * 1024


class DefinitiveProviderError(RuntimeError):
    """The provider definitively rejected the mutation."""


class AmbiguousProviderError(RuntimeError):
    """The provider may have applied the mutation."""


class PreflightProviderError(RuntimeError):
    """A read-only target check failed before any mutation began."""


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


def _read_secret(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise DefinitiveProviderError("GitHub credential file is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 8_192:
        raise DefinitiveProviderError("GitHub credential must be a small regular non-symlink file")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise DefinitiveProviderError("GitHub credential file is unreadable") from exc
    if not 20 <= len(token) <= 4_096 or any(character in token for character in "\r\n\x00"):
        raise DefinitiveProviderError("GitHub credential is malformed")
    return token


def _read_bounded(response: Any) -> bytes:
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                raise AmbiguousProviderError("GitHub response exceeds the byte limit")
        except ValueError as exc:
            raise AmbiguousProviderError("GitHub returned an invalid Content-Length") from exc
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, MAX_RESPONSE_BYTES + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise AmbiguousProviderError("GitHub response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


class GitHubActions:
    def __init__(self, token_file: Path, *, opener: OpenerDirector | Any | None = None) -> None:
        self.token_file = token_file
        self._opener = opener or build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def check(self) -> None:
        _read_secret(self.token_file)

    def preflight(self, repository: str, action_kind: str, payload: dict[str, Any]) -> None:
        if action_kind == "github.issue.create":
            return
        if action_kind not in {"github.issue.comment", "github.issue.update"}:
            raise DefinitiveProviderError("GitHub action kind is unsupported")
        owner, name = repository.split("/", 1)
        base = f"{GITHUB_API_ORIGIN}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        number = int(payload["issue_number"])
        url = base + f"/issues/{number}"
        request = Request(  # noqa: S310  # nosec B310
            url,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {_read_secret(self.token_file)}",
                "User-Agent": "contextbridge-adapter-actions/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                status = int(response.status)
                raw = _read_bounded(response)
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise DefinitiveProviderError(f"GitHub rejected the issue target (HTTP {exc.code})") from exc
            raise PreflightProviderError(f"GitHub target check returned HTTP {exc.code}") from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise PreflightProviderError("GitHub target check could not complete") from exc
        if not 200 <= status < 300:
            raise PreflightProviderError(f"GitHub target check returned HTTP {status}")
        try:
            value = loads(raw, max_bytes=MAX_RESPONSE_BYTES)
        except StrictJSONError as exc:
            raise PreflightProviderError("GitHub target check returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("number") != number or value.get("repository_url") != base:
            raise PreflightProviderError("GitHub target check did not match the requested issue")
        if "pull_request" in value:
            raise DefinitiveProviderError("target is a pull request; this action permits issues only")

    def execute(self, repository: str, action_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        owner, name = repository.split("/", 1)
        base = f"{GITHUB_API_ORIGIN}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        if action_kind == "github.issue.create":
            method, url, body = "POST", base + "/issues", {"title": payload["title"], "body": payload["body"]}
        elif action_kind == "github.issue.comment":
            number = int(payload["issue_number"])
            method, url, body = "POST", base + f"/issues/{number}/comments", {"body": payload["body"]}
        elif action_kind == "github.issue.update":
            number = int(payload["issue_number"])
            method, url = "PATCH", base + f"/issues/{number}"
            body = {key: payload[key] for key in ("title", "body", "state") if key in payload}
        else:
            raise DefinitiveProviderError("GitHub action kind is unsupported")
        return self._request(method, url, body)

    def _request(self, method: str, url: str, body: dict[str, Any]) -> dict[str, Any]:
        if not url.startswith(GITHUB_API_ORIGIN + "/"):
            raise DefinitiveProviderError("GitHub request escaped the fixed API origin")
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(  # noqa: S310  # nosec B310
            url,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {_read_secret(self.token_file)}",
                "Content-Type": "application/json",
                "User-Agent": "contextbridge-adapter-actions/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener.open(request, timeout=30) as response:
                status = int(response.status)
                raw = _read_bounded(response)
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise DefinitiveProviderError(f"GitHub rejected the action (HTTP {exc.code})") from exc
            raise AmbiguousProviderError(f"GitHub returned an ambiguous HTTP {exc.code}") from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise AmbiguousProviderError("GitHub transport failed after mutation began") from exc
        if not 200 <= status < 300:
            raise AmbiguousProviderError(f"GitHub returned an ambiguous HTTP {status}")
        try:
            value = loads(raw, max_bytes=MAX_RESPONSE_BYTES)
        except StrictJSONError as exc:
            raise AmbiguousProviderError("GitHub returned an invalid success document") from exc
        if not isinstance(value, dict):
            raise AmbiguousProviderError("GitHub success document is not an object")
        external_id = value.get("id")
        html_url = value.get("html_url")
        if isinstance(external_id, bool) or not isinstance(external_id, int) or external_id < 1:
            raise AmbiguousProviderError("GitHub success document omitted its numeric ID")
        if not isinstance(html_url, str) or len(html_url) > 2_048:
            raise AmbiguousProviderError("GitHub success document omitted its URL")
        parsed = urlsplit(html_url)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
            raise AmbiguousProviderError("GitHub success URL is outside github.com")
        return {"provider": "github", "external_id": str(external_id), "url": html_url}
