from enum import Enum


class TransactionState(str, Enum):
    CREATED = "CREATED"
    ROUTE_SELECTED = "ROUTE_SELECTED"
    AUTH_INITIATED = "AUTH_INITIATED"
    AUTHORISED = "AUTHORISED"
    AUTH_FAILED = "AUTH_FAILED"
    AUTH_TIMEOUT = "AUTH_TIMEOUT"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    CAPTURE_INITIATED = "CAPTURE_INITIATED"
    CAPTURED = "CAPTURED"
    PARTIALLY_CAPTURED = "PARTIALLY_CAPTURED"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    VOID_INITIATED = "VOID_INITIATED"
    VOIDED = "VOIDED"
    REFUND_INITIATED = "REFUND_INITIATED"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUND_FAILED = "REFUND_FAILED"
    SETTLED = "SETTLED"
    ABANDONED = "ABANDONED"
    FAILED = "FAILED"
    DISPUTE_OPENED = "DISPUTE_OPENED"
    DISPUTE_RESOLVED = "DISPUTE_RESOLVED"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"


class GatewayName(str, Enum):
    RAZORPAY = "razorpay"
    STRIPE = "stripe"
    PAYU = "payu"
    UPI = "upi"


class PaymentMethod(str, Enum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


# Which gateways support which payment methods
GATEWAY_METHODS: dict[GatewayName, list[PaymentMethod]] = {
    GatewayName.RAZORPAY: [PaymentMethod.CARD, PaymentMethod.UPI, PaymentMethod.NETBANKING, PaymentMethod.WALLET, PaymentMethod.EMI],
    GatewayName.STRIPE:   [PaymentMethod.CARD, PaymentMethod.WALLET],
    GatewayName.PAYU:     [PaymentMethod.CARD, PaymentMethod.NETBANKING, PaymentMethod.WALLET, PaymentMethod.EMI],
    GatewayName.UPI:      [PaymentMethod.UPI],
}

# Valid state transitions — the ONLY allowed moves
VALID_TRANSITIONS: dict[TransactionState, list[TransactionState]] = {
    TransactionState.CREATED:             [TransactionState.ROUTE_SELECTED, TransactionState.ABANDONED],
    TransactionState.ROUTE_SELECTED:      [TransactionState.AUTH_INITIATED, TransactionState.FAILED],
    TransactionState.AUTH_INITIATED:      [TransactionState.AUTHORISED, TransactionState.AUTH_FAILED,
                                           TransactionState.AUTH_TIMEOUT, TransactionState.CAPTURED,
                                           TransactionState.AUTH_EXPIRED],
    TransactionState.AUTHORISED:          [TransactionState.CAPTURE_INITIATED, TransactionState.VOID_INITIATED,
                                           TransactionState.AUTH_EXPIRED, TransactionState.CAPTURED],
    TransactionState.AUTH_FAILED:         [TransactionState.ROUTE_SELECTED, TransactionState.FAILED],
    TransactionState.AUTH_TIMEOUT:        [TransactionState.ROUTE_SELECTED, TransactionState.AUTHORISED, TransactionState.FAILED],
    TransactionState.AUTH_EXPIRED:        [TransactionState.FAILED],
    TransactionState.CAPTURE_INITIATED:   [TransactionState.CAPTURED, TransactionState.PARTIALLY_CAPTURED, TransactionState.CAPTURE_FAILED],
    TransactionState.CAPTURED:            [TransactionState.REFUND_INITIATED, TransactionState.SETTLED,
                                           TransactionState.DISPUTE_OPENED, TransactionState.RECONCILIATION_MISMATCH],
    TransactionState.PARTIALLY_CAPTURED:  [TransactionState.CAPTURE_INITIATED, TransactionState.REFUND_INITIATED,
                                           TransactionState.VOID_INITIATED, TransactionState.SETTLED],
    TransactionState.CAPTURE_FAILED:      [TransactionState.CAPTURE_INITIATED, TransactionState.VOID_INITIATED, TransactionState.FAILED],
    TransactionState.VOID_INITIATED:      [TransactionState.VOIDED, TransactionState.FAILED],
    TransactionState.VOIDED:              [],
    TransactionState.REFUND_INITIATED:    [TransactionState.REFUNDED, TransactionState.PARTIALLY_REFUNDED, TransactionState.REFUND_FAILED],
    TransactionState.REFUNDED:            [],
    TransactionState.PARTIALLY_REFUNDED:  [TransactionState.REFUND_INITIATED, TransactionState.SETTLED],
    TransactionState.REFUND_FAILED:       [TransactionState.REFUND_INITIATED, TransactionState.FAILED],
    TransactionState.SETTLED:             [TransactionState.REFUND_INITIATED, TransactionState.DISPUTE_OPENED,
                                           TransactionState.RECONCILIATION_MISMATCH],
    TransactionState.ABANDONED:           [],
    TransactionState.FAILED:              [],
    TransactionState.DISPUTE_OPENED:      [TransactionState.DISPUTE_RESOLVED, TransactionState.REFUNDED],
    TransactionState.DISPUTE_RESOLVED:    [],
    TransactionState.RECONCILIATION_MISMATCH: [TransactionState.CAPTURED, TransactionState.FAILED, TransactionState.REFUNDED],
}

TERMINAL_STATES = {
    TransactionState.REFUNDED, TransactionState.FAILED, TransactionState.ABANDONED,
    TransactionState.VOIDED, TransactionState.DISPUTE_RESOLVED,
}

REFUNDABLE_STATES = {
    TransactionState.CAPTURED, TransactionState.PARTIALLY_CAPTURED, TransactionState.SETTLED,
}

# Default routing weights (must sum to 1.0)
DEFAULT_WEIGHTS = {
    "success_rate": 0.35,
    "latency":      0.20,
    "cost":         0.20,
    "health":       0.15,
    "fit":          0.10,
}

# Gateway costs: (percentage, fixed_paise)
GATEWAY_COSTS: dict[GatewayName, tuple[float, int]] = {
    GatewayName.RAZORPAY: (0.02,  200),
    GatewayName.STRIPE:   (0.025, 300),
    GatewayName.PAYU:     (0.018, 150),
    GatewayName.UPI:      (0.0,   0),
}

# Gateway timeouts in seconds
GATEWAY_TIMEOUTS: dict[GatewayName, int] = {
    GatewayName.RAZORPAY: 30,
    GatewayName.STRIPE:   30,
    GatewayName.PAYU:     45,
    GatewayName.UPI:      60,
}