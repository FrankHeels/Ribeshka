"""PostgreSQL-backed behavior checks for the anonymous session cart."""

# Запуск: `.venv/bin/python manage.py test tests.test_session_cart --settings=fishsite.settings_test --noinput`.
# Django TestCase создаёт тестовую БД `test_fishsite` в изолированном PostgreSQL
# профиле `fishsite.settings_test`, применяет миграции и откатывает транзакции
# каждого теста. setUpTestData создаёт общие тестовые Category и Product;
# отдельные товары создаются методами теста. Django Client проверяет реальные
# URL, шаблоны и сессии; отдельный Client включает CSRF-проверку. RequestFactory
# используется для прямых проверок состояния корзины и числа ORM-запросов.
# Внешние сервисы и SMTP этот файл не использует.

from decimal import Decimal

from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from catalog.models import Category, Product
from orders.cart import CART_SESSION_KEY, cart_count, get_cart_state


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

    def add(self, client, product=None, weight="1", **extra):
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

    # Два добавления одного товара (2 кг и 3 кг) дают 5 кг; проверяются PRG-
    # перенаправления, сохранение постороннего ключа сессии и цена/счётчик в HTML.
    def test_add_redirects_persists_and_readding_accumulates_weight(self):
        client = Client()
        unrelated = client.session
        unrelated["unrelated"] = "preserve me"
        unrelated.save()

        response = self.add(client, weight="2")
        self.assertRedirects(response, reverse("orders:cart"), fetch_redirect_response=False)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "2"})

        response = self.add(client, weight="3")
        self.assertRedirects(response, reverse("orders:cart"), fetch_redirect_response=False)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "5"})
        self.assertEqual(client.session["unrelated"], "preserve me")

        cart = client.get(reverse("orders:cart"))
        self.assertContains(cart, 'value="5"')
        self.assertContains(cart, "500,00 ₽")
        self.assertContains(cart, 'aria-label="Корзина, товаров: 1"')

    # После добавления 3 кг страница товара всё равно предлагает добавить 1 кг,
    # сохраняет подписи «Добавить» и не показывает действие обновления корзины.
    def test_product_page_starts_at_one_and_keeps_add_copy_when_item_is_in_cart(self):
        client = Client()
        response = self.add(client, weight="3")
        self.assertEqual(response.status_code, 302)

        page = client.get(reverse("catalog:product", args=(self.product.slug,)))
        self.assertContains(page, 'id="product-weight"')
        self.assertContains(page, 'value="1"')
        self.assertContains(page, "<legend>Добавить в корзину</legend>", html=True)
        self.assertContains(
            page,
            '<button class="button button-primary" type="submit" name="action" value="add">Добавить в корзину</button>',
            html=True,
        )
        self.assertNotContains(page, "Обновить вес в корзине")

    # Пустая корзина не меняет начальный ввод на карточке: это целый 1 кг с диапазоном 1–99999.
    def test_product_page_defaults_to_one_kilogram(self):
        page = Client().get(reverse("catalog:product", args=(self.product.slug,)))
        self.assertContains(page, 'inputmode="numeric" min="1" max="99999" step="1" value="1"')

    # Добавление 3 кг в первую сессию не меняет вторую: в ней пустая корзина.
    def test_cart_isolated_between_sessions(self):
        first, second = Client(), Client()
        self.add(first, weight="3")

        self.assertEqual(self.cart_lines(first), {str(self.product.pk): "3"})
        self.assertEqual(self.cart_lines(second), {})
        self.assertContains(second.get(reverse("orders:cart")), "Корзина пуста")

    # При включённой CSRF-проверке POST без токена получает 403; тот же add с
    # токеном из GET карточки проходит и сохраняет 1 кг в сессии.
    def test_csrf_is_required_for_cart_changes(self):
        client = Client(enforce_csrf_checks=True)
        product_url = reverse("catalog:product", args=(self.product.slug,))
        page = client.get(product_url)
        token = page.cookies["csrftoken"].value

        denied = client.post(
            reverse("orders:cart"),
            {"action": "add", "product_id": str(self.product.pk), "weight": "1"},
        )
        self.assertEqual(denied.status_code, 403)

        accepted = client.post(
            reverse("orders:cart"),
            {
                "action": "add",
                "product_id": str(self.product.pk),
                "weight": "1",
                "csrfmiddlewaretoken": token,
            },
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "1"})

    # Две разные позиции по 1 кг и 10,05 ₽/кг дают общий итог 20,10 ₽ и две
    # доступные суммы строк по 10,05 ₽.
    def test_line_totals_are_added(self):
        second_product = self.make_product()
        first_product = self.make_product(name="Первая тестовая рыба", slug="session-cart-round-a")
        client = Client()
        self.add(client, first_product, "1")
        self.add(client, second_product, "1")

        self.assertEqual(
            self.cart_lines(client),
            {str(first_product.pk): "1", str(second_product.pk): "1"},
        )
        response = client.get(reverse("orders:cart"))
        self.assertContains(response, "20,10 ₽")
        self.assertContains(response, 'data-cart-line-total-value data-cart-line-total-available="true">10,05 ₽', count=2)

    # Поддельные price_per_kg, line_total и total не влияют на расчёт: 2 кг
    # базового товара показываются по серверной цене 100 ₽/кг и сумме 200 ₽.
    def test_client_price_and_total_fields_are_ignored(self):
        client = Client()
        response = self.add(
            client,
            weight="2",
            price_per_kg="0.01",
            line_total="0.00",
            total="0.00",
        )
        self.assertEqual(response.status_code, 302)
        page = client.get(reverse("orders:cart"))
        self.assertContains(page, "100,00 ₽")
        self.assertContains(page, "200,00 ₽")

    # Для уже сохранённых 2 кг после изменения цены в БД корзина показывает
    # новую цену 125,50 ₽/кг и пересчитанные 251 ₽.
    def test_current_price_is_reloaded_for_existing_session_line(self):
        client = Client()
        self.add(client, weight="2")
        Product.objects.filter(pk=self.product.pk).update(price_per_kg=Decimal("125.50"))

        page = client.get(reverse("orders:cart"))
        self.assertContains(page, "125,50 ₽")
        self.assertContains(page, "251,00 ₽")

    # Для товара с устаревшими минимумом 10,5 и шагом 3,25 проверяются три add:
    # входы 1, 2, 3 кг последовательно дают веса строки 1, 3 и 6 кг.
    def test_integer_weights_ignore_legacy_product_minimum_and_step(self):
        product = self.make_product(
            name="Рыба с legacy минимумом и шагом",
            slug="session-cart-offset-step",
            minimum="10.5",
            step="3.25",
        )
        client = Client()

        for weight, expected_weight in (("1", "1"), ("2", "3"), ("3", "6")):
            with self.subTest(weight=weight, expected_weight=expected_weight):
                response = self.add(client, product, weight)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.cart_lines(client)[str(product.pk)], expected_weight)

    # Add с нулевым весом возвращает 400, сохраняет введённый «0» в поле,
    # связывает сводку ошибки с полем и не добавляет строку в сессию.
    def test_invalid_add_keeps_raw_input_and_links_summary_to_weight_control(self):
        client = Client()
        response = self.add(client, weight="0")

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'id="product-weight"', status_code=400)
        self.assertContains(response, 'value="0"', status_code=400)
        self.assertContains(response, 'href="#product-weight"', status_code=400)
        self.assertContains(response, "Минимальный вес — 1 кг.", status_code=400)
        self.assertEqual(self.cart_lines(client), {})

    # После скрытия ранее добавленного товара ошибочный add другой позиции
    # (0 кг) всё равно сообщает об удалении недоступной строки, не раскрывает
    # её название и удаляет её ID из сессии.
    def test_invalid_add_still_reports_other_unavailable_lines_removed_from_cart(self):
        client = Client()
        hidden = self.make_product(
            name="Недоступная тестовая рыба",
            slug="session-cart-now-hidden",
        )
        self.add(client, hidden, "1")
        Product.objects.filter(pk=hidden.pk).update(is_published=False)

        response = self.add(client, self.product, "0")

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Некоторые позиции больше недоступны или были удалены из корзины", status_code=400)
        self.assertNotContains(response, hidden.name, status_code=400)
        self.assertNotIn(str(hidden.pk), self.cart_lines(client))

    # Для одного товара отвергаются -1, 0, 0.5, 1.5, 1.0000, 100000, NaN,
    # ±Infinity и текст; после каждого ответа 400 строки нет. Граничные 99999
    # кг принимаются и сохраняются.
    def test_fractional_out_of_range_and_non_finite_weights_are_rejected(self):
        client = Client()
        product = self.make_product(
            name="Целый вес",
            slug="session-cart-precision",
        )

        for weight in ("-1", "0", "0.5", "1.5", "1.0000", "100000", "NaN", "Infinity", "-Infinity", "one kg"):
            with self.subTest(weight=weight):
                response = self.add(client, product, weight)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(str(product.pk), self.cart_lines(client))

        accepted = self.add(client, product, "99999")
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.cart_lines(client)[str(product.pk)], "99999")

    # При существующих 99999 кг товара и другой строке 7 кг добавление ещё 1 кг
    # отклоняется с 400 и сообщением о пределе; весь исходный состав сохранён.
    def test_additive_weight_overflow_preserves_the_existing_cart(self):
        client = Client()
        other_product = self.make_product(name="Другая тестовая рыба", slug="session-cart-overflow-other")
        original_lines = {str(self.product.pk): "99999", str(other_product.pk): "7"}
        self.set_cart_lines(client, original_lines)

        response = self.add(client, weight="1")

        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "Общий вес товара не может превышать 99 999 кг.",
            status_code=400,
        )
        self.assertEqual(self.cart_lines(client), original_lines)

    # Научная запись «1e1» принимается как 10 кг, нормализуется в сессии до «10»
    # и отображается обычным целым числом.
    def test_scientific_decimal_input_is_saved_as_fixed_point(self):
        client = Client()
        response = self.add(client, weight="1e1")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "10"})
        self.assertContains(client.get(reverse("orders:cart")), 'value="10"')

    # После сохранения 2 кг обновления значениями 0, -1 и 1.5 каждое дают 400,
    # показывают исходный ввод и оставляют в сессии прежние 2 кг.
    def test_invalid_update_preserves_saved_weight_and_shows_raw_input(self):
        client = Client()
        self.add(client, weight="2")
        for weight in ("0", "-1", "1.5"):
            with self.subTest(weight=weight):
                response = client.post(
                    reverse("orders:cart"),
                    {"action": "update", "cart_line_id": str(self.product.pk), "weight": weight},
                )

                self.assertEqual(response.status_code, 400)
                self.assertContains(response, f'value="{weight}"', status_code=400)
                self.assertEqual(self.cart_lines(client), {str(self.product.pk): "2"})

    # Два add (2 и 3 кг) накапливают 5 кг, затем update на 4 кг заменяет общий
    # вес, а отображаемая сумма равна 400 ₽.
    def test_cart_update_replaces_the_accumulated_weight_absolutely(self):
        client = Client()
        self.add(client, weight="2")
        self.add(client, weight="3")
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "5"})

        updated = client.post(
            reverse("orders:cart"),
            {"action": "update", "cart_line_id": str(self.product.pk), "weight": "4"},
        )

        self.assertEqual(updated.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "4"})
        self.assertContains(client.get(reverse("orders:cart")), "400,00 ₽")

    # Update корректного ID, отсутствующего в корзине, возвращает 404 и не
    # создаёт строку в сессии.
    def test_update_rejects_a_product_not_in_the_cart(self):
        client = Client()
        response = client.post(
            reverse("orders:cart"),
            {"action": "update", "cart_line_id": str(self.product.pk), "weight": "2"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.cart_lines(client), {})

    # Удаление строки с нечисловым полем веса проходит дважды с 302, удаляет
    # позицию и оставляет пустую корзину; кнопка в HTML помечена formnovalidate.
    def test_remove_works_with_invalid_weight_and_repeated_remove_is_safe(self):
        client = Client()
        self.add(client, weight="2")
        page = client.get(reverse("orders:cart"))
        self.assertContains(page, f'value="remove" form="cart-item-{self.product.pk}" formnovalidate')

        data = {"action": "remove", "cart_line_id": str(self.product.pk), "weight": "not-a-number"}
        removed = client.post(reverse("orders:cart"), data)
        repeated = client.post(reverse("orders:cart"), data)
        self.assertEqual(removed.status_code, 302)
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(self.cart_lines(client), {})
        self.assertContains(client.get(reverse("orders:cart")), "Корзина пуста")

    # Проверяются оба случая для ранее добавленной строки: снятие публикации и
    # физическое удаление. Корзина очищается, показывается пустое состояние, а
    # для скрытого товара не раскрываются название, slug или секретная цена.
    def test_hidden_or_deleted_products_are_pruned_without_revealing_details(self):
        client = Client()
        secret_price = "9876.54"
        Product.objects.filter(pk=self.product.pk).update(price_per_kg=Decimal(secret_price))
        self.add(client, weight="1")
        Product.objects.filter(pk=self.product.pk).update(is_published=False)

        hidden_cart = client.get(reverse("orders:cart"))
        self.assertContains(hidden_cart, "Корзина пуста")
        self.assertNotContains(hidden_cart, self.product.name)
        self.assertNotContains(hidden_cart, self.product.slug)
        self.assertNotContains(hidden_cart, secret_price)
        self.assertEqual(self.cart_lines(client), {})

        Product.objects.filter(pk=self.product.pk).update(is_published=True)
        self.add(client, weight="1")
        Product.objects.filter(pk=self.product.pk).delete()
        deleted_cart = client.get(reverse("orders:cart"))
        self.assertContains(deleted_cart, "Корзина пуста")
        self.assertNotContains(deleted_cart, self.product.name)
        self.assertEqual(self.cart_lines(client), {})

    # Сохранённые 2 кг остаются допустимыми после изменения legacy-минимума и
    # шага товара на 10,5/3,25; вес и сумма 200 ₽ остаются в корзине.
    def test_legacy_minimum_and_step_changes_do_not_invalidate_integer_weight(self):
        client = Client()
        self.add(client, weight="2")
        Product.objects.filter(pk=self.product.pk).update(
            minimum_weight=Decimal("10.5"),
            weight_step=Decimal("3.25"),
        )

        page = client.get(reverse("orders:cart"))
        self.assertContains(page, 'value="2"')
        self.assertContains(page, "200,00 ₽")
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "2"})

    # Устаревшие 1.5 кг остаются без округления, с ошибкой и скрытыми суммами;
    # add 2 кг отклонён без изменения сессии, карточка по-прежнему предлагает
    # add 1 кг, а update до 3 кг исправляет строку и сумму.
    def test_legacy_fractional_session_cannot_be_added_to_and_can_be_fixed(self):
        client = Client()
        self.set_cart_lines(client, {str(self.product.pk): "1.5"})

        page = client.get(reverse("orders:cart"))
        self.assertContains(page, 'value="1.5"')
        self.assertContains(page, "Укажите целое количество килограммов от 1 до 99 999.")
        self.assertContains(page, 'data-cart-total-available="false"')
        self.assertContains(page, 'data-cart-total-value aria-hidden="true">—</strong>')
        self.assertContains(page, 'data-cart-line-total-available="false" aria-hidden="true">—</span>')
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "1.5"})

        rejected_add = self.add(client, weight="2")
        self.assertEqual(rejected_add.status_code, 400)
        self.assertContains(
            rejected_add,
            "Исправьте или удалите позицию в корзине перед добавлением.",
            status_code=400,
        )
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "1.5"})

        product_page = client.get(reverse("catalog:product", args=(self.product.slug,)))
        self.assertContains(product_page, 'value="1"')
        self.assertContains(product_page, "Добавить в корзину")
        self.assertNotContains(product_page, "Обновить вес в корзине")

        fixed = client.post(
            reverse("orders:cart"),
            {"action": "update", "cart_line_id": str(self.product.pk), "weight": "3"},
        )
        self.assertEqual(fixed.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "3"})
        self.assertContains(client.get(reverse("orders:cart")), "300,00 ₽")

    # Старый целый формат «2.000» допускает add 3 кг, арифметически даёт 5 кг
    # и нормализуется в сессии и HTML до «5».
    def test_legacy_integer_decimal_session_adds_arithmetically_and_normalizes(self):
        client = Client()
        self.set_cart_lines(client, {str(self.product.pk): "2.000"})

        response = self.add(client, weight="3")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cart_lines(client), {str(self.product.pk): "5"})
        page = client.get(reverse("orders:cart"))
        self.assertContains(page, 'value="5"')
        self.assertContains(page, "500,00 ₽")

    # Старая дробная строка 1.5 кг удаляется POST-ом remove даже при
    # невалидном тексте веса; ответ перенаправляет, сессия пуста.
    def test_legacy_fractional_session_can_be_removed_without_valid_weight(self):
        client = Client()
        self.set_cart_lines(client, {str(self.product.pk): "1.5"})

        removed = client.post(
            reverse("orders:cart"),
            {"action": "remove", "cart_line_id": str(self.product.pk), "weight": "not-a-number"},
        )
        self.assertEqual(removed.status_code, 302)
        self.assertEqual(self.cart_lines(client), {})

    # Некорректные ID (переполнение и ведущий ноль), NaN и ID отсутствующего
    # товара удаляются при GET без 500; ответ 200 показывает пустую корзину.
    def test_malformed_session_data_is_discarded_without_server_error(self):
        client = Client()
        self.set_cart_lines(
            client,
            {
                "99999999999999999999": "1",
                "01": "1",
                str(self.product.pk): "NaN",
                str(self.product.pk + 100): "1",
            },
        )

        page = client.get(reverse("orders:cart"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Корзина пуста")
        self.assertEqual(self.cart_lines(client), {})

    # Сессионное значение-корзина неверной формы (список) исправляется на пустой
    # словарь при GET; сервер отвечает 200 без ошибки.
    def test_non_dict_session_cart_is_repaired_without_server_error(self):
        client = Client()
        session = client.session
        session[CART_SESSION_KEY] = ["bad", "shape"]
        session.save()

        page = client.get(reverse("orders:cart"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Корзина пуста")
        self.assertEqual(self.cart_lines(client), {})

    # Add с пустым, неканоничным, переполненным и текстовым ID даёт 404; add
    # неопубликованного товара также даёт 404, неизвестное действие — 400.
    def test_malformed_add_ids_hidden_products_and_unknown_actions_fail_safely(self):
        client = Client()
        for product_id in ("", "01", "99999999999999999999", "not-an-id"):
            with self.subTest(product_id=product_id):
                response = client.post(
                    reverse("orders:cart"),
                    {"action": "add", "product_id": product_id, "weight": "1"},
                )
                self.assertEqual(response.status_code, 404)

        unpublished = self.make_product(
            name="Скрытый товар",
            slug="session-cart-unpublished",
            published=False,
        )
        response = self.add(client, unpublished, "1")
        self.assertEqual(response.status_code, 404)

        unknown = client.post(reverse("orders:cart"), {"action": "other"})
        self.assertEqual(unknown.status_code, 400)

    # Для RequestFactory-запроса с одной опубликованной строкой повторный вызов
    # get_cart_state возвращает тот же объект; два чтения состояния и cart_count
    # вместе выполняют ровно один запрос к БД.
    def test_request_cart_state_is_loaded_only_once(self):
        request = RequestFactory().get(reverse("orders:cart"))
        request.session = {CART_SESSION_KEY: {str(self.product.pk): "1"}}
        with self.assertNumQueries(1):
            first = get_cart_state(request)
            second = get_cart_state(request)
            self.assertEqual(cart_count(request), 1)
        self.assertIs(first, second)

    # У запроса без session состояние пустое (items=[], count=0), cart_count=0;
    # все чтения выполняются без запросов к БД.
    def test_request_without_session_has_an_empty_cart_without_database_access(self):
        request = RequestFactory().get(reverse("orders:cart"))
        with self.assertNumQueries(0):
            state = get_cart_state(request)
            self.assertEqual(state.items, [])
            self.assertEqual(state.count, 0)
            self.assertEqual(cart_count(request), 0)

    # GET checkout при строке 2 кг перенаправляет на /cart/#request; корзина
    # содержит включённую форму заявки с контактными и submission-полями, без
    # сообщения о выключенном приёме или disabled-контролов.
    def test_checkout_redirects_to_enabled_cart_request_form(self):
        client = Client()
        self.add(client, weight="2")

        checkout = client.get(reverse("orders:checkout"))
        self.assertEqual(checkout.status_code, 302)
        self.assertEqual(checkout.url, reverse("orders:cart") + "#request")

        cart = client.get(reverse("orders:cart"))
        self.assertContains(cart, self.product.name)
        self.assertContains(cart, 'id="retail-request-form"')
        self.assertContains(cart, 'name="phone"')
        self.assertContains(cart, 'name="submission_key"')
        self.assertContains(cart, 'name="submission_token"')
        self.assertNotContains(cart, "Приём заявок пока не подключён")
        self.assertNotContains(cart, "disabled")

    # PUT на URL корзины отклоняется ограничителем HTTP-методов со статусом 405.
    def test_cart_rejects_unsupported_method(self):
        self.assertEqual(self.client.put(reverse("orders:cart")).status_code, 405)
