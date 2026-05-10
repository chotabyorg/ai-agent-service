"""Validation for Bitrix24 portal and optional inbound webhook credentials."""

from __future__ import annotations

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
WEBHOOK_PATH_RE = re.compile(r"^/rest/(?P<user_id>[1-9]\d*)/(?P<token>[a-zA-Z0-9._-]{8,})/")


class BitrixCredentialsValidationStatus(Enum):
    VALID = "valid"
    INVALID_FORMAT = "invalid_format"
    UNSAFE_HOST = "unsafe_host"
    PORTAL_UNREACHABLE = "portal_unreachable"
    INVALID_WEBHOOK = "invalid_webhook"
    INVALID_CREDENTIALS = "invalid_credentials"
    ACCESS_DENIED = "access_denied"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(slots=True)
class BitrixCredentialsValidationResult:
    status: BitrixCredentialsValidationStatus
    normalized_credentials: dict[str, str] | None = None
    details: str | None = None


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


def _normalize_webhook_url(webhook_url: str, portal_url: str) -> str | None:
    value = webhook_url.strip()
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

    host = (parsed.hostname or "").lower().rstrip(".")
    portal_host = urlsplit(portal_url).hostname
    if not host or host != portal_host:
        return None

    path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    match = WEBHOOK_PATH_RE.match(path)
    if not match:
        return None

    user_id = match.group("user_id")
    token = match.group("token")
    return f"{portal_url}/rest/{user_id}/{token}/"


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


def _is_public_host(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
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
    if status_code == 403 or error_code in {"INVALID_CREDENTIALS", "ACCESS_DENIED", "INSUFFICIENT_SCOPE"}:
        return BitrixCredentialsValidationStatus.ACCESS_DENIED
    if status_code in {429, 503} or error_code in {"QUERY_LIMIT_EXCEEDED", "OVERLOAD_LIMIT"}:
        return BitrixCredentialsValidationStatus.RATE_LIMITED
    if status_code == 404:
        return BitrixCredentialsValidationStatus.PORTAL_UNREACHABLE
    return BitrixCredentialsValidationStatus.ERROR


async def _probe_bitrix_endpoint(url: str) -> BitrixCredentialsValidationStatus:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(
                url,
                json={},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError:
        return BitrixCredentialsValidationStatus.PORTAL_UNREACHABLE

    payload = {}
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    error_code = str(payload.get("error", "")).upper()
    return _classify_response(response.status_code, error_code)


async def validate_bitrix_credentials(credentials: dict[str, object]) -> BitrixCredentialsValidationResult:
    raw_portal = credentials.get("portal_url")
    raw_webhook = credentials.get("webhook_url")
    webhook_candidate: str | None = raw_webhook if isinstance(raw_webhook, str) else None

    normalized_portal_url: str | None = None
    if isinstance(raw_portal, str) and raw_portal.strip():
        normalized_portal_url = _normalize_portal_url(raw_portal)
    elif isinstance(raw_webhook, str) and raw_webhook.strip():
        stripped_webhook = raw_webhook.strip()
        # Backward compatibility: some clients send portal URL in `webhook_url`.
        portal_like_webhook = _normalize_portal_url(stripped_webhook)
        if portal_like_webhook:
            normalized_portal_url = portal_like_webhook
            webhook_candidate = None
        parsed = urlsplit(stripped_webhook)
        if parsed.scheme and parsed.hostname:
            candidate = f"https://{parsed.hostname.lower().rstrip('.')}"
            normalized_portal_url = normalized_portal_url or _normalize_portal_url(candidate)

    if not normalized_portal_url:
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.INVALID_FORMAT,
            details="portal_url must match format https://<portal>.bitrix24.<tld>.",
        )

    host = urlsplit(normalized_portal_url).hostname
    if not host or not _is_public_host(host):
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.UNSAFE_HOST,
            details="Bitrix portal host must resolve to public IP addresses.",
        )

    portal_probe_status = await _probe_bitrix_endpoint(f"{normalized_portal_url}/rest/server.time")
    if portal_probe_status not in {
        BitrixCredentialsValidationStatus.VALID,
        BitrixCredentialsValidationStatus.INVALID_CREDENTIALS,
        BitrixCredentialsValidationStatus.ACCESS_DENIED,
    }:
        details = "Bitrix portal is not reachable via REST API."
        if portal_probe_status == BitrixCredentialsValidationStatus.RATE_LIMITED:
            details = "Bitrix REST API is rate-limited or temporarily blocked."
        return BitrixCredentialsValidationResult(status=portal_probe_status, details=details)

    normalized_credentials: dict[str, str] = {"portal_url": normalized_portal_url}

    if webhook_candidate is not None:
        if not webhook_candidate.strip():
            return BitrixCredentialsValidationResult(
                status=BitrixCredentialsValidationStatus.INVALID_WEBHOOK,
                details="webhook_url must be a non-empty string when provided.",
            )

        # If webhook value is equal to portal URL, treat it as portal-only config.
        if _normalize_portal_url(webhook_candidate):
            return BitrixCredentialsValidationResult(
                status=BitrixCredentialsValidationStatus.VALID,
                normalized_credentials=normalized_credentials,
            )

        normalized_webhook = _normalize_webhook_url(webhook_candidate, normalized_portal_url)
        if not normalized_webhook:
            return BitrixCredentialsValidationResult(
                status=BitrixCredentialsValidationStatus.INVALID_WEBHOOK,
                details="webhook_url must match https://<portal>.bitrix24.<tld>/rest/<user_id>/<token>/.",
            )

        webhook_probe_status = await _probe_bitrix_endpoint(f"{normalized_webhook}server.time")
        if webhook_probe_status != BitrixCredentialsValidationStatus.VALID:
            if webhook_probe_status in {
                BitrixCredentialsValidationStatus.INVALID_CREDENTIALS,
                BitrixCredentialsValidationStatus.ACCESS_DENIED,
            }:
                details = "Webhook is invalid or has insufficient permissions."
                return BitrixCredentialsValidationResult(status=webhook_probe_status, details=details)
            if webhook_probe_status == BitrixCredentialsValidationStatus.RATE_LIMITED:
                details = "Bitrix REST API is rate-limited or temporarily blocked."
                return BitrixCredentialsValidationResult(status=webhook_probe_status, details=details)
            return BitrixCredentialsValidationResult(
                status=BitrixCredentialsValidationStatus.PORTAL_UNREACHABLE,
                details="Bitrix webhook endpoint is not reachable.",
            )

        normalized_credentials["webhook_url"] = normalized_webhook

    return BitrixCredentialsValidationResult(
        status=BitrixCredentialsValidationStatus.VALID,
        normalized_credentials=normalized_credentials,
    )
