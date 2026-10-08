"""Delivery-only API configuration and progressive-enhancement contract."""
import json
import re

from django.test import TestCase, override_settings

from content.models import ContentPage, ContentSection


class DeliveryMapTests(TestCase):
    @override_settings(YANDEX_MAPS_API_KEY="")
    def test_missing_key_keeps_fallback_and_link_without_loading_api(self):
        response = self.client.get("/delivery/")
        self.assertContains(response, "Интерактивная карта сейчас недоступна")
        self.assertContains(response, "Открыть в Картах")
        self.assertContains(response, "Для интерактивной карты включите JavaScript")
        self.assertNotContains(response, "js/delivery-map.js")
        self.assertNotContains(response, 'id="yandex-maps-api-key"')

    @override_settings(YANDEX_MAPS_API_KEY='test-key</script><script>alert("x")</script>&')
    def test_browser_key_is_serialized_safely(self):
        response = self.client.get("/delivery/")
        self.assertContains(response, "js/delivery-map.js")
        html = response.content.decode()
        data = re.search(r'<script id="yandex-maps-api-key" type="application/json">(.*?)</script>', html).group(1)
        self.assertEqual(json.loads(data), 'test-key</script><script>alert("x")</script>&')
        self.assertNotIn('<script>alert("x")</script>', html)

    @override_settings(YANDEX_MAPS_API_KEY="synthetic-map-key")
    def test_other_pages_do_not_receive_map_or_key(self):
        for path in ("/", "/about/", "/contacts/", "/privacy/", "/cart/", "/wholesale/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "synthetic-map-key")
                self.assertNotContains(response, "js/delivery-map.js")
                self.assertNotContains(response, "data-delivery-map")

    def test_map_follows_all_delivery_materials(self):
        page = ContentPage.objects.get(key=ContentPage.Key.DELIVERY)
        ContentSection.objects.create(page=page, key="delivery_test", title="Условия самовывоза", body="Последний материал доставки", display_order=999)
        html = self.client.get("/delivery/").content.decode()
        self.assertLess(html.index("Последний материал доставки"), html.index("data-delivery-map"))
