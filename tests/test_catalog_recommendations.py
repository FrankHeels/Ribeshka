"""Homepage recommendations are the sole public selection of sales hits."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from catalog.admin import ProductAdminForm
from catalog.models import Category, Product
from content.models import HomepageRecommendation


class CatalogRecommendationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name="Рыба", slug="recommendation-fish")
        cls.other_category = Category.objects.create(name="Икра", slug="recommendation-caviar")

        def product(name, slug, category=None, **overrides):
            return Product.objects.create(
                name=name, slug=slug, category=category or cls.category,
                price_per_kg=Decimal("100.00"), is_published=overrides.pop("is_published", True),
                **overrides,
            )

        cls.legacy_hit = product("Старая отметка", "legacy-hit", is_popular=True, display_order=1)
        cls.selected = product("Выбранная рыба", "selected-fish", display_order=2)
        cls.second = product("Выбранная икра", "selected-caviar", cls.other_category)
        cls.hidden = product("Черновик", "hidden-fish", is_published=False)
        cls.blocked = product("Заблокированная рыба", "blocked-fish", integration_block_reason="source_deleted")
        HomepageRecommendation.objects.update_or_create(slot=1, defaults={"product": cls.selected})
        HomepageRecommendation.objects.update_or_create(slot=2, defaults={"product": cls.second})

    def test_legacy_popular_parameter_keeps_all_published_category_products(self):
        url = reverse("catalog:category", args=(self.category.slug,))
        for query in ({}, {"popular": "1"}):
            with self.subTest(query=query):
                response = self.client.get(url, query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(list(response.context["products"]), [self.legacy_hit, self.selected])
                self.assertNotContains(response, "Фильтр товаров категории")
                self.assertNotContains(response, "Хиты продаж")
                self.assertNotContains(response, "product-popular-badge")

    def test_legacy_parameter_preserves_pagination(self):
        for index in range(11):
            Product.objects.create(
                name=f"Рыба {index:02d}", slug=f"pagination-fish-{index}", category=self.category,
                price_per_kg=Decimal("100.00"), is_published=True, display_order=10 + index,
            )
        url = reverse("catalog:category", args=(self.category.slug,))
        response = self.client.get(url, {"popular": "1", "page": "2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 13)
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual([p.slug for p in response.context["products"]], ["pagination-fish-10"])

    def test_home_hits_follow_explicit_slots_and_ignore_legacy_flag(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["featured_products"], [self.selected, self.second])
        self.assertContains(response, '<span class="product-popular-badge">Хит</span>', count=2, html=True)

    def test_unavailable_recommendations_have_no_automatic_replacements(self):
        for changes in ({"is_published": False}, {"integration_block_reason": "source_deleted"}):
            with self.subTest(changes=changes):
                Product.objects.filter(pk__in=(self.selected.pk, self.second.pk)).update(
                    is_published=True, integration_block_reason="",
                )
                Product.objects.filter(pk=self.selected.pk).update(**changes)
                response = self.client.get(reverse("home"))
                self.assertEqual(response.context["featured_products"], [self.second])
                self.assertContains(response, '<span class="product-popular-badge">Хит</span>', count=1, html=True)

    def test_product_form_does_not_offer_a_second_selection_of_hits(self):
        form = ProductAdminForm(instance=self.selected)
        self.assertNotIn("is_popular", form.fields)
