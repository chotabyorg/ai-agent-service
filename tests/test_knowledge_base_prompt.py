from uuid import uuid4

from services.knowledge_base_client import KnowledgeBaseContext
from services.knowledge_base_prompt import build_knowledge_base_system_message


def make_context(
    *,
    content: str = "Warranty is 12 months.",
    score: float | None = 0.88,
    source_filename: str = "faq.pdf",
    chunk_index: int = 2,
) -> KnowledgeBaseContext:
    return KnowledgeBaseContext(
        chunk_id=uuid4(),
        document_id=uuid4(),
        knowledge_base_id=uuid4(),
        content=content,
        score=score,
        source_filename=source_filename,
        chunk_index=chunk_index,
        metadata={"raw_secret": "must-not-leak", "content_hash": "abc"},
    )


def test_prompt_formats_source_line_without_raw_metadata() -> None:
    message = build_knowledge_base_system_message(
        [make_context()],
        max_chars_per_chunk=1000,
        max_total_context_chars=3000,
    )

    assert message is not None
    assert message.startswith("<knowledge_base_context>")
    assert '[1] source="faq.pdf" chunk_index=2 score=0.88' in message
    assert "Warranty is 12 months." in message
    assert "raw_secret" not in message
    assert "must-not-leak" not in message
    assert "content_hash" not in message


def test_prompt_trims_each_chunk() -> None:
    message = build_knowledge_base_system_message(
        [make_context(content="x" * 20)],
        max_chars_per_chunk=10,
        max_total_context_chars=5000,
    )

    assert message is not None
    assert "x" * 10 in message
    assert "x" * 11 not in message


def test_prompt_trims_total_context() -> None:
    message = build_knowledge_base_system_message(
        [
            make_context(content="a" * 80, source_filename="first.pdf", chunk_index=0),
            make_context(content="b" * 80, source_filename="second.pdf", chunk_index=1),
        ],
        max_chars_per_chunk=80,
        max_total_context_chars=180,
    )

    assert message is not None
    assert len(message) <= 180
    assert message.endswith("</knowledge_base_context>")


def test_prompt_returns_none_for_empty_or_blank_context() -> None:
    assert build_knowledge_base_system_message([], max_chars_per_chunk=100, max_total_context_chars=500) is None
    assert (
        build_knowledge_base_system_message(
            [make_context(content="   ")],
            max_chars_per_chunk=100,
            max_total_context_chars=500,
        )
        is None
    )
