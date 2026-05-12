"""Available LLM models served via OpenRouter."""

# Первая модель в списке считается моделью по умолчанию для новых агентов.
AVAILABLE_MODELS = [
    {"display_name": "DeepSeek V4 Flash", "model_id": "deepseek/deepseek-v4-flash"},
    {"display_name": "DeepSeek V4 Pro", "model_id": "deepseek/deepseek-v4-pro"},
    {"display_name": "Kimi K2.6", "model_id": "moonshotai/kimi-k2.6"},
    {"display_name": "Claude Sonnet 4.6", "model_id": "anthropic/claude-sonnet-4.6"},
    {"display_name": "Gemini 3.1 Flash Lite Preview", "model_id": "google/gemini-3.1-flash-lite-preview"},
    {"display_name": "MiMo-V2.5-Pro", "model_id": "xiaomi/mimo-v2.5-pro"},
    {"display_name": "MiMo-V2.5", "model_id": "xiaomi/mimo-v2.5"},
    {"display_name": "Mistral Medium 3.5", "model_id": "mistralai/mistral-medium-3-5"},
]