"""Ping tokens: the URL *is* the credential.

Derived rather than stored, so there is no token table and only one thing
(the server secret) to rotate.
"""

import hashlib
import hmac


def derive_token(secret: str, name: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), name.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:32]
