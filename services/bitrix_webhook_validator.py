"""Validation and normalization for Bitrix24 webhook credentials."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

import httpx

PORTAL_URL_RE = re.compile(
    r"^https://(?P<portal>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.bitrix24\.(?P<tld>[a-z]{2,24})/?$",
    re.IGNORECASE,
)
WEBHOOK_PATH_RE = re.compile(r"^/rest/(?P<user_id>[1-9]\d*)/(?P<token>[a-zA-Z0-9._-]{3,})(?:/|$)")


class BitrixCredentialsValidationStatus(Enum):
    VALID = "valid"
    INVALID_FORMAT = "invalid_format"
    UNSAFE_HOST = "unsafe_host"
    INVALID_WEBHOOK = "invalid_webhook"
    INVALID_CREDENTIALS = "invalid_credentials"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"


@dataclass(slots=True)
class BitrixCredentialsValidationResult:
    status: BitrixCredentialsValidationStatus
    normalized_credentials: dict[str, str] | None = None
    details: str | None = None


def _generic_invalid_webhook_message() -> str:
    return "webhook_url must be a valid Bitrix24 inbound webhook URL."


def _normalize_portal_url(portal_url: str) -> str | None:
    value = portal_url.strip()
    if len(value) > 2048:
        return None

    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        return None
    if parsed.scheme.lower() != "https":
        return None
    if parsed.query or parsed.fragment:
        return None
    if parsed.port not in (None, 443):
        return None
    if parsed.path not in ("", "/"):
        return None

    normalized = f"https://{(parsed.hostname or '').lower().rstrip('.')}"
    if not PORTAL_URL_RE.match(normalized):
        return None
    return normalized


def _normalize_webhook_url(webhook_url: str) -> tuple[str, str] | None:
    value = webhook_url.strip()
    if len(value) > 2048:
        return None

    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        return None
    if parsed.scheme.lower() != "https":
        return None
    if parsed.fragment:
        return None
    if parsed.port not in (None, 443):
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    portal_url = f"https://{host}"
    if not PORTAL_URL_RE.match(portal_url):
        return None

    path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    match = WEBHOOK_PATH_RE.match(path)
    if not match:
        return None

    user_id = match.group("user_id")
    token = match.group("token")
    return portal_url, f"{portal_url}/rest/{user_id}/{token}/"


def _is_public_ip(ip_string: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_string)
    except ValueError:
        return False
    return not (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or ip_obj.is_reserved
    )


async def _is_public_host(hostname: str) -> bool:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    ip_values: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            ip_values.add(sockaddr[0])

    if not ip_values:
        return False
    return all(_is_public_ip(ip) for ip in ip_values)


def _classify_response(status_code: int, error_code: str) -> BitrixCredentialsValidationStatus:
    if status_code == 200:
        return BitrixCredentialsValidationStatus.VALID
    if status_code == 401 or error_code == "NO_AUTH_FOUND":
        return BitrixCredentialsValidationStatus.INVALID_CREDENTIALS
    if status_code == 403 or error_code in {"ACCESS_DENIED", "INSUFFICIENT_SCOPE"}:
        return BitrixCredentialsValidationStatus.ACCESS_DENIED
    if status_code == 404:
        return BitrixCredentialsValidationStatus.INVALID_WEBHOOK
    if status_code in {429, 500, 502, 503, 504} or error_code in {
        "QUERY_LIMIT_EXCEEDED",
        "OVERLOAD_LIMIT",
    }:
        return BitrixCredentialsValidationStatus.RATE_LIMITED
    return BitrixCredentialsValidationStatus.UPSTREAM_ERROR


async def _probe_bitrix_endpoint(
    url: str,
    client: httpx.AsyncClient | None = None,
) -> BitrixCredentialsValidationStatus:
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as local_client:
                response = await local_client.post(
                    url,
                    json={},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
        else:
            response = await client.post(
                url,
                json={},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return BitrixCredentialsValidationStatus.UPSTREAM_ERROR

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    error_code = str(payload.get("error", "")).upper()
    return _classify_response(response.status_code, error_code)


async def validate_bitrix_credentials(credentials: dict[str, object]) -> BitrixCredentialsValidationResult:
    raw_webhook = credentials.get("webhook_url")
    if not isinstance(raw_webhook, str) or not raw_webhook.strip():
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.INVALID_WEBHOOK,
            details="webhook_url is required for Bitrix credentials.",
        )

    normalized_webhook = _normalize_webhook_url(raw_webhook)
    if normalized_webhook is None:
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.INVALID_WEBHOOK,
            details=_generic_invalid_webhook_message(),
        )

    portal_url, webhook_url = normalized_webhook
    raw_portal = credentials.get("portal_url")
    normalized_portal_url: str | None = None
    if isinstance(raw_portal, str) and raw_portal.strip():
        normalized_portal_url = _normalize_portal_url(raw_portal)
        if normalized_portal_url is None or normalized_portal_url != portal_url:
            return BitrixCredentialsValidationResult(
                status=BitrixCredentialsValidationStatus.INVALID_FORMAT,
                details="portal_url must match the Bitrix24 portal in webhook_url.",
            )

    host = urlsplit(portal_url).hostname
    if not host or not await _is_public_host(host):
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.UNSAFE_HOST,
            details="Bitrix portal host must resolve to public IP addresses.",
        )

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        webhook_probe_status = await _probe_bitrix_endpoint(f"{webhook_url}server.time", client=client)

    if webhook_probe_status == BitrixCredentialsValidationStatus.VALID:
        normalized_credentials = {"webhook_url": webhook_url}
        if normalized_portal_url is not None:
            normalized_credentials["portal_url"] = normalized_portal_url
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.VALID,
            normalized_credentials=normalized_credentials,
        )

    if webhook_probe_status in {
        BitrixCredentialsValidationStatus.INVALID_WEBHOOK,
        BitrixCredentialsValidationStatus.INVALID_CREDENTIALS,
        BitrixCredentialsValidationStatus.ACCESS_DENIED,
    }:
        return BitrixCredentialsValidationResult(
            status=webhook_probe_status,
            details="Bitrix webhook is invalid or has insufficient permissions.",
        )

    return BitrixCredentialsValidationResult(
        status=webhook_probe_status,
        details="Could not validate Bitrix webhook due to upstream issue.",
    )
