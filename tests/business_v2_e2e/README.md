# Business V2 actual three-service acceptance

Run from the candidate AI repository using a Python environment containing Docker
SDK, asyncpg, HTTPX, SQLAlchemy and cryptography:

```sh
python -m tests.business_v2_e2e.run
```

The candidate checkouts must be siblings named `ai`, `conversation`, and `crm`.
The image names below identify dependency environments from the local foundation
verification; build equivalent service images with the exact checked-in dependency
pins if those local tags are unavailable. `BUSINESS_E2E_AI_IMAGE`,
`BUSINESS_E2E_CONVERSATION_IMAGE`, and `BUSINESS_E2E_CRM_IMAGE` can select local
dependency images; `BUSINESS_E2E_CONVERSATION_ROOT` can select an isolated source
export. These settings change only the disposable harness. Do not substitute host
dependency versions for service environments. Every run records
application source SHA256 and installed HTTP/ORM dependency versions in
`runtime-manifests.jsonl` alongside its result, so cached image tags do not stand
in for evidence of the source actually executed.

The runner creates only new, disposable local containers. It never reads `.env`,
uses inherited application DSNs, or contacts production. The actual unchanged
Conversation source, current AI source, and current CRM source are mounted
read-only into separate application containers. Service-specific existing images
supply their exact dependencies:

- AI/model: `business-v2-current-ai-runtime:local`
- Conversation: `conversation-service:goal4a-author`
- CRM: `business-v2-current-crm-runtime:latest`

The AI and Conversation images use their pinned `/opt/venv`; CRM uses its image's
Python 3.12 environment. The host controller does not replace service dependencies.
AI and Conversation have separate migrated PostgreSQL16 databases. CRM has its
own migrated PostgreSQL18 database. All containers share one disposable network
namespace; service egress is restricted before imports to explicit numerical
loopback ports. Only loopback host ports are published. Private test configuration,
keys, model inputs and logs are retained in a newly created mode0700 temporary
directory; individual generated records are mode0600. Containers and tmpfs database
contents are removed in `finally` using only handles created by this run.

The only functional fakes are a scripted OpenAI-compatible model and synthetic
user-service `/api/v1/me` identity. The actual direct OpenAI client, typed business
registry, adapter, RS256 assertions, CRM auth/grants/commands/projection/receipts,
HTTP transport and PostgreSQL transactions execute. Owner connection and capability
toggles use actual AI owner APIs and real CRM grant configuration. The synthetic
Agent is inserted as a trusted fixture; all customer/business records use CRM API.

Each scenario enters the actual Conversation inbound API, creates and claims a
real stable Turn, then executes through the real managed HTTP client and AI route.
Drafts are never promoted into canonical delivered Messages. Later customer Turns
receive AI's retained pending-proposal projection. No legacy Session/history API
may be called; the bootstrap records and rejects any such invocation.

Coverage includes:

- Catalog factual lookup; customer lookup and useful booking answer.
- New customer + inquiry + booking atomic aggregate, with no effect before consent.
- Existing active customer reuse requires a separate explicit reference call;
  case-only name matches work, mismatches receive a safe clarification, and
  corrected details complete one booking without creating another customer.
- Natural model-decision protocol for `+`, `ага`, `ок`, `👍`, `давайте`.
- Changed details require a replacement proposal and later consent; reject and
  unrelated replies produce no effect or borrowed operation refs.
- Ambiguous identity returns no candidates; staff-note/deal sections are denied
  while the customer's booking remains useful.
- Every canonical capability, including safe writes and confirmed reschedule,
  cancellation, enabled deal creation and movement.
- Disabled capability and default-off deal writes denied server-side.
- Supported booking + order grouping executes both effects after one confirmation;
  unsupported mixed mutation bundles are denied before reservation/effect.
- Actual CRM commit followed by discarded successful HTTP response: AI/Conversation
  retain UNKNOWN, later ordinary Turn cannot trigger a blind retry, and explicit
  trusted original-operation receipt lookup proves one effect. Conversation's
  already-terminal result remains immutable; only AI retained evidence is enriched.
- Real owner catalog edit after AI dispatch admission but before CRM acceptance
  tests authoritative no-effect rejection, with no customer/booking mutation.

The two fault injectors are transport/bootstrap-only. Response loss discards the
actual CRM application's successful response after its transaction returned. The
stale-facts injector performs a real CRM owner PATCH immediately before passing a
managed execute body to the actual provider. It does not synthesize receipts or
provider effects.

This is deterministic protocol/full-stack evidence, not a live-model semantic
quality claim. A separately authorized real model smoke remains optional. The
runner prints individual PASS assertions and only prints
`REAL_CROSS_SERVICE_E2E_PASS` after the whole matrix passes. `summary.json` in the
private evidence directory is the structured result. Do not publish raw model
request logs or private config, even though all this harness's inputs are synthetic.

## Native execution without a container daemon

`python -m tests.business_v2_e2e.native` reuses the same `Harness.cases` and real
service bootstrap. Supply absolute `--cs-root`, `--crm-root`, `--ai-python`,
`--cs-python`, `--crm-python`, `--pg18-bin`, `--pg16-admin-input` and a new `--output`
directory. AI/Conversation use their pinned Python 3.11 environments and an
explicitly disposable native PostgreSQL16; CRM uses Python3.12 and its own freshly
initialized PostgreSQL18 cluster. Install each repository's requirements into its
own environment before running. All three source checkouts must be clean.
On Windows, invoke `python -X utf8 -m tests.business_v2_e2e.native` and supply
`--tzdata-dir` pointing to an installed IANA zoneinfo data
directory; this replaces Linux's system timezone database, not service packages.
The fixture records the timezone file hash and does not download data at runtime.

The private PG16 JSON input has `disposable: "yes"`, `host: "127.0.0.1"`, `port`,
`user` and `password` fields. It must refer only to a task-owned test cluster, never
a production/shared application DSN. Only newly generated `native_e2e_*` roles and
databases are created/dropped. The PG18 cluster and all child processes belong to
this run. Child environments contain an OS allowlist plus generated synthetic
fixture settings; network guards precede application imports. The model remains
a loopback script. No runtime credentials or `.env` are loaded.

`summary.json` records exact source revisions/trees, unchanged-source checks,
scenario outcomes, cleanup and runtime-manifest hash. Other files are private
synthetic fixture evidence, not public artifacts. This is a native full-stack
protocol check; live Widget/Telegram/model/phone acceptance remains separate.
