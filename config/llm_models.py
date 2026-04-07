"""Available LLM models served via OpenRouter."""

# Первая модель в списке считается моделью по умолчанию для новых агентов.
AVAILABLE_MODELS = [
    {"display_name": "Mimo V2 Flash", "model_id": "xiaomi/mimo-v2-flash"},
    {"display_name": "Mimo V2 Pro", "model_id": "xiaomi/mimo-v2-pro"},
    {"display_name": "Gemini 3 Flash Preview", "model_id": "google/gemini-3-flash-preview"},
    {"display_name": "Gemini 3.1 Flash Lite Preview", "model_id": "google/gemini-3.1-flash-lite-preview"},
    {"display_name": "Kimi K2.5", "model_id": "moonshotai/kimi-k2.5"},
    {"display_name": "Qwen 3.5 35B A3B", "model_id": "qwen/qwen3.5-35b-a3b"},
    {"display_name": "Qwen 3.5 397B A17B", "model_id": "qwen/qwen3.5-397b-a17b"},
    {"display_name": "Qwen 3.6 Plus (free)", "model_id": "qwen/qwen3.6-plus:free"},
    {"display_name": "GLM 5.1", "model_id": "z-ai/glm-5.1"},
]