from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def calculate_line_total(price_per_kg: Decimal, desired_weight: Decimal) -> Decimal:
    """Calculate a preliminary line amount using explicit currency rounding."""
    if not isinstance(price_per_kg, Decimal) or not isinstance(desired_weight, Decimal):
        raise TypeError("Цена и вес должны быть Decimal.")
    return (price_per_kg * desired_weight).quantize(CENT, rounding=ROUND_HALF_UP)
