"""Closed routing hints only; never route, retry, or attribute a limiter."""
from dataclasses import dataclass
import json
from types import SimpleNamespace

import httpx
from openai import APIError, AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk


HEADER = 'X-OpenRouter-Metadata'
_MISSING = object()
_PROVIDERS = {'Makora': 'MAKORA', 'NextBit': 'NEXTBIT',
              'Cloudflare': 'CLOUDFLARE', 'OpenAI': 'OPENAI'}


@dataclass(frozen=True, slots=True)
class RouterObservation:
    router_metadata_state: str = 'MISSING'
    router_selected_hint: str = 'MISSING'
    router_attempt_bucket: str = 'MISSING'

    def fields(self):
        return dict(router_metadata_state=self.router_metadata_state,
                    router_selected_hint=self.router_selected_hint,
                    router_attempt_bucket=self.router_attempt_bucket)


def router_metadata_headers(client, headers, *, managed):
    """Request observations only from the direct, managed OpenRouter SDK client."""
    if not managed or type(client) is not AsyncOpenAI:
        return headers
    url = client.base_url
    if (type(url) is not httpx.URL or url.scheme != 'https'
            or url.host != 'openrouter.ai' or url.port not in (None, 443)
            or url.path.rstrip('/') != '/api/v1' or url.query):
        return headers
    return {**(headers or {}), HEADER: 'enabled'}


def _metadata(value):
    if type(value) is dict:
        return value.get('openrouter_metadata', _MISSING)
    if type(value) not in (SimpleNamespace, ChatCompletion, ChatCompletionChunk):
        return _MISSING
    stored = object.__getattribute__(value, '__dict__')
    if 'openrouter_metadata' in stored:
        return stored['openrouter_metadata']
    if type(value) in (ChatCompletion, ChatCompletionChunk):
        # Pydantic stores unknown SDK response fields separately from __dict__.
        extra = object.__getattribute__(value, '__pydantic_extra__')
        if type(extra) is dict:
            return extra.get('openrouter_metadata', _MISSING)
    return _MISSING


def _selected(metadata):
    endpoints = metadata.get('endpoints', _MISSING)
    if endpoints is _MISSING:
        return 'MISSING'
    if type(endpoints) is not dict:
        return 'INVALID'
    available = endpoints.get('available', _MISSING)
    if available is _MISSING:
        return 'MISSING'
    if type(available) is not list or len(available) > 64:
        return 'INVALID'
    selected = []
    for item in available:
        if type(item) is not dict or type(item.get('selected')) is not bool:
            return 'INVALID'
        if item['selected']:
            selected.append(item.get('provider'))
    if not selected:
        return 'UNSELECTED'
    if len(selected) != 1 or type(selected[0]) is not str:
        return 'INVALID'
    return _PROVIDERS.get(selected[0], 'OTHER')


def has_router_metadata(value):
    return _metadata(value) is not _MISSING


def router_observation(value):
    """Discard all raw metadata, including requested model, summary and pipeline."""
    metadata = _metadata(value)
    if metadata is _MISSING:
        return RouterObservation()
    if type(metadata) is not dict:
        return RouterObservation('INVALID', 'INVALID', 'INVALID')
    attempt = metadata.get('attempt', _MISSING)
    bucket = ('MISSING' if attempt is _MISSING else 'INVALID'
              if type(attempt) is not int or attempt < 0 else
              'ZERO' if attempt == 0 else 'ONE' if attempt == 1 else 'MULTIPLE')
    return RouterObservation('VALID', _selected(metadata), bucket)


def error_router_observation(error):
    """Inspect already-buffered errors only. Never read a stream or reuse a hint."""
    if not isinstance(error, APIError):
        return RouterObservation()
    stored = vars(error)
    body = stored.get('body')
    if type(body) is dict and 'openrouter_metadata' in body:
        return router_observation(body)
    # The SDK unwraps HTTP errors and can drop the top-level metadata sibling.
    response = stored.get('response')
    if type(response) is not httpx.Response:
        return RouterObservation()
    content = vars(response).get('_content')
    if type(content) is not bytes:
        return RouterObservation()
    if len(content) > 65536:
        return RouterObservation('INVALID', 'INVALID', 'INVALID')
    try:
        return router_observation(json.loads(content))
    except (ValueError, UnicodeError, RecursionError):
        return RouterObservation('INVALID', 'INVALID', 'INVALID')
