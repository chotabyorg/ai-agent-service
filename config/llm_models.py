"""Available LLM models served via OpenRouter."""

# Первая модель в списке считается моделью по умолчанию для новых агентов.
AVAILABLE_MODELS = [
    {"display_name": "GPT-6 Luna", "model_id": "openai/gpt-6-luna"},
    {"display_name": "Kimi K3", "model_id": "moonshotai/kimi-k3"},
    {"display_name": "Qwen3.8 27B", "model_id": "qwen/qwen3.8-27b"},
    {"display_name": "Muse Glimmer 30B", "model_id": "meta/muse-glimmer-30b"},
]


def get_default_model_id() -> str:
    """Keep the runtime fallback aligned with model discovery's first entry."""
    return AVAILABLE_MODELS[0]["model_id"]
