"""Prove that flattening existing categories retains records and product links."""

# Запуск этого файла: `.venv/bin/python manage.py test tests.test_category_migration --settings=fishsite.settings_test --noinput`.
# Django Test Runner создаёт отдельную `test_fishsite` в изолированном PostgreSQL-кластере
# `127.0.0.1:15432` (профиль `fishsite.settings_test`), применяет миграции и удаляет БД
# после прогона; окружение и подключение защищены правилами из `docs/test-environment.md`.
# Тест использует `TransactionTestCase` и `MigrationExecutor`: переключает схему на
# историческую миграцию, создаёт категории и товар через исторические модели, применяет
# целевую миграцию, затем проверяет записи, связь товара и состав таблицы через introspection.
# Внешние сервисы и фикстуры не используются; нужны зависимости проекта, тестовое окружение
# и доступность отдельного тестового PostgreSQL, подготовленного по `docs/test-environment.md`.

from decimal import Decimal

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from catalog.models import Category, Product


class FlatCategoryMigrationTests(TransactionTestCase):
    migrate_from = ("catalog", "0001_initial")
    migrate_to = ("catalog", "0002_flat_categories")

    # Создаёт корневую и вложенную категории с заполненными полями и товар во вложенной;
    # после миграции ожидаются две сохранённые категории, прежние данные дочерней категории,
    # сохранённая ссылка товара на неё и отсутствие столбца parent_id в таблице категорий.
    # Блок finally в любом случае оставляет схему на целевой миграции.
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
