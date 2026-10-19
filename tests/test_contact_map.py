"""Contact map address, safe browser configuration and no-API fallback."""
import json
import re
from urllib.parse import parse_qs, urlparse

from django.test import TestCase, override_settings


class ContactMapTests(TestCase):
    @override_settings(YANDEX_MAPS_API_KEY="")
    def test_missing_key_keeps_address_and_directions_without_loading_api(self):
        response = self.client.get("/contacts/")
        self.assertContains(response, "г. Гурьевск, ул. Каштановая, 1Б")
        self.assertContains(response, "Интерактивная карта сейчас недоступна")
        self.assertContains(response, "Построить маршрут")
        self.assertContains(response, "Открыть в Яндекс Картах")
        self.assertContains(response, "Для интерактивной карты включите JavaScript")
        self.assertNotContains(response, "js/contact-map.js")
        self.assertNotContains(response, 'id="yandex-maps-api-key"')

    @override_settings(YANDEX_MAPS_API_KEY='test-key</script><script>alert("x")</script>&')
    def test_browser_key_is_serialized_safely(self):
        response = self.client.get("/contacts/")
        self.assertContains(response, "js/contact-map.js")
        html = response.content.decode()
        data = re.search(r'<script id="yandex-maps-api-key" type="application/json">(.*?)</script>', html).group(1)
        self.assertEqual(json.loads(data), 'test-key</script><script>alert("x")</script>&')
        self.assertNotIn('<script>alert("x")</script>', html)

    @override_settings(YANDEX_MAPS_API_KEY="synthetic-map-key")
    def test_marker_and_route_point_to_the_requested_building(self):
        response = self.client.get("/contacts/")
        html = response.content.decode()
        data = re.search(r'<script id="contact-map-location" type="application/json">(.*?)</script>', html).group(1)
        location = json.loads(data)
        self.assertEqual(location["address"], "г. Гурьевск, ул. Каштановая, 1Б")
        self.assertEqual(location["coordinates"], [20.608287, 54.764501])
        self.assertEqual(parse_qs(urlparse(location["map_url"]).query)["ll"], ["20.608287,54.764501"])
        self.assertEqual(parse_qs(urlparse(location["directions_url"]).query)["rtext"], ["~54.764501,20.608287"])
        self.assertContains(response, 'aria-label="Приблизить карту"')
        self.assertContains(response, 'aria-label="Отдалить карту"')
        self.assertContains(response, 'aria-label="Вернуться к метке"')

    @override_settings(YANDEX_MAPS_API_KEY="synthetic-map-key")
    def test_other_pages_do_not_load_contact_map(self):
        for path in ("/", "/about/", "/delivery/", "/privacy/", "/cart/", "/wholesale/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "js/contact-map.js")
                self.assertNotContains(response, "data-contact-map")
                if path != "/delivery/":
                    self.assertNotContains(response, "synthetic-map-key")
