"""Google Contacts helper logic."""

from __future__ import annotations

import json
from typing import Any

from mcp_clients.servers.google_workspace.schemas.contacts import ContactInput
from mcp_clients.servers.google_workspace.services.lead_service import (
    normalize_email,
    normalize_phone,
    normalize_telegram,
    normalize_website,
)


def normalize_contact(contact: ContactInput) -> dict[str, Any]:
    """Normalize contact values for matching and payload generation."""

    data = contact.model_dump()
    data["email"] = normalize_email(data.get("email"))
    data["phone"] = normalize_phone(data.get("phone"))
    data["telegram"] = normalize_telegram(data.get("telegram"))
    data["website"] = normalize_website(data.get("website"))
    return data


def build_person_payload(contact: ContactInput) -> dict[str, Any]:
    """Build a Google People API person payload."""

    data = normalize_contact(contact)
    person: dict[str, Any] = {}
    if data.get("name"):
        given_name, family_name = _split_name(data["name"])
        person["names"] = [{"givenName": given_name, "familyName": family_name}]
    if data.get("email"):
        person["emailAddresses"] = [{"value": data["email"], "type": "work"}]
    if data.get("phone"):
        person["phoneNumbers"] = [{"value": data["phone"], "type": "work"}]
    if data.get("company"):
        person["organizations"] = [{"name": data["company"], "type": "work"}]
    urls = []
    if data.get("website"):
        urls.append({"value": data["website"], "type": "work"})
    if data.get("telegram"):
        urls.append({"value": f"https://t.me/{data['telegram']}", "type": "profile"})
    if urls:
        person["urls"] = urls
    notes = data.get("notes")
    if data.get("customFields"):
        custom_json = json.dumps(data["customFields"], ensure_ascii=False, sort_keys=True)
        notes = f"{notes or ''}\ncustomFields={custom_json}".strip()
    if notes:
        person["biographies"] = [{"value": notes, "contentType": "TEXT_PLAIN"}]
    return person


def find_duplicate_contact(
    people_results: list[dict[str, Any]],
    contact: ContactInput,
    dedupe_keys: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Find a duplicate People API result by configured contact keys."""

    candidate = normalize_contact(contact)
    for key in dedupe_keys:
        candidate_value = _contact_match_value(candidate, key)
        if not candidate_value:
            continue
        for result in people_results:
            person = result.get("person", result)
            if _person_match_value(person, key) == candidate_value:
                return person, key
    return None, None


def build_contact_search_query(contact: ContactInput, dedupe_keys: list[str]) -> str:
    """Build a narrow contact search query from available dedupe fields."""

    data = normalize_contact(contact)
    for key in dedupe_keys:
        value = _contact_match_value(data, key)
        if value:
            return value
    return data.get("name") or data.get("company") or ""


def contact_resource(person: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an audit-friendly contact resource."""

    return {
        "type": "google_contact",
        "resourceName": (person or {}).get("resourceName"),
    }


def _split_name(name: str) -> tuple[str, str]:
    parts = name.strip().split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _contact_match_value(contact: dict[str, Any], key: str) -> str:
    if key.startswith("custom:"):
        return str((contact.get("customFields") or {}).get(key.removeprefix("custom:")) or "").strip().lower()
    return str(contact.get(key) or "").strip().lower()


def _person_match_value(person: dict[str, Any], key: str) -> str:
    if key == "email":
        return normalize_email(_first_value(person, "emailAddresses"))
    if key == "phone":
        return normalize_phone(_first_value(person, "phoneNumbers"))
    if key == "website":
        return normalize_website(_first_value(person, "urls"))
    if key == "telegram":
        urls = person.get("urls", [])
        if isinstance(urls, list):
            for item in urls:
                value = str(item.get("value", ""))
                if "t.me/" in value:
                    return normalize_telegram(value.rsplit("/", 1)[-1])
        return ""
    if key == "company":
        return str((person.get("organizations") or [{}])[0].get("name", "")).strip().lower()
    if key == "name":
        names = person.get("names", [])
        if isinstance(names, list) and names:
            name = names[0]
            return " ".join(
                part for part in [name.get("givenName", ""), name.get("familyName", "")] if part
            ).strip().lower()
    return ""


def _first_value(person: dict[str, Any], field: str) -> str:
    values = person.get(field, [])
    if isinstance(values, list) and values:
        return str(values[0].get("value", ""))
    return ""
