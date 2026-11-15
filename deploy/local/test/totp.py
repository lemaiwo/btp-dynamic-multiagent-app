"""The current TOTP code for a base32 key (RFC 6238: SHA-1, 6 digits, 30 s).
Test helper: ``python3 totp.py <base32 key>`` prints the code."""
import base64
import hashlib
import hmac
import struct
import sys
import time


def code(key: str, at: float | None = None) -> str:
    secret = base64.b32decode(key + "=" * (-len(key) % 8))
    counter = struct.pack(">Q", int((time.time() if at is None else at) // 30))
    digest = hmac.new(secret, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


if __name__ == "__main__":
    print(code(sys.argv[1]))
