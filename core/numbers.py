from decimal import Decimal

TWO_PLACES = Decimal("0.01")


def money(value) -> str:
    """Marks as a two-decimal string for display (internally negative marks keep four decimals)."""
    return str(Decimal(str(value)).quantize(TWO_PLACES))
