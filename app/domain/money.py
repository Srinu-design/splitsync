# app/domain/money.py
import re
from decimal import Decimal

RUPEE_RE = re.compile(r"^(0|[1-9]\d{0,6})(\.\d{1,2})?$")
MAX_PAISE = 100_000_000          # ₹10,00,000.00


class InvalidAmount(ValueError):
    pass


def parse_rupees(s: str) -> int:
    """'100' -> 10000, '100.5' -> 10050, '0.01' -> 1. Never touches float."""
    s = s.strip()
    if not RUPEE_RE.match(s):
        raise InvalidAmount(s)
    paise = int((Decimal(s) * 100).to_integral_exact())
    if not 1 <= paise <= MAX_PAISE:
        raise InvalidAmount(s)
    return paise
