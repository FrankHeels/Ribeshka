"""Check the migration graph even when PostgreSQL is unavailable."""

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase


class FlatCategoryMigrationStateTests(SimpleTestCase):
    def test_migration_removes_hierarchy_but_keeps_product_relation(self):
        loader = MigrationLoader(None)
        before = loader.project_state([("catalog", "0001_initial")]).apps
        after = loader.project_state([("catalog", "0002_flat_categories")]).apps

        self.assertIn("parent", {field.name for field in before.get_model("catalog", "Category")._meta.fields})
        category = after.get_model("catalog", "Category")
        self.assertNotIn("parent", {field.name for field in category._meta.fields})
        self.assertFalse(category._meta.indexes)
        self.assertFalse(category._meta.constraints)
        self.assertIs(after.get_model("catalog", "Product")._meta.get_field("category").remote_field.model, category)
