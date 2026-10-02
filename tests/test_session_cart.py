"""PostgreSQL-backed behavior checks for the anonymous session cart."""

from decimal import Decimal

from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from catalog.models import Category, Product
from inquiries.models import Inquiry
from integrations.models import NotificationOutbox
from orders.cart import CART_SESSION_KEY, cart_count, get_cart_state
from orders.models import Order


class SessionCartTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Тестовая рыба", slug="session-cart-test")
        cls.product = Product.objects.create(
            name="Тестовый лосось",
            slug="session-cart-salmon",
            category=cls.category,
            price_per_kg=Decimal("100.00"),
            minimum_weight=Decimal("0.1"),
            weight_step=Decimal("0.1"),
            is_published=True,
        )

    def make_product(
        self,
        *,
        name="Вторая тестовая рыба",
        slug="session-cart-second",
        price="10.05",
        minimum="0.1",
        step="0.1",
        published=True,
    ):
        return Product.objects.create(
            name=name,
            slug=slug,
            category=self.category,
            price_per_kg=Decimal(price),
            minimum_weight=Decimal(minimum),
            weight_step=Decimal(step),
            is_published=published,
        )

    def add(self, client, product=None, weight="0.1", **extra):
        product = product or self.product
        return client.post(
            reverse("orders:cart"),
            {
                "action": "add",
                "product_id": str(product.pk),
                "weight": weight,
                **extra,
            },
        )

    @staticmethod
    def cart_lines(client):
        return dict(client.session.get(CART_SESSION_KEY, {}))

    @staticmethod
    def set_cart_lines(client, lines):
        session = client.session
        session[CART_SESSION_KEY] = dict(lines)
        session.save()

    def test_add_redirects_persists_and_readding_replaces_weight(self):
        client = Client()
        unrelated = client.session
        unrelated["unrelated"] = "preserve me"
        unrelated.save()

        response = self.add(client, weight="0.2")
        self.assertRedirects(response, reverse("orders:cart"), fetch_redirect_response=False)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "0.2"})

        response = self.add(client, weight="0.3")
        self.assertRedirects(response, reverse("orders:cart"), fetch_redirect_response=False)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "0.3"})
        self.assertEqual(client.session["unrelated"], "preserve me")

        cart = client.get(reverse("orders:cart"))
        self.assertContains(cart, 'value="0.3"')
        self.assertContains(cart, "30,00 ₽")
        self.assertContains(cart, 'aria-label="Корзина, товаров: 1"')

    def test_product_page_uses_saved_weight_when_the_item_is_already_in_cart(self):
        client = Client()
        response = self.add(client, weight="0.3")
        self.assertEqual(response.status_code, 302)

        page = client.get(reverse("catalog:product", args=(self.product.slug,)))
        self.assertContains(page, 'id="product-weight"')
        self.assertContains(page, 'value="0.3"')

    def test_cart_isolated_between_sessions(self):
        first, second = Client(), Client()
        self.add(first, weight="0.4")

        self.assertEqual(self.cart_lines(first), {str(self.product.pk): "0.4"})
        self.assertEqual(self.cart_lines(second), {})
        self.assertContains(second.get(reverse("orders:cart")), "Корзина пуста")

    def test_csrf_is_required_for_cart_changes(self):
        client = Client(enforce_csrf_checks=True)
        product_url = reverse("catalog:product", args=(self.product.slug,))
        page = client.get(product_url)
        token = page.cookies["csrftoken"].value

        denied = client.post(
            reverse("orders:cart"),
            {"action": "add", "product_id": str(self.product.pk), "weight": "0.1"},
        )
        self.assertEqual(denied.status_code, 403)

        accepted = client.post(
            reverse("orders:cart"),
            {
                "action": "add",
                "product_id": str(self.product.pk),
                "weight": "0.1",
                "csrfmiddlewaretoken": token,
            },
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "0.1"})

    def test_two_rounded_line_totals_are_added(self):
        second_product = self.make_product()
        client = Client()
        self.add(client, self.make_product(name="Первая дробная рыба", slug="session-cart-round-a"), "0.1")
        self.add(client, second_product, "0.1")

        response = client.get(reverse("orders:cart"))
        self.assertContains(response, "2,02 ₽")
        self.assertContains(response, "1,01 ₽", count=2)

    def test_client_price_and_total_fields_are_ignored(self):
        client = Client()
        response = self.add(
            client,
            weight="0.2",
            price_per_kg="0.01",
            line_total="0.00",
            total="0.00",
        )
        self.assertEqual(response.status_code, 302)
        page = client.get(reverse("orders:cart"))
        self.assertContains(page, "100,00 ₽")
        self.assertContains(page, "20,00 ₽")

    def test_current_price_is_reloaded_for_existing_session_line(self):
        client = Client()
        self.add(client, weight="0.2")
        Product.objects.filter(pk=self.product.pk).update(price_per_kg=Decimal("125.50"))

        page = client.get(reverse("orders:cart"))
        self.assertContains(page, "125,50 ₽")
        self.assertContains(page, "25,10 ₽")

    def test_offset_minimum_and_step_are_checked_as_exact_decimals(self):
        product = self.make_product(
            name="Рыба с дробным минимумом",
            slug="session-cart-offset-step",
            minimum="0.15",
            step="0.1",
        )
        client = Client()

        for weight in ("-0.05", "0", "0.05", "0.2", "0.3"):
            with self.subTest(weight=weight):
                response = self.add(client, product, weight)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(str(product.pk), self.cart_lines(client))

        for weight in ("0.15", "0.25", "0.35"):
            with self.subTest(weight=weight):
                response = self.add(client, product, weight)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.cart_lines(client)[str(product.pk)], weight)

    def test_invalid_add_keeps_raw_input_and_links_summary_to_weight_control(self):
        client = Client()
        response = self.add(client, weight="0")

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'id="product-weight"', status_code=400)
        self.assertContains(response, 'value="0"', status_code=400)
        self.assertContains(response, 'href="#product-weight"', status_code=400)
        self.assertEqual(self.cart_lines(client), {})

    def test_invalid_add_still_reports_other_unavailable_lines_removed_from_cart(self):
        client = Client()
        hidden = self.make_product(
            name="Недоступная тестовая рыба",
            slug="session-cart-now-hidden",
        )
        self.add(client, hidden, "0.1")
        Product.objects.filter(pk=hidden.pk).update(is_published=False)

        response = self.add(client, self.product, "0")

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Некоторые позиции больше недоступны или были удалены из корзины", status_code=400)
        self.assertNotContains(response, hidden.name, status_code=400)
        self.assertNotIn(str(hidden.pk), self.cart_lines(client))

    def test_decimal_precision_bounds_and_non_finite_values_are_rejected(self):
        client = Client()
        product = self.make_product(
            name="Точный вес",
            slug="session-cart-precision",
            minimum="0.001",
            step="0.001",
        )

        for weight in ("100000", "1.0000", "NaN", "Infinity", "-Infinity", "one kg"):
            with self.subTest(weight=weight):
                response = self.add(client, product, weight)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(str(product.pk), self.cart_lines(client))

        accepted = self.add(client, product, "99999.999")
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.cart_lines(client)[str(product.pk)], "99999.999")

    def test_scientific_decimal_input_is_saved_as_fixed_point(self):
        client = Client()
        response = self.add(client, weight="1e1")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "10"})
        self.assertContains(client.get(reverse("orders:cart")), 'value="10"')

    def test_invalid_update_preserves_saved_weight_and_shows_raw_input(self):
        client = Client()
        self.add(client, weight="0.2")
        response = client.post(
            reverse("orders:cart"),
            {"action": "update", "cart_line_id": str(self.product.pk), "weight": "0"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'value="0"', status_code=400)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "0.2"})

    def test_update_rejects_a_product_not_in_the_cart(self):
        client = Client()
        response = client.post(
            reverse("orders:cart"),
            {"action": "update", "cart_line_id": str(self.product.pk), "weight": "0.2"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.cart_lines(client), {})

    def test_remove_works_with_invalid_weight_and_repeated_remove_is_safe(self):
        client = Client()
        self.add(client, weight="0.2")
        page = client.get(reverse("orders:cart"))
        self.assertContains(page, f'value="remove" form="cart-item-{self.product.pk}" formnovalidate')

        data = {"action": "remove", "cart_line_id": str(self.product.pk), "weight": "not-a-number"}
        removed = client.post(reverse("orders:cart"), data)
        repeated = client.post(reverse("orders:cart"), data)
        self.assertEqual(removed.status_code, 302)
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(self.cart_lines(client), {})
        self.assertContains(client.get(reverse("orders:cart")), "Корзина пуста")

    def test_hidden_or_deleted_products_are_pruned_without_revealing_details(self):
        client = Client()
        secret_price = "9876.54"
        Product.objects.filter(pk=self.product.pk).update(price_per_kg=Decimal(secret_price))
        self.add(client, weight="0.1")
        Product.objects.filter(pk=self.product.pk).update(is_published=False)

        hidden_cart = client.get(reverse("orders:cart"))
        self.assertContains(hidden_cart, "Корзина пуста")
        self.assertNotContains(hidden_cart, self.product.name)
        self.assertNotContains(hidden_cart, self.product.slug)
        self.assertNotContains(hidden_cart, secret_price)
        self.assertEqual(self.cart_lines(client), {})

        Product.objects.filter(pk=self.product.pk).update(is_published=True)
        self.add(client, weight="0.1")
        Product.objects.filter(pk=self.product.pk).delete()
        deleted_cart = client.get(reverse("orders:cart"))
        self.assertContains(deleted_cart, "Корзина пуста")
        self.assertNotContains(deleted_cart, self.product.name)
        self.assertEqual(self.cart_lines(client), {})

    def test_changed_minimum_or_step_keeps_line_but_hides_invalid_total(self):
        client = Client()
        self.add(client, weight="0.2")
        Product.objects.filter(pk=self.product.pk).update(
            minimum_weight=Decimal("0.15"),
            weight_step=Decimal("0.1"),
        )

        page = client.get(reverse("orders:cart"))
        self.assertContains(page, 'value="0.2"')
        self.assertContains(page, "Минимальный вес или шаг изменился")
        self.assertNotContains(page, "Предварительная сумма <strong>")
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "0.2"})

        fixed = client.post(
            reverse("orders:cart"),
            {"action": "update", "cart_line_id": str(self.product.pk), "weight": "0.25"},
        )
        self.assertEqual(fixed.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "0.25"})
        self.assertContains(client.get(reverse("orders:cart")), "25,00 ₽")

    def test_malformed_session_data_is_discarded_without_server_error(self):
        client = Client()
        self.set_cart_lines(
            client,
            {
                "99999999999999999999": "0.1",
                "01": "0.1",
                str(self.product.pk): "NaN",
                str(self.product.pk + 100): "0.1",
            },
        )

        page = client.get(reverse("orders:cart"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Корзина пуста")
        self.assertEqual(self.cart_lines(client), {})

    def test_non_dict_session_cart_is_repaired_without_server_error(self):
        client = Client()
        session = client.session
        session[CART_SESSION_KEY] = ["bad", "shape"]
        session.save()

        page = client.get(reverse("orders:cart"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Корзина пуста")
        self.assertEqual(self.cart_lines(client), {})

    def test_malformed_add_ids_hidden_products_and_unknown_actions_fail_safely(self):
        client = Client()
        for product_id in ("", "01", "99999999999999999999", "not-an-id"):
            with self.subTest(product_id=product_id):
                response = client.post(
                    reverse("orders:cart"),
                    {"action": "add", "product_id": product_id, "weight": "0.1"},
                )
                self.assertEqual(response.status_code, 404)

        unpublished = self.make_product(
            name="Скрытый товар",
            slug="session-cart-unpublished",
            published=False,
        )
        response = self.add(client, unpublished, "0.1")
        self.assertEqual(response.status_code, 404)

        unknown = client.post(reverse("orders:cart"), {"action": "other"})
        self.assertEqual(unknown.status_code, 400)

    def test_request_cart_state_is_loaded_only_once(self):
        request = RequestFactory().get(reverse("orders:cart"))
        request.session = {CART_SESSION_KEY: {str(self.product.pk): "0.1"}}
        with self.assertNumQueries(1):
            first = get_cart_state(request)
            second = get_cart_state(request)
            self.assertEqual(cart_count(request), 1)
        self.assertIs(first, second)

    def test_request_without_session_has_an_empty_cart_without_database_access(self):
        request = RequestFactory().get(reverse("orders:cart"))
        with self.assertNumQueries(0):
            state = get_cart_state(request)
            self.assertEqual(state.items, [])
            self.assertEqual(state.count, 0)
            self.assertEqual(cart_count(request), 0)

    def test_checkout_is_read_only_and_public_order_posts_remain_unavailable(self):
        client = Client()
        self.add(client, weight="0.2")

        checkout = client.get(reverse("orders:checkout"))
        self.assertContains(checkout, self.product.name)
        self.assertContains(checkout, "Приём заявок пока не подключён")
        self.assertContains(checkout, 'name="phone"')
        self.assertContains(checkout, "disabled")

        before = (Order.objects.count(), Inquiry.objects.count(), NotificationOutbox.objects.count())
        checkout_post = client.post(reverse("orders:checkout"), {"name": "Не сохранять"})
        wholesale_post = client.post(reverse("inquiries:wholesale"), {"need": "Не сохранять"})
        after = (Order.objects.count(), Inquiry.objects.count(), NotificationOutbox.objects.count())
        self.assertEqual(checkout_post.status_code, 503)
        self.assertEqual(wholesale_post.status_code, 503)
        self.assertEqual(after, before)

    def test_cart_rejects_unsupported_method(self):
        self.assertEqual(self.client.put(reverse("orders:cart")).status_code, 405)
