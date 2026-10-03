# Changelog

## Unreleased

- Report available v2 action endpoints as schedulable `waiting` endpoints
  instead of the display-only `idle` state.
- Add a real pinned public-Core boundary proof covering scoped confirmation,
  worker placement, one mutation claim, simulated provider execution, receipt,
  and scheduled-action reconciliation without live provider traffic.

## 0.1.0 - 2026-10-03

- Add a strict ContextBridge adapter-v2 worker for confirmed external actions.
- Add owner- and tenant-bound opaque destination and payload references.
- Add GitHub issue creation, commenting, and bounded issue updates.
- Add durable no-replay fencing for ambiguous provider outcomes.
- Add relay presence, adapter UID, lease, and mutation-claim verification.
- Require an independent least-privilege presence credential at execution.
- Reject pull-request targets before issue-only comment or update actions.
