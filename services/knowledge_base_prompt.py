"""Prompt formatting helpers for Knowledge Base retrieval contexts."""

from collections.abc import Sequence

from services.knowledge_base_client import KnowledgeBaseContext


KB_CONTEXT_HEADER = (
    "<knowledge_base_context>\n"
    "Use these snippets as company knowledge. If snippets are irrelevant or insufficient, do not invent facts.\n"
)
KB_CONTEXT_FOOTER = "</knowledge_base_context>"


def build_knowledge_base_system_message(
    contexts: Sequence[KnowledgeBaseContext],
    *,
    max_chars_per_chunk: int,
    max_total_context_chars: int,
) -> str | None:
    """Return a bounded system message for KB snippets, or ``None`` if empty."""

    if not contexts or max_chars_per_chunk <= 0 or max_total_context_chars <= 0:
        return None

    blocks: list[str] = []
    for index, context in enumerate(contexts, start=1):
        content = context.content.strip()
        if not content:
            continue
        trimmed_content = content[:max_chars_per_chunk].strip()
        source = context.source_filename.replace("\\", "\\\\").replace('"', '\\"')
        score = "" if context.score is None else f" score={context.score:.2f}"
        blocks.append(
            f'[{index}] source="{source}" chunk_index={context.chunk_index}{score}\n'
            f"{trimmed_content}"
        )

    if not blocks:
        return None

    message = f"{KB_CONTEXT_HEADER}\n" + "\n\n".join(blocks) + f"\n{KB_CONTEXT_FOOTER}"
    if len(message) <= max_total_context_chars:
        return message

    minimum_size = len(KB_CONTEXT_HEADER) + len(KB_CONTEXT_FOOTER)
    if max_total_context_chars <= minimum_size:
        return None

    available = max_total_context_chars - len(KB_CONTEXT_HEADER) - len(KB_CONTEXT_FOOTER) - 1
    body = ("\n\n".join(blocks))[:available].rstrip()
    if not body:
        return None
    return f"{KB_CONTEXT_HEADER}{body}\n{KB_CONTEXT_FOOTER}"
