# Prompt Runtime V2

`PromptComposer` composes every actual LLM request, including raw legacy
`system_prompt` agents. Ordered layers are platform policy, owner business prompt,
runtime capability context, request-time channel/time context, and optional
knowledge context. The shared LLM core keeps knowledge in a separate system
message after the first four layers, preserving managed context budgeting.

Platform policy requires factual results, natural progressive data collection,
provider-neutral user language, and READ / WRITE_SAFE / WRITE_CONFIRM semantics.
It never grants tool authorization. Only actual runtime tools are available;
the server owns identity, capability permission, frozen proposal validation,
authority, durable operations and uncertain-write recovery.

The managed runtime can supply a closed safe projection:
`{pending: null | {proposal_id, summary, confirmable}}`. These are internal model
fields, not user confirmation grammar. When the runtime supplies
`business_decide_proposal`, the model interprets the full customer reply as
CONFIRM, REJECT, CHANGE or UNRELATED. No list of yes words executes a mutation.
The proposal control and ledger still decide whether execution is authorized.
Provider receipts, credentials and workspace/actor context are absent from this
projection. Neither this composition nor its preview changes the managed v1 API.

`PromptBuilder.build_static` renders only owner business instructions. Cached
prompts do not freeze time, channel or permissions. Structured config contains
`responsibilities` for human business duties; old `capabilities` remains an input
alias. Explicit `responsibilities`, including an empty list, wins. Reading old
stored configurations does not rewrite them. The API validates either field and preserves the submitted configuration; new
clients write the canonical field on explicit save. Existing owner-authored algorithms, greetings,
personality, fallback and restrictions continue to be rendered; platform-authored
guidance contains no provider-specific tool names or blind retry instruction.

The synthetic beauty-salon fixture and composition/API/parity tests verify these
contracts. They do not establish real-model semantic accuracy or real ChotaCRM
execution; those require the separate business E2E and optional live-model smoke.
