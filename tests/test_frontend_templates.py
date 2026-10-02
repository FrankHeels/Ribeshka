import re
from decimal import Decimal

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase


class FrontendTemplateTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")
        # These tests exercise markup with an explicit empty navigation fixture.
        # Catalog navigation queries are covered by request-level view tests.
        self.request.navigation_categories = []
        self.product = {
            "id": 1,
            "slug": "sample",
            "name": "Тестовый товар",
            "price_per_kg": Decimal("100.00"),
            "minimum_weight": Decimal("0.1"),
            "weight_step": Decimal("0.1"),
            "photos": [],
        }

    def render(self, template, context):
        return render_to_string(template, context, request=self.request)

    def test_fractional_weight_uses_html_number_format(self):
        detail = self.render(
            "catalog/product.html",
            {"product": self.product, "initial_weight": Decimal("1.0"), "forms_enabled": True},
        )
        number_input = re.search(r'<input id="product-weight"[^>]+>', detail).group()
        for attribute in ('min="0.1"', 'step="0.1"', 'value="1.0"'):
            self.assertIn(attribute, number_input)

        item = {
            "key": "line1",
            "product": self.product,
            "price_per_kg": Decimal("100.00"),
            "weight": Decimal("1.0"),
            "minimum_weight": Decimal("0.1"),
            "weight_step": Decimal("0.1"),
            "line_total": Decimal("100.00"),
            "weight_errors": ["Неверный вес"],
        }
        cart = self.render(
            "orders/cart.html",
            {"cart_items": [item, {**item, "key": "line2", "weight_errors": []}], "cart_total": Decimal("200.00"), "forms_enabled": True},
        )
        self.assertIn('min="0.1" step="0.1" value="1.0"', cart)
        self.assertEqual(cart.count("Неверный вес"), 1)
        self.assertIn('aria-describedby="weight-error-line1"', cart)

    def test_error_summary_targets_existing_controls(self):
        contexts = (
            (
                "orders/checkout.html",
                {
                    "cart_items": [{"product": self.product, "weight": Decimal("1.0"), "price_per_kg": Decimal("100.00"), "line_total": Decimal("100.00")}],
                    "cart_total": Decimal("100.00"),
                    "forms_enabled": True,
                    "price_changed": True,
                    "field_errors": {"pickup_method": ["Выберите способ"], "consent": ["Нужно согласие"], "confirm_updated_cart": ["Подтвердите изменения"]},
                },
            ),
            (
                "inquiries/wholesale.html",
                {"forms_enabled": True, "field_errors": {"phone": ["Укажите телефон"], "email": ["Проверьте почту"], "consent": ["Нужно согласие"]}},
            ),
        )
        for template, context in contexts:
            with self.subTest(template=template):
                rendered = self.render(template, context)
                ids = set(re.findall(r'\bid="([^"]+)"', rendered))
                summary_targets = set(re.findall(r'href="#(id_[^"]+)"', rendered))
                self.assertTrue(summary_targets)
                self.assertFalse(summary_targets - ids)

    def test_populated_home_has_populated_heading(self):
        html = self.render(
            "home.html",
            {"catalog_ready": True, "categories": [], "featured_products": [self.product], "about_summary": "Подтверждённый текст"},
        )
        self.assertIn("Рекомендуемые товары", html)
        self.assertNotIn("В каталоге пока пусто", html)
        self.assertIn("Подтверждённый текст", html)

    def test_category_page_does_not_render_legacy_subcategory_context(self):
        html = self.render(
            "catalog/category.html",
            {
                "category": {"name": "Рыба", "description": "Описание категории"},
                "products": [self.product],
                "child_categories": [{"name": "Лишняя подкатегория", "slug": "nested"}],
            },
        )
        self.assertIn("Тестовый товар", html)
        self.assertNotIn("Лишняя подкатегория", html)
        self.assertNotIn("Подкатегории", html)
