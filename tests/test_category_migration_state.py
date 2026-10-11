"""Check the migration graph even when PostgreSQL is unavailable."""

# Запуск именно этого файла:
# .venv/bin/python manage.py test tests.test_category_migration_state --settings=fishsite.settings_test --noinput
# Тест запускается Django test runner с тестовым профилем fishsite.settings_test,
# который защищает подключения и направляет БД в изолированное тестовое окружение.
# Сам класс наследует SimpleTestCase и не обращается к БД: MigrationLoader(None)
# читает локальный граф миграций и строит project state для двух миграций.
# Проверяется состояние моделей до и после миграции; SQL, фикстуры и запись данных
# не используются, поэтому этот файл может выполняться без работающего PostgreSQL.

from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase


class FlatCategoryMigrationStateTests(SimpleTestCase):
    # Сравнивает состояния catalog после 0001_initial и 0002_flat_categories:
    # до миграции у Category есть parent, после него поля parent, индексов и
    # ограничений нет; Product.category по-прежнему ссылается на эту Category.
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
