# Запуск только этого файла: `.venv/bin/python manage.py test tests.test_frontend_templates --settings=fishsite.settings_test --noinput`.
# Профиль `fishsite.settings_test` использует изолированную тестовую БД PostgreSQL
# (`127.0.0.1:15432`, база `test_fishsite`); `TestCase` разрешает шаблонному
# контекст-процессору читать общие настройки сайта в этой тестовой БД.
# Сценарии собирают фиктивный GET-запрос через `RequestFactory`, задают синтетические
# данные товара и рендерят Django-шаблоны через `render_to_string`; проверяется HTML.
import re
from decimal import Decimal

from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase


class FrontendTemplateTests(TestCase):
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

    # Проверяет форму товара с целыми килограммами и кнопками шагов, затем корзину
    # с допустимым весом 2 кг и ошибочным устаревшим весом 1,5 кг: значение ошибки
    # сохраняется, сумма строки и общая сумма отмечаются недоступными, а live-status
    # остаётся доступен; проверяются связанные ARIA-атрибуты и отсутствие статуса
    # «Сумма пересчитана.».
    def test_weight_controls_use_whole_kilograms_and_preserve_invalid_legacy_value(self):
        detail = self.render(
            "catalog/product.html",
            {"product": self.product, "initial_weight": "1", "forms_enabled": True},
        )
        number_input = re.search(r'<input id="product-weight"[^>]+>', detail).group()
        for attribute in ('inputmode="numeric"', 'min="1"', 'max="99999"', 'step="1"', 'value="1"'):
            self.assertIn(attribute, number_input)
        for step in ("-1", "1"):
            self.assertIn(f'data-product-weight-step="{step}"', detail)
        self.assertIn('aria-controls="product-weight" hidden', detail)
        self.assertIn('aria-label="Уменьшить вес на 1 кг"', detail)
        self.assertIn('aria-label="Увеличить вес на 1 кг"', detail)
        self.assertIn("<legend>Добавить в корзину</legend>", detail)
        self.assertIn('name="action" value="add">Добавить в корзину</button>', detail)

        valid_item = {
            "key": "line1",
            "product": self.product,
            "price_per_kg": Decimal("100.00"),
            "weight": Decimal("2.000"),
            "weight_display": "2",
            "line_total": Decimal("200.00"),
            "weight_errors": [],
        }
        legacy_item = {
            **valid_item,
            "key": "line2",
            "weight": Decimal("1.5"),
            "weight_display": "1.5",
            "line_total": None,
            "weight_errors": ["Укажите целое количество килограммов от 1 до 99 999."],
        }
        cart = self.render(
            "orders/cart.html",
            {
                "cart_items": [valid_item, legacy_item],
                "cart_total": None,
                "cart_total_is_valid": False,
                "forms_enabled": True,
                "submission": {"consent_url": "/personal-data-consent/revisions/sha256:abc/"},
            },
        )
        self.assertIn('type="number" inputmode="numeric" min="1" max="99999" step="1" value="2"', cart)
        self.assertIn('min="1" max="99999" step="1" value="1.5"', cart)
        self.assertIn('aria-describedby="weight-error-line2"', cart)
        self.assertIn('data-cart-step="-1"', cart)
        self.assertIn('aria-label="Уменьшить вес на 1 кг"', cart)
        self.assertIn('data-cart-step="1"', cart)
        self.assertIn('aria-label="Увеличить вес на 1 кг"', cart)
        self.assertIn('data-cart-line-total-value data-cart-line-total-available="true">200,00 ₽', cart)
        self.assertIn('data-cart-line-total-value data-cart-line-total-available="false" aria-hidden="true">—', cart)
        self.assertIn('data-cart-total data-cart-total-available="false" class="is-unavailable"', cart)
        self.assertIn('data-cart-total-value aria-hidden="true">—</strong>', cart)
        self.assertIn('data-cart-total-status role="status" aria-live="polite"', cart)
        self.assertNotIn('data-cart-status', cart)
        self.assertNotIn("Сумма пересчитана.", cart)

    # Для сводки ошибок проверяет два варианта `subTest`: корзина с ошибками выбора
    # получения, согласия и подтверждения изменившейся корзины, затем оптовая форма
    # с ошибками телефона, почты и согласия. В обоих случаях каждая ссылка сводки
    # ведёт на существующий id. Отдельный рендер оптовой формы без ошибок подтверждает,
    # что поля потребности и подпись «Опишите потребность» не выводятся.
    def test_error_summary_targets_existing_controls(self):
        contexts = (
            (
                "orders/cart.html",
                {
                    "cart_items": [{"key": "1", "product": self.product, "weight": Decimal("1.0"), "weight_display": "1", "price_per_kg": Decimal("100.00"), "line_total": Decimal("100.00"), "weight_errors": []}],
                    "cart_total": Decimal("100.00"),
                    "cart_total_is_valid": True,
                    "cart_issues": False,
                    "forms_enabled": True,
                    "price_changed": True,
                    "cart_changes": [{"message": "Цена обновилась"}],
                    "submission": {
                        "submission_key": "key",
                        "submission_token": "token",
                        "consent_url": "/personal-data-consent/revisions/sha256:abc/",
                    },
                    "field_errors": {"pickup_method": ["Выберите способ"], "consent": ["Нужно согласие"], "confirm_updated_cart": ["Подтвердите изменения"]},
                },
            ),
            (
                "inquiries/wholesale.html",
                {
                    "forms_enabled": True,
                    "field_errors": {"phone": ["Укажите телефон"], "email": ["Проверьте почту"], "consent": ["Нужно согласие"]},
                    "submission": {"consent_url": "/personal-data-consent/revisions/sha256:abc/"},
                },
            ),
        )
        for template, context in contexts:
            with self.subTest(template=template):
                rendered = self.render(template, context)
                ids = set(re.findall(r'\bid="([^"]+)"', rendered))
                summary_targets = set(re.findall(r'href="#(id_[^"]+)"', rendered))
                self.assertTrue(summary_targets)
                self.assertFalse(summary_targets - ids)

        wholesale = self.render(
            "inquiries/wholesale.html",
            {
                "forms_enabled": True,
                "submission": {
                    "submission_key": "key",
                    "submission_token": "token",
                    "consent_url": "/personal-data-consent/revisions/sha256:abc/",
                },
            },
        )
        self.assertNotIn('name="need"', wholesale)
        self.assertNotIn("Опишите потребность", wholesale)

    # Рендерит главную с одним рекомендуемым товаром и подтверждённым описанием:
    # заголовок сообщает о рекомендуемых товарах, пустое состояние каталога скрыто,
    # а текст о компании присутствует.
    def test_populated_home_has_populated_heading(self):
        html = self.render(
            "home.html",
            {"catalog_ready": True, "categories": [], "featured_products": [self.product], "about_summary": "Подтверждённый текст"},
        )
        self.assertIn("Рекомендуемые товары", html)
        self.assertNotIn("В каталоге пока пусто", html)
        self.assertIn("Подтверждённый текст", html)

    # Рендерит страницу категории с товаром и переданным устаревшим контекстом
    # подкатегории: название товара показывается, но название подкатегории и секция
    # «Подкатегории» в HTML отсутствуют.
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
