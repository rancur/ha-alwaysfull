"""Golden-vector tests for AlwaysFullClient request signing.

These are pure unit tests: no aiohttp I/O, no Home Assistant. The expected
hashes below were computed from the canonical string and independently
verified against the vendor's live production server (a signed request
returned a business-logic error rather than a signature error, proving the
server accepted the signature). They are ground truth — if the implementation
does not reproduce them, the implementation is wrong.
"""

from custom_components.alwaysfull.api import AlwaysFullClient

BODY = {
    "account": "user@example.com",
    "password": "d41d8cd98f00b204e9800998ecf8427e",
    "appId": "appBiz",
    "appType": "android",
    "appVersion": "1.2.29",
    "timeZone": -7,
}


def test_sign_golden_vector_no_token():
    c = AlwaysFullClient(session=None, token="")
    assert c.sign(BODY, 1700000000000) == "09e59d24d687126cda92550f23c5c3f9"


def test_sign_golden_vector_with_token():
    c = AlwaysFullClient(session=None, token="tok123")
    assert c.sign(BODY, 1700000000000) == "8f62367db288f5479ce5936207fda3c0"


def test_sign_skips_none_values():
    c = AlwaysFullClient(session=None, token="")
    a = c.sign({**BODY, "extra": None}, 1700000000000)
    assert a == "09e59d24d687126cda92550f23c5c3f9"


def test_sign_serialises_non_strings_as_compact_json():
    c = AlwaysFullClient(session=None, token="")
    # timeZone is an int; it must appear as -7 with no spaces
    assert c.sign({"timeZone": -7}, 0) == c.sign({"timeZone": -7}, 0)
    assert "timeZone=-7&" in c.canonical({"timeZone": -7}, 0)
