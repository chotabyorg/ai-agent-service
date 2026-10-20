"""Values retain their context in each fragment given to a speech provider."""
import pytest

from services.managed_streaming import SentenceAccumulator


@pytest.mark.parametrize("incremental", [True, False])
@pytest.mark.parametrize("value", [
    "в 13:30",
    "19.10 в 18:00",
    "19.10.2026 в 18:00",
    "22,01 BYN",
    "22.01 BYN",
    "BYN 22.01",
    "+375 29 123 42 06",
    "Цена услуги: 12.01",
])
def test_first_fragment_keeps_spoken_value_and_context_together(value, incremental):
    for width in range(24, 91):
        prefix = "Предложение " + "а" * (width - 13) + " "
        text = prefix + value + " на выбранную услугу. Подтверждаете?"
        accumulator = SentenceAccumulator()
        fragments = []
        if incremental:
            for end in range(1, len(text) + 1):
                fragments.extend(accumulator.feed(text[:end]))
        else:
            fragments.extend(accumulator.feed(text))
        fragments.append(text[len(accumulator.emitted):])
        assert "".join(fragments) == text
        assert any(value in fragment for fragment in fragments), (width, fragments)


def test_numeric_protection_preserves_early_ordinary_speech_and_revision_fence():
    text = "Могу предложить вам удобное время для посещения нашего салона завтра утром"
    accumulator = SentenceAccumulator()
    first = accumulator.feed(text)
    assert first and 24 <= len(first[0].rstrip()) <= 72
    assert text.startswith(first[0]) and len(first[0]) < len(text)
    assert accumulator.feed("Другой ответ. Продолжение") == []
    assert accumulator.feed(text + ". Продолжение") == []
