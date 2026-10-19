# Запуск этого файла: `.venv/bin/python manage.py test tests.test_delivery_map --settings=fishsite.settings_test --noinput`.
# Django test runner применяет миграции к отдельной БД `test_fishsite`, запускает тесты и удаляет её.
# `settings_test` использует изолированный PostgreSQL на 127.0.0.1:15432; обычная БД и её настройки не используются.
# Каждый метод наследует Django `TestCase`: запросы выполняются встроенным тестовым клиентом, а изменения БД откатываются.
# Ключ карты задаётся локально через `override_settings`; ответы проверяются по HTML и assertions, без браузера и внешнего API.
"""Delivery map configuration and progressive-enhancement contract."""
import json
import re

from django.test import TestCase, override_settings

from content.models import ContentPage


class DeliveryMapTests(TestCase):
    # Без API-ключа страница доставки сохраняет резервное сообщение, ссылку на Карты и noscript-подсказку,
    # но не подключает JS карты и не выводит JSON-элемент с ключом.
    @override_settings(YANDEX_MAPS_API_KEY="")
    def test_missing_key_keeps_fallback_and_link_without_loading_api(self):
        response = self.client.get("/delivery/")
        self.assertContains(response, "Интерактивная карта сейчас недоступна")
        self.assertContains(response, "Открыть в Картах")
        self.assertContains(response, "Для интерактивной карты включите JavaScript")
        self.assertNotContains(response, "js/delivery-map.js")
        self.assertNotContains(response, 'id="yandex-maps-api-key"')

    # При заданном синтетическом ключе подключается JS карты, ключ безопасно сериализуется как JSON,
    # а его содержимое с закрывающим script-тегом и HTML не превращается в исполняемый script.
    @override_settings(YANDEX_MAPS_API_KEY='test-key</script><script>alert("x")</script>&')
    def test_browser_key_is_serialized_safely(self):
        response = self.client.get("/delivery/")
        self.assertContains(response, "js/delivery-map.js")
        html = response.content.decode()
        data = re.search(r'<script id="yandex-maps-api-key" type="application/json">(.*?)</script>', html).group(1)
        self.assertEqual(json.loads(data), 'test-key</script><script>alert("x")</script>&')
        self.assertNotIn('<script>alert("x")</script>', html)

    # На главной, страницах «О компании», политики, корзины и оптовой заявки (каждый путь — subTest)
    # ответ имеет статус 200 и не содержит синтетический ключ, JS карты или маркер блока карты.
    @override_settings(YANDEX_MAPS_API_KEY="synthetic-map-key")
    def test_other_pages_do_not_receive_map_or_key(self):
        for path in ("/", "/about/", "/privacy/", "/cart/", "/wholesale/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "synthetic-map-key")
                self.assertNotContains(response, "js/delivery-map.js")
                self.assertNotContains(response, "data-delivery-map")

    # Карта следует за единым редактируемым текстом страницы доставки.
    def test_map_follows_all_delivery_materials(self):
        page = ContentPage.objects.get(key=ContentPage.Key.DELIVERY)
        page.body = "Последний материал доставки"
        page.save(update_fields=["body"])
        html = self.client.get("/delivery/").content.decode()
        self.assertLess(html.index("Последний материал доставки"), html.index("data-delivery-map"))
