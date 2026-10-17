"""The caller ANI header is trusted only in a bounded, validated shape."""

from types import SimpleNamespace

from api.internal.managed_turns import caller_phone_header


def test_valid_belarusian_caller_number_is_accepted():
    request = SimpleNamespace(headers={"X-Caller-Phone": "+375291112233"})
    assert caller_phone_header(request) == "+375291112233"


def test_invalid_or_missing_caller_number_is_dropped():
    assert caller_phone_header(SimpleNamespace(headers={})) == ""
    assert caller_phone_header(SimpleNamespace(headers={"X-Caller-Phone": "+15551234567"})) == ""
    assert caller_phone_header(SimpleNamespace(headers={"X-Caller-Phone": "375291112233"})) == ""
    assert caller_phone_header(SimpleNamespace(headers={"X-Caller-Phone": "+37529111223"})) == ""
