import uuid

from locust import HttpUser, between, task


class PaymentUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task
    def create_payment(self):
        order_id = f"LOAD-{uuid.uuid4().hex[:12]}"
        self.client.post(
            "/api/v1/payments",
            headers={
                "X-API-Key": "test-api-key-12345",
                "Idempotency-Key": str(uuid.uuid4()),
                "X-Mock-Response": "success",
            },
            json={
                "merchant_order_id": order_id,
                "amount": 10000,
                "currency": "INR",
                "payment_method": "card",
                "customer": {"id": "load", "email": "load@example.com", "phone": "9999999999"},
            },
        )
