from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Product


class PriceRevisionConflict(Exception):
    def __init__(self, product):
        self.product = product
        super().__init__("Цена изменилась после загрузки формы.")


def _decimal_price(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({"price_per_kg": "Введите цену числом."}) from exc
    if not price.is_finite() or price <= 0 or price.as_tuple().exponent < -2:
        raise ValidationError({"price_per_kg": "Цена должна быть больше нуля и содержать не больше двух знаков после запятой."})
    return Product._meta.get_field("price_per_kg").clean(price, None)


@transaction.atomic
def change_product_price(*, product_id, price, actor=None, source=Product.PriceSource.MANUAL, expected_revision=None):
    """Update the current price and its metadata under a row lock."""
    product = Product.objects.select_for_update().get(pk=product_id)
    if expected_revision is not None and product.price_revision != expected_revision:
        raise PriceRevisionConflict(product)

    new_price = _decimal_price(price)
    if product.price_per_kg == new_price and product.price_source == source:
        return product, False

    changed_at = timezone.now()
    revision = product.price_revision + 1
    Product.objects.filter(pk=product.pk).update(
        price_per_kg=new_price,
        price_revision=revision,
        price_changed_at=changed_at,
        price_changed_by=actor,
        price_source=source,
    )
    product.price_per_kg = new_price
    product.price_revision = revision
    product.price_changed_at = changed_at
    product.price_changed_by = actor
    product.price_source = source
    return product, True
