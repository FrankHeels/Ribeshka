# Запуск только этого файла:
# .venv/bin/python manage.py test tests.test_legal_pages --settings=fishsite.settings_test --noinput
# Django TestCase использует изолированную тестовую БД test_fishsite на
# 127.0.0.1:15432 под ролью fishsite_tester; runner создаёт её перед тестами
# и удаляет после. SimpleTestCase БД не использует. Запросы проверяются через
# Django test client, а шаблоны форм — рендерингом render_to_string с тестовым
# контекстом; HTML и XML проверяются assertions unittest/Django.
from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from django.template.loader import render_to_string


class LegalPageTests(TestCase):
    # Для /privacy/, /personal-data-consent/ и /yandex-metrica-consent/
    # проверить HTTP 200, заголовок и исходный текст документа, пометку и
    # незаполненное поле черновика, noindex, отсутствие ссылок на .md и ровно
    # один h1; для каждого маршрута выполняется отдельный subTest.
    def test_each_public_document_renders_its_source_and_stays_a_draft(self):
        pages = (
            (
                "/privacy/",
                "Политика обработки персональных данных",
                "Обновление документа не расширяет ранее полученное согласие автоматически.",
            ),
            (
                "/personal-data-consent/",
                "Согласие на обработку персональных данных",
                "Для аналитики предусмотрено",
            ),
            (
                "/yandex-metrica-consent/",
                "Согласие на обработку персональных данных с помощью Яндекс Метрики",
                "Это дополнительная возможность управления браузером",
            ),
        )
        for path, title, source_text in pages:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn(f"<h1>{title}</h1>", html)
                self.assertIn("Проект документа", html)
                self.assertIn("{{OPERATOR_FULL_NAME}}", html)
                self.assertIn(source_text, html)
                self.assertIn('<meta name="robots" content="noindex, nofollow">', html)
                self.assertNotRegex(html, r'href="[^"]+\.md(?:#[^"]*)?"')
                self.assertEqual(html.count("<h1"), 1)

    # На /privacy/ проверить HTTP 200, что ссылки из Markdown ведут на
    # маршруты согласий, заголовки таблиц имеют scope="col", а таблица
    # обёрнута контейнером legal-table-wrap.
    def test_legal_source_links_resolve_to_site_routes_and_tables_are_scoped(self):
        response = self.client.get("/privacy/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('href="/personal-data-consent/"', html)
        self.assertIn('href="/yandex-metrica-consent/"', html)
        self.assertIn('<th scope="col">', html)
        self.assertIn('class="legal-table-wrap"', html)

    # На /privacy/ проверить HTTP 200 и наличие в футере ссылок на все три
    # документа: политику, согласие на обработку данных и согласие для Метрики.
    def test_footer_links_all_three_documents(self):
        response = self.client.get("/privacy/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for path in ("/privacy/", "/personal-data-consent/", "/yandex-metrica-consent/"):
            with self.subTest(path=path):
                self.assertIn(f'href="{path}"', html)

    # Запросить /sitemap.xml и проверить HTTP 200; каждый из трёх черновых
    # документов (/privacy/, /personal-data-consent/,
    # /yandex-metrica-consent/) должен отсутствовать в ответе отдельного
    # subTest.
    def test_draft_legal_pages_are_not_listed_in_sitemap(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        for path in ("/privacy/", "/personal-data-consent/", "/yandex-metrica-consent/"):
            with self.subTest(path=path):
                self.assertNotContains(response, path)


class LegalConsentFormLinksTests(SimpleTestCase):
    # Отрендерить корзину с синтетической розничной позицией и оптовую форму
    # с включёнными формами; для обоих шаблонов проверить ссылку на версию
    # согласия и политику, а также что чекбокс согласия не отмечен. Отдельный
    # subTest различает retail и wholesale. Для обоих HTML проверить, что
    # ссылка на Метрику отсутствует внутри main, а ссылка на политику — вне
    # label чекбокса.
    def test_active_retail_and_wholesale_forms_link_both_documents_without_prechecking(self):
        product = {"id": 1, "slug": "sample", "name": "Тестовый товар", "photos": []}
        retail = render_to_string(
            "orders/cart.html",
            {
                "cart_items": [
                    {
                        "key": "line1",
                        "product": product,
                        "price_per_kg": Decimal("100.00"),
                        "weight": Decimal("2"),
                        "weight_display": "2",
                        "line_total": Decimal("200.00"),
                        "weight_errors": [],
                    }
                ],
                "cart_total": Decimal("200.00"),
                "cart_total_is_valid": True,
                "forms_enabled": True,
                "field_errors": {},
                "values": {},
                "submission": {
                    "submission_key": "key",
                    "submission_token": "token",
                    "consent_url": "/personal-data-consent/revisions/sha256:abc/",
                },
            },
        )
        wholesale = render_to_string(
            "inquiries/wholesale.html",
            {
                "forms_enabled": True,
                "field_errors": {},
                "values": {},
                "submission": {"consent_url": "/personal-data-consent/revisions/sha256:abc/"},
            },
        )

        for html in (retail, wholesale):
            with self.subTest(template="retail" if html is retail else "wholesale"):
                self.assertIn('href="/personal-data-consent/revisions/sha256:abc/"', html)
                self.assertIn('href="/privacy/"', html)
                checkbox = html.split('id="id_consent"', 1)[1].split(">", 1)[0]
                self.assertNotIn("checked", checkbox)
        for html in (retail, wholesale):
            main = html.split("<main", 1)[1].split("</main>", 1)[0]
            self.assertNotIn('href="/yandex-metrica-consent/"', main)
            label = html.split('id="id_consent"', 1)[1].split("</label>", 1)[0]
            self.assertNotIn('href="/privacy/"', label)
