"""Inbound Bot Framework JWT validation (no Bot SDK) with a locally generated RSA key."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from coworker import botconn

APP, TENANT = "bot-app", "tid"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ACT = {"serviceUrl": "https://smba.trafficmanager.net/amer/"}


def tok(key=KEY, **over):
    claims = {"iss": botconn.BF_ISSUER, "aud": APP, "exp": int(time.time()) + 600,
              "serviceurl": "https://smba.trafficmanager.net/amer/"}
    claims.update(over)
    return "Bearer " + jwt.encode(claims, key, algorithm="RS256")


def check(header, act=ACT):
    return botconn.validate_inbound(header, act, app_id=APP, tenant_id=TENANT, key_resolver=lambda t: KEY.public_key())


def test_valid_token():
    assert check(tok())["aud"] == APP


@pytest.mark.parametrize("header", [None, "Basic x", tok(aud="other"), tok(iss="https://evil"), tok(OTHER),
                                    tok(exp=int(time.time()) - 3600)])
def test_rejected(header):
    with pytest.raises(botconn.AuthError):
        check(header)


def test_service_url_mismatch_rejected():
    with pytest.raises(botconn.AuthError):
        check(tok(), {"serviceUrl": "https://attacker.example/"})
