"""Prove that flattening existing categories retains records and product links."""

from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from catalog.models import Category, Product


class FlatCategoryMigrationTests(TransactionTestCase):
    migrate_from = ("catalog", "0001_initial")
    migrate_to = ("catalog", "0002_flat_categories")

    def test_existing_nested_categories_become_independent_without_losing_products(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        try:
            old_apps = executor.loader.project_state([self.migrate_from]).apps
            OldCategory = old_apps.get_model("catalog", "Category")
            OldProduct = old_apps.get_model("catalog", "Product")
            root = OldCategory.objects.create(name="Рыба", slug="fish", display_order=4)
            child = OldCategory.objects.create(
                name="Копчёная рыба",
                slug="smoked",
                parent_id=root.pk,
                description="Описание",
                image="catalog/categories/smoked.png",
                display_order=2,
            )
            product = OldProduct.objects.create(
                name="Товар",
                slug="product",
                category_id=child.pk,
                price_per_kg=Decimal("100.00"),
            )

            MigrationExecutor(connection).migrate([self.migrate_to])

            self.assertEqual(Category.objects.count(), 2)
            migrated = Category.objects.get(pk=child.pk)
            self.assertEqual(
                (migrated.name, migrated.slug, migrated.description, migrated.image.name, migrated.display_order),
                ("Копчёная рыба", "smoked", "Описание", "catalog/categories/smoked.png", 2),
            )
            self.assertEqual(Product.objects.get(pk=product.pk).category_id, migrated.pk)
            with connection.cursor() as cursor:
                columns = {column.name for column in connection.introspection.get_table_description(cursor, "catalog_category")}
            self.assertNotIn("parent_id", columns)
        finally:
            MigrationExecutor(connection).migrate([self.migrate_to])
