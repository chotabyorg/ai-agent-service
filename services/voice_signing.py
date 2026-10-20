"""Reuse parsed key material inside one owned, explicitly enabled Managed Turn."""
from contextlib import contextmanager
from contextvars import ContextVar


_current = ContextVar("voice_signing_material", default=None)


@contextmanager
def managed_signing_scope(owner):
    # Identity is checked as well as ContextVar locality: inherited child contexts
    # and another runtime cannot use this Turn's entry. No token/grant is cached.
    entry = {"owner": owner, "pem": None, "key": None}
    token = _current.set(entry)
    try:
        yield
    finally:
        entry.clear()
        _current.reset(token)


# Keep the existing Voice helper import compatible with its selected profile.
voice_signing_scope = managed_signing_scope


def signing_key(owner, pem, load):
    entry = _current.get()
    if owner is None or entry is None or entry.get("owner") is not owner:
        return load(pem)
    if entry["key"] is not None and entry["pem"] == pem:
        return entry["key"]
    # Rotation, including an invalid new key, must never fall back to old material.
    entry["pem"], entry["key"] = None, None
    key = load(pem)
    entry["pem"], entry["key"] = pem, key
    return key
