$headers = @{
  "X-API-Key" = "test-api-key-12345"
  "Idempotency-Key" = [guid]::NewGuid().ToString()
  "X-Mock-Response" = "timeout"
  "Content-Type" = "application/json"
}

$body = @{
  merchant_order_id = "CHAOS-$([guid]::NewGuid().ToString('N').Substring(0, 10))"
  amount = 10000
  currency = "INR"
  payment_method = "card"
  customer = @{ id = "chaos"; email = "chaos@example.com"; phone = "9999999999" }
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/v1/payments" -Headers $headers -Body $body
