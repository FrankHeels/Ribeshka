"""Public search metadata and indexing exclusions for the retail cart."""

from decimal import Decimal
from html import escape
from xml.etree import ElementTree

from django.test import Client, TestCase
from django.urls import reverse

from catalog.models import Category, Product
from orders.cart import CART_SESSION_KEY


class CartIndexingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="SEO category", slug="seo-category")
        cls.product = Product.objects.create(
            name="SEO cart fish",
            slug="seo-cart-fish",
            category=cls.category,
            price_per_kg=Decimal("120.00"),
            is_published=True,
        )

    def add_product(self, client):
        response = client.post(
            reverse("orders:cart"),
            {"action": "add", "product_id": self.product.pk, "weight": "2"},
        )
        self.assertRedirects(response, reverse("orders:cart"), fetch_redirect_response=False)
        self.assertEqual(client.session[CART_SESSION_KEY], {str(self.product.pk): "2"})
        return response

    def assert_private_and_excluded(self, response):
        self.assertEqual(response.get("X-Robots-Tag"), "noindex, nofollow")
        cache_directives = {part.strip() for part in response["Cache-Control"].split(",")}
        self.assertTrue({"private", "no-store"}.issubset(cache_directives))

    def test_get_and_head_exclude_empty_and_populated_cart_without_changing_session(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                client = Client()
                if populated:
                    self.add_product(client)
                for method in ("get", "head"):
                    with self.subTest(method=method):
                        response = getattr(client, method)(reverse("orders:cart"))
                        self.assertEqual(response.status_code, 200)
                        self.assert_private_and_excluded(response)
                        if method == "get":
                            self.assertContains(response, '<meta name="robots" content="noindex, nofollow">')
                            self.assertNotContains(response, '<meta name="robots" content="index, follow">')
                            if populated:
                                self.assertContains(response, self.product.name)
                        else:
                            self.assertEqual(response.content, b"")
                        expected_lines = {str(self.product.pk): "2"} if populated else {}
                        self.assertEqual(client.session.get(CART_SESSION_KEY, {}), expected_lines)

    def test_successful_cart_action_redirect_is_private_and_excluded(self):
        response = self.add_product(self.client)
        self.assert_private_and_excluded(response)

    def test_invalid_update_remains_private_excluded_and_preserves_line(self):
        self.add_product(self.client)
        response = self.client.post(
            reverse("orders:cart"),
            {"action": "update", "cart_line_id": str(self.product.pk), "weight": "0"},
        )
        self.assertEqual(response.status_code, 400)
        self.assert_private_and_excluded(response)
        self.assertContains(response, '<meta name="robots" content="noindex, nofollow">', status_code=400)
        self.assertContains(response, "Вес не сохранён. Исправьте значение.", status_code=400)
        self.assertEqual(self.client.session[CART_SESSION_KEY], {str(self.product.pk): "2"})

    def test_unknown_action_response_is_private_and_excluded(self):
        response = self.client.post(reverse("orders:cart"), {"action": "unknown"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), "Неизвестное действие корзины.")
        self.assert_private_and_excluded(response)
        self.assertEqual(self.client.session.get(CART_SESSION_KEY, {}), {})


class PublicSearchMetadataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(
            name="Public SEO fish",
            slug="public-seo-fish",
            seo_title="Категория & рыба",
            seo_description='Категория "рыба" <для выбора>.',
        )
        cls.products = [
            Product.objects.create(
                name=f"Public SEO fish {number:02d}",
                slug=f"public-seo-fish-{number:02d}",
                category=cls.category,
                price_per_kg=Decimal("120.00"),
                is_published=True,
                seo_title=f"Рыба {number:02d} & ассортимент",
                seo_description=f'Описание "рыбы {number:02d}" <для выбора>.',
            )
            for number in range(13)
        ]
        cls.hidden_category = Category.objects.create(name="Hidden SEO fish", slug="hidden-seo-fish")
        cls.blocked_category = Category.objects.create(name="Blocked SEO fish", slug="blocked-seo-fish")
        cls.empty_category = Category.objects.create(name="Empty SEO fish", slug="empty-seo-fish")
        cls.unavailable_products = [
            Product.objects.create(
                name=f"Unavailable SEO fish {number}",
                slug=f"unavailable-seo-fish-{number}",
                category=category,
                price_per_kg=Decimal("120.00"),
                is_published=published,
                integration_block_reason=reason,
            )
            for number, (category, published, reason) in enumerate(
                (
                    (cls.category, False, ""),
                    (cls.category, True, "archived"),
                    (cls.hidden_category, False, ""),
                    (cls.blocked_category, True, "archived"),
                )
            )
        ]

    def assert_canonical(self, response, path):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'<link rel="canonical" href="https://testserver{path}">', count=1)
        self.assertEqual(response.content.count(b'<link rel="canonical"'), 1)
        self.assertNotContains(response, '<meta name="robots" content="noindex')
        self.assertNotIn("noindex", response.get("X-Robots-Tag", "").lower())

    def test_robots_advertises_absolute_sitemap_and_allows_get_and_head(self):
        response = self.client.get(reverse("robots"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(
            response.content.decode(),
            "User-agent: *\nAllow: /\n\nSitemap: https://testserver/sitemap.xml\n",
        )
        head = self.client.head(reverse("robots"), secure=True)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head["Content-Type"], response["Content-Type"])
        self.assertEqual(head.content, b"")

    def test_sitemap_contains_only_absolute_public_pages_and_eligible_catalog(self):
        response = self.client.get(reverse("sitemap"), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml; charset=utf-8")
        root = ElementTree.fromstring(response.content)
        locations = [node.text for node in root.findall("{*}url/{*}loc")]
        expected_paths = {
            reverse(name)
            for name in ("home", "inquiries:wholesale", "content:about", "content:delivery", "content:contacts")
        }
        expected_paths.add(reverse("catalog:category", args=(self.category.slug,)))
        expected_paths.update(reverse("catalog:product", args=(product.slug,)) for product in self.products)
        self.assertEqual(set(locations), {f"https://testserver{path}" for path in expected_paths})
        self.assertEqual(len(locations), len(expected_paths))

    def test_product_metadata_is_escaped_and_tracking_does_not_change_canonical(self):
        product = self.products[0]
        path = reverse("catalog:product", args=(product.slug,))
        response = self.client.get(path, {"utm_source": "seo-test", "page": "2"}, secure=True)
        self.assert_canonical(response, path)
        self.assertContains(response, f"<title>{escape(product.seo_title)}</title>")
        self.assertContains(response, f'<meta name="description" content="{escape(product.seo_description)}">')

    def test_category_pagination_has_canonical_for_actual_page_and_crawlable_links(self):
        path = reverse("catalog:category", args=(self.category.slug,))
        for page, expected_suffix in (("1", ""), ("2", "?page=2"), ("999", "?page=2"), ("invalid", "")):
            with self.subTest(page=page):
                response = self.client.get(path, {"page": page, "utm_source": "seo-test"}, secure=True)
                self.assert_canonical(response, path + expected_suffix)
                self.assertContains(response, f"<title>{escape(self.category.seo_title)}</title>")
                self.assertContains(response, f'<meta name="description" content="{escape(self.category.seo_description)}">')
                other_page = "1" if expected_suffix else "2"
                self.assertContains(response, f'href="?page={other_page}"')
                for product in self.unavailable_products:
                    self.assertNotContains(response, reverse("catalog:product", args=(product.slug,)))

    def test_unpublished_and_blocked_catalog_urls_are_not_public_search_pages(self):
        paths = [reverse("catalog:product", args=(product.slug,)) for product in self.unavailable_products]
        paths.extend(
            reverse("catalog:category", args=(category.slug,))
            for category in (self.hidden_category, self.blocked_category, self.empty_category)
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path, secure=True)
                self.assertEqual(response.status_code, 404)
                self.assertContains(response, '<meta name="robots" content="noindex, nofollow">', status_code=404)
                self.assertNotContains(response, '<link rel="canonical"', status_code=404)
