from __future__ import annotations

import sys
import threading
from contextlib import AbstractContextManager
from typing import Any

from .contracts import ContractError, RuntimeOptions, parse_action_job, parse_profile_options
from .core_client import ContextBridgeV2Client, CoreProtocolError, WorkLease
from .github import AmbiguousProviderError, DefinitiveProviderError, GitHubActions, PreflightProviderError
from .relay_client import RelayClient, RelayProtocolError
from .store import ActionStore, ActionStoreError, AmbiguousActionError


class MutationOutcomeUnknown(RuntimeError):
    """The adapter intentionally stopped because a mutation may have occurred."""


class _PresenceKeeper(AbstractContextManager["_PresenceKeeper"]):
    def __init__(self, relay: RelayClient, adapter_id: str, instance_id: str, adapter_uid: str) -> None:
        self.relay = relay
        self.adapter_id = adapter_id
        self.instance_id = instance_id
        self.adapter_uid = adapter_uid
        self.stop_event = threading.Event()
        self.busy = threading.Event()
        self.lost = threading.Event()
        self.disabled = threading.Event()
        self.thread = threading.Thread(target=self._run, name="cb-actions-presence", daemon=True)

    def __enter__(self) -> _PresenceKeeper:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        wait = 20
        while not self.stop_event.wait(wait):
            try:
                lease = self.relay.heartbeat(
                    adapter_id=self.adapter_id,
                    instance_id=self.instance_id,
                    active=1 if self.busy.is_set() else 0,
                )
                if lease.adapter_uid != self.adapter_uid:
                    self.lost.set()
                    return
                if lease.enabled:
                    self.disabled.clear()
                else:
                    self.disabled.set()
                wait = lease.heartbeat_after_seconds
            except RelayProtocolError:
                self.lost.set()
                return


class ActionsAdapterService:
    def __init__(self, client: ContextBridgeV2Client) -> None:
        self.client = client

    def run(
        self,
        relay: RelayClient,
        *,
        adapter_id: str,
        instance_id: str,
        once: bool = False,
    ) -> int:
        self.client.status()
        profiles = self.client.profiles()
        options = parse_profile_options(profiles[self.client.profile])
        store = ActionStore(options.database_path)
        provider = GitHubActions(options.github_token_file)
        identity = relay.whoami()
        if identity.scheduled_actions:
            raise RelayProtocolError("presence credential must not grant scheduled-action authority")
        presence = relay.heartbeat(adapter_id=adapter_id, instance_id=instance_id)
        if presence.adapter_uid != options.adapter_uid:
            raise RelayProtocolError("profile adapter_uid does not match the authenticated relay presence")
        endpoint_capability = self.client.heartbeat(actions=options.allowed_actions)
        completed = 0
        with _PresenceKeeper(relay, adapter_id, instance_id, options.adapter_uid) as keeper:
            if not presence.enabled:
                keeper.disabled.set()
            while True:
                if keeper.lost.is_set():
                    raise RelayProtocolError("adapter presence lease was lost")
                if keeper.disabled.is_set():
                    if once:
                        return completed
                    if keeper.stop_event.wait(1):
                        return completed
                    continue
                work = self.client.next(endpoint_capability, wait=not once)
                if work is None:
                    if once:
                        return completed
                    endpoint_capability = self.client.heartbeat(endpoint_capability, actions=options.allowed_actions)
                    continue
                keeper.busy.set()
                endpoint_capability = self.client.heartbeat(
                    endpoint_capability, busy=True, actions=options.allowed_actions
                )
                try:
                    self.process(
                        work,
                        options,
                        store,
                        provider,
                        relay=relay,
                        adapter_id=adapter_id,
                        instance_id=instance_id,
                    )
                finally:
                    keeper.busy.clear()
                completed += 1
                endpoint_capability = self.client.heartbeat(endpoint_capability, actions=options.allowed_actions)
                if once:
                    return completed

    def process(
        self,
        work: WorkLease,
        options: RuntimeOptions,
        store: ActionStore,
        provider: GitHubActions | Any,
        *,
        relay: RelayClient | Any | None = None,
        adapter_id: str = "actions-primary",
        instance_id: str = "actions-local",
    ) -> None:
        try:
            envelope = parse_action_job(work.job, options)
            prepared = store.prepare(envelope)
        except AmbiguousActionError as exc:
            raise MutationOutcomeUnknown("stored occurrence is observation-only after an ambiguous mutation") from exc
        except (ContractError, ActionStoreError) as exc:
            self._complete_error(work, "adapter_action_invalid", exc)
            return

        if prepared.cached_receipt is not None:
            self.client.check_lease(work)
            self.client.complete(work, _receipt_output(prepared.cached_receipt))
            return
        if work.observation_only:
            raise MutationOutcomeUnknown("ContextBridge marked the occurrence observation-only without a receipt")

        self.client.check_lease(work)
        if relay is not None:
            try:
                presence = relay.heartbeat(adapter_id=adapter_id, instance_id=instance_id, active=1)
            except RelayProtocolError as exc:
                self._complete_error(work, "adapter_presence_unavailable", exc)
                return
            if presence.adapter_uid != options.adapter_uid or not presence.enabled or not presence.available:
                self._complete_error(
                    work, "adapter_presence_denied", RuntimeError("adapter is disabled or unavailable")
                )
                return
        try:
            provider.preflight(prepared.repository, envelope.action_kind, prepared.payload)
        except DefinitiveProviderError as exc:
            self._complete_error(work, "adapter_target_rejected", exc)
            return
        except PreflightProviderError as exc:
            self._complete_error(work, "adapter_target_unavailable", exc)
            return

        self.client.progress(work, 1, "authorized action references and scope verified", 25)
        self.client.check_lease(work)
        store.mark_mutating(envelope, prepared.request_digest)
        try:
            self.client.claim_mutation(work)
        except CoreProtocolError as exc:
            store.mark_unknown(envelope, prepared.request_digest, "adapter_claim_unknown")
            raise MutationOutcomeUnknown("mutation claim outcome is ambiguous") from exc

        try:
            receipt = provider.execute(prepared.repository, envelope.action_kind, prepared.payload)
        except DefinitiveProviderError as exc:
            store.mark_failed(envelope, prepared.request_digest, "adapter_provider_rejected")
            self._complete_error(work, "adapter_provider_rejected", exc)
            return
        except AmbiguousProviderError as exc:
            store.mark_unknown(envelope, prepared.request_digest, "adapter_provider_unknown")
            raise MutationOutcomeUnknown("provider mutation outcome is ambiguous") from exc
        except Exception as exc:  # noqa: BLE001 - after claim, every unexpected failure is ambiguous.
            try:
                store.mark_unknown(envelope, prepared.request_digest, "adapter_internal_unknown")
            except ActionStoreError:
                pass
            raise MutationOutcomeUnknown("unexpected failure after the mutation boundary") from exc

        try:
            store.mark_completed(envelope, prepared.request_digest, receipt)
        except ActionStoreError as exc:
            raise MutationOutcomeUnknown("provider succeeded but its durable receipt could not be committed") from exc
        self.client.progress(work, 2, "provider receipt committed", 95)
        self.client.complete(work, _receipt_output(receipt))

    def _complete_error(self, work: WorkLease, code: str, detail: BaseException) -> None:
        print(f"actions adapter job {work.job.get('id', '?')}: {code}: {detail}", file=sys.stderr)
        self.client.check_lease(work)
        self.client.complete(work, {"mode": "text", "error": code, "model": "contextbridge-actions"})


def _receipt_output(receipt: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "schema": "contextbridge.action-receipt.v1",
        "provider": str(receipt.get("provider", ""))[:40],
        "external_id": str(receipt.get("external_id", ""))[:256],
        "url": str(receipt.get("url", ""))[:2_048],
    }
    return {
        "mode": "json",
        "json": normalized,
        "provider": normalized["provider"],
        "model": "contextbridge-actions",
        "finish_reason": "stop",
        "cost_status": "unknown",
    }


def inspect_profile(profile: dict[str, Any]) -> tuple[RuntimeOptions, dict[str, Any]]:
    options = parse_profile_options(profile)
    store = ActionStore(options.database_path)
    provider = GitHubActions(options.github_token_file)
    provider.check()
    return options, {
        "database": str(store.path),
        "github_credential": "ready",
        "allowed_actions": list(options.allowed_actions),
        "adapter_uid": options.adapter_uid,
    }
