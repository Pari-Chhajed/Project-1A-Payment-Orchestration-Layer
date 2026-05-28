"""Unit tests for money utilities and cryptographic functions."""
import hashlib
import hmac
import pytest
from app.utils.money import rupees_to_paise, paise_to_rupees, validate_amount
from app.utils.crypto import (
    verify_razorpay_signature, verify_stripe_signature,
    hash_payload, verify_upi_signature,
)
import time


# ── Money ─────────────────────────────────────────────────────────
class TestMoney:
    def test_100_rupees_equals_10000_paise(self):
        assert rupees_to_paise(100) == 10000

    def test_2_50_rupees_equals_250_paise(self):
        assert rupees_to_paise("2.50") == 250

    def test_no_float_rounding_error(self):
        # 0.1 + 0.2 in float = 0.30000000000000004, but we use int
        assert rupees_to_paise("0.10") == 10
        assert rupees_to_paise("0.20") == 20

    def test_2800_rupees_equals_280000_paise(self):
        assert rupees_to_paise(2800) == 280000

    def test_paise_to_rupees_10000(self):
        assert paise_to_rupees(10000) == "100.00"

    def test_paise_to_rupees_250(self):
        assert paise_to_rupees(250) == "2.50"

    def test_paise_to_rupees_1(self):
        assert paise_to_rupees(1) == "0.01"

    def test_validate_positive_integer(self):
        valid, _ = validate_amount(10000)
        assert valid is True

    def test_validate_zero_invalid(self):
        valid, err = validate_amount(0)
        assert valid is False
        assert "greater than 0" in err

    def test_validate_negative_invalid(self):
        valid, _ = validate_amount(-100)
        assert valid is False

    def test_validate_too_large_invalid(self):
        valid, _ = validate_amount(99_999_999_999)
        assert valid is False

    def test_validate_float_invalid(self):
        valid, _ = validate_amount(100.5)   # type: ignore
        assert valid is False


# ── Crypto ────────────────────────────────────────────────────────
class TestCrypto:
    def test_razorpay_valid_signature(self):
        secret = "test_secret"
        body = '{"event":"payment.captured"}'
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        assert verify_razorpay_signature(body, sig, secret)

    def test_razorpay_wrong_secret(self):
        body = '{"event":"payment.captured"}'
        sig = hmac.new(b"wrong", body.encode(), hashlib.sha256).hexdigest()
        assert not verify_razorpay_signature(body, sig, "correct_secret")

    def test_razorpay_tampered_body(self):
        """FS-10: tampered payload must fail"""
        secret = "test_secret"
        original = '{"event":"payment.captured","amount":100000}'
        sig = hmac.new(secret.encode(), original.encode(), hashlib.sha256).hexdigest()
        tampered = '{"event":"payment.captured","amount":9999999}'
        assert not verify_razorpay_signature(tampered, sig, secret)

    def test_razorpay_empty_signature(self):
        assert not verify_razorpay_signature('{"x":1}', "", "secret")

    def test_stripe_valid_signature(self):
        secret = "stripe_secret"
        body = '{"id":"evt_001"}'
        ts = str(int(time.time()))
        signed = f"{ts}.{body}"
        sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        header = f"t={ts},v1={sig}"
        assert verify_stripe_signature(body, header, secret)

    def test_stripe_expired_timestamp(self):
        """Stripe replays with old timestamps must fail."""
        secret = "stripe_secret"
        body = '{"id":"evt_002"}'
        old_ts = str(int(time.time()) - 400)  # 400s ago > 5min tolerance
        signed = f"{old_ts}.{body}"
        sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
        header = f"t={old_ts},v1={sig}"
        assert not verify_stripe_signature(body, header, secret)

    def test_stripe_malformed_header(self):
        assert not verify_stripe_signature('{}', "bad_header", "secret")

    def test_hash_payload_consistent(self):
        payload = '{"key":"value"}'
        assert hash_payload(payload) == hash_payload(payload)

    def test_hash_payload_different_inputs(self):
        assert hash_payload("aaa") != hash_payload("bbb")

    def test_hash_payload_sha256_length(self):
        assert len(hash_payload("test")) == 64

    def test_upi_valid_signature(self):
        secret = "upi_secret"
        body = '{"status":"SUCCESS","txnRefNum":"UPI123"}'
        sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        assert verify_upi_signature(body, sig, secret)

    def test_upi_invalid_signature(self):
        assert not verify_upi_signature('{}', "badsig", "secret")