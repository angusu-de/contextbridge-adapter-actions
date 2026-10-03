# ContextBridge Actions Adapter

An out-of-tree ContextBridge adapter for explicit external mutations. It keeps
write authority separate from read-only research and from the public core.

Status: alpha. The contracts, negative paths, package artifacts, and simulated
provider boundary are tested on the supported Python versions. No live GitHub
mutation is performed by the test suite, and installing this repository grants
no authority until an operator separately configures credentials, destinations,
relay policy, preview, and confirmation.

The first bounded provider family is GitHub:

- `github.issue.create`
- `github.issue.comment`
- `github.issue.update`

There is deliberately no arbitrary URL, generic `POST`, browser control,
shell command, or model-selected credential. Adding an action requires code,
a strict request shape, and negative tests.

This is the write tier. Read-only web, RSS, GitHub, YouTube, and local research
remain in `contextbridge-adapter-research`; neither adapter imports the other.

## Safety model

1. An operator registers a destination for one authenticated producer subject
   and optional tenant.
2. Content is staged separately and receives an opaque `ref_...` selector.
3. The caller asks the ContextBridge relay for a scheduled-action preview and
   explicitly confirms it.
4. At execution time ContextBridge supplies its signed-in owner/tenant scope,
   exact adapter UID, one-attempt occurrence, lease generation, and capability.
5. This adapter verifies every binding, persists `mutating`, claims the lease,
   and only then calls GitHub.
6. Ambiguous provider outcomes are never retried automatically.

Execution uses a separate presence credential with the same producer subject
but **without** scheduled-action authority. The channel that stages, previews,
and confirms an action keeps the policy-bearing producer credential. A
compromised executor can therefore renew its liveness lease, but cannot mint or
confirm its own external work.

Opaque references are selectors, not bearer credentials. Matching owner,
tenant, destination, action kind, expiry, adapter UID, and occurrence are all
required.

## Configure ContextBridge

Use a public ContextBridge build containing adapter protocol v2 and scoped
scheduled actions. Registration is explicit and does not install this process:

```console
contextbridge adapter setup github-actions \
  --driver contextbridge-actions \
  --task scheduled_action \
  --route github_actions \
  --classification remote \
  --option 'adapter_uid="adp_REPLACE_WITH_PRESENCE_UID"' \
  --option 'database_path="C:/ProgramData/ContextBridgeActions/actions.db"' \
  --option 'github_token_file="C:/ProgramData/ContextBridgeActions/secrets/github.token"' \
  --token-file C:/ProgramData/ContextBridgeActions/secrets/adapter.token \
  --create-token
```

The relay producer credential also needs a scheduled-action policy binding the
same stable adapter UID, profile, principal, allowed action kinds, and
destination references. Preview and confirmation remain relay operations; this
adapter cannot bypass them.

Create a second producer credential with the same `--subject`, but omit
`--scheduled-actions-policy`; store it as `presence.token`. Never give the
executor the channel's policy-bearing producer token.

## Register and stage

The staging commands require the already authenticated subject and tenant. A
channel should obtain the subject from `GET /v1/cluster/whoami`, never from
free-form model output.

```console
contextbridge-actions-adapter destination add \
  --database ./actions.db --relay-url https://relay.example.net \
  --producer-token-file ./secrets/producer.token \
  --repository IamAngusU/ContextBridge

contextbridge-actions-adapter payload stage \
  --database ./actions.db --relay-url https://relay.example.net \
  --producer-token-file ./secrets/producer.token \
  --destination-ref dst_... --action-kind github.issue.comment \
  --file ./comment.json --ttl-hours 24
```

Payload examples:

```json
{"issue_number":126,"body":"Verified follow-up."}
```

```json
{"title":"Bounded adapter report","body":"Details."}
```

```json
{"issue_number":126,"state":"closed"}
```

Run the independently managed process:

```console
contextbridge-actions-adapter run \
  --url http://127.0.0.1:32145 \
  --profile github-actions \
  --token-file ./secrets/adapter.token \
  --relay-url https://relay.example.net \
  --presence-token-file ./secrets/presence.token
```

Absence, disablement, or failure of this process affects only its own route.
ContextBridge, local inference, pools, Alva, WhatsApp, Voice, and research do
not import or depend on this package.

## Agent use

ContextBridge can back a bounded agent: a planner may research and propose an
action, but proposal text never grants write authority. The concrete action is
staged, previewed, and confirmed through the same scoped contract. This makes
the agent powerful without turning a prompt into an administrator credential.

An agent can therefore research, draft, and request an action. It cannot grant
itself a destination, action kind, credential, tenant, or confirmation. Those
remain independent ContextBridge/operator decisions.
