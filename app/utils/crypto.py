"""
Cryptographic utilities.
CRITICAL: Always use hmac.compare_digest() — never == for signatures.
== is vulnerable to timing attacks. compare_digest() runs in constant time.
"""
import hashlib
import hmac
import time
import uuid


def verify_razorpay_signature(raw_body: str, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def verify_stripe_signature(raw_body: str, stripe_header: str, secret: str) -> bool:
    """Stripe uses timestamp.body signed with HMAC-SHA256."""
    try:
        parts = dict(p.split("=", 1) for p in stripe_header.split(","))
        timestamp = parts.get("t", "")
        received_sig = parts.get("v1", "")
        signed_payload = f"{timestamp}.{raw_body}"
        expected = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        # Check timestamp tolerance (5 minutes)
        if abs(time.time() - int(timestamp)) > 300:
            return False
        return hmac.compare_digest(received_sig, expected)
    except Exception:
        return False


def verify_payu_signature(payload: dict, received_hash: str, salt: str) -> bool:
    hash_str = "|".join([
        salt, payload.get("status", ""),
        "", "", "", "", "", "", "", "", "",
        payload.get("email", ""), payload.get("firstname", ""),
        payload.get("productinfo", ""), payload.get("amount", ""),
        payload.get("txnid", ""), payload.get("key", ""),
    ])
    expected = hashlib.sha512(hash_str.encode()).hexdigest()
    try:
        return hmac.compare_digest(received_hash.lower(), expected.lower())
    except Exception:
        return False


def verify_upi_signature(raw_body: str, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def hash_payload(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def generate_trace_id() -> str:
    return str(uuid.uuid4())


def generate_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"