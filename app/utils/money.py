"""
Money utilities — all amounts in integer PAISE (1 INR = 100 paise).
NEVER use float for money. 0.1 + 0.2 != 0.3 in floating point.
"""


def rupees_to_paise(rupees: float | int | str) -> int:
    """Convert rupees to paise as integer. '100.50' → 10050"""
    s = str(rupees)
    parts = s.split(".")
    whole = int(parts[0]) * 100
    if len(parts) > 1:
        decimal = parts[1][:2].ljust(2, "0")
        whole += int(decimal)
    return whole


def paise_to_rupees(paise: int) -> str:
    """10050 → '100.50'"""
    return f"{paise // 100}.{paise % 100:02d}"


def validate_amount(paise: int) -> tuple[bool, str]:
    if not isinstance(paise, int):
        return False, "Amount must be an integer (in paise)"
    if paise <= 0:
        return False, "Amount must be greater than 0"
    if paise > 10_000_000_000:
        return False, "Amount exceeds maximum limit (10 Crore)"
    return True, ""