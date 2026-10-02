from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import Category, Product, ProductImage


CATALOG = (
    {
        "name": "Копченая рыба",
        "slug": "kopchenaya-ryba",
        "description": "Примеры рыбы горячего копчения для проверки каталога.",
        "products": (
            ("Лосось горячего копчения", "smoked-salmon", "Сочная мякоть с выраженным ароматом копчения.", "Нежный лосось горячего копчения с золотистой корочкой.", "1890.00", "smoked-salmon.png"),
            ("Скумбрия горячего копчения", "smoked-mackerel", "Плотная рыба с насыщенным вкусом.", "Скумбрия горячего копчения с золотистой кожицей и сочной мякотью.", "790.00", "smoked-mackerel.png"),
            ("Горбуша горячего копчения", "smoked-pink-salmon", "Нежная мякоть и мягкий аромат дыма.", "Горбуша горячего копчения, приготовленная целым стейком.", "990.00", "smoked-pink-salmon.png"),
        ),
    },
    {
        "name": "Вяленая рыба",
        "slug": "vyalenaya-ryba",
        "description": "Примеры традиционной вяленой рыбы для проверки каталога.",
        "products": (
            ("Вобла вяленая", "dried-roach", "Классическая вяленая рыба с плотной текстурой.", "Вяленая вобла целиком.", "1250.00", "dried-roach.png"),
            ("Лещ вяленый", "dried-bream", "Насыщенный вкус и плотная текстура.", "Вяленый лещ целиком.", "980.00", "dried-bream.png"),
            ("Окунь вяленый", "dried-perch", "Небольшая рыба с выразительным вкусом.", "Вяленый окунь целиком.", "1100.00", "dried-perch.png"),
        ),
    },
    {
        "name": "Свежемороженная рыба",
        "slug": "svezhemorozhennaya-ryba",
        "description": "Примеры свежемороженой рыбы для проверки каталога.",
        "products": (
            ("Филе трески свежемороженое", "frozen-cod", "Светлое филе с нежной слоистой мякотью.", "Филе трески в замороженном виде.", "1150.00", "frozen-cod.png"),
            ("Горбуша свежемороженая", "frozen-pink-salmon", "Целая рыба для запекания и домашних блюд.", "Свежемороженая горбуша.", "690.00", "frozen-pink-salmon.png"),
            ("Филе минтая свежемороженое", "frozen-pollock", "Нежное филе с мягким вкусом.", "Филе минтая в замороженном виде.", "590.00", "frozen-pollock.png"),
        ),
    },
    {
        "name": "Слабосоленая рыба",
        "slug": "slabosolenaya-ryba",
        "description": "Примеры слабосоленой рыбы для проверки каталога.",
        "products": (
            ("Лосось слабосоленый", "salted-salmon", "Нежные ломтики с деликатным вкусом.", "Филе лосося слабого посола.", "2190.00", "salted-salmon.png"),
            ("Сельдь слабосоленая", "salted-herring", "Мягкий посол сохраняет вкус и сочность рыбы.", "Филе сельди слабого посола.", "620.00", "salted-herring.png"),
            ("Форель слабосоленая", "salted-trout", "Нежная мякоть и тонкий вкус.", "Филе форели слабого посола.", "2050.00", "salted-trout.png"),
        ),
    },
)


class Command(BaseCommand):
    help = "Добавить демонстрационные категории и товары; существующие записи не изменяются."

    @transaction.atomic
    def handle(self, *args, **options):
        assets_dir = Path(settings.MEDIA_ROOT) / "catalog" / "products"
        expected_assets = [product[-1] for category in CATALOG for product in category["products"]]
        missing = [name for name in expected_assets if not (assets_dir / name).is_file()]
        if missing:
            raise CommandError(
                "Не найдены изображения в media/catalog/products/: " + ", ".join(missing)
            )

        made_categories = made_products = made_images = 0
        for order, category_data in enumerate(CATALOG):
            category, created = Category.objects.get_or_create(
                slug=category_data["slug"],
                defaults={
                    "name": category_data["name"],
                    "description": category_data["description"],
                    "display_order": order,
                },
            )
            made_categories += created
            for name, slug, short_description, description, price, image_name in category_data["products"]:
                product, created = Product.objects.get_or_create(
                    slug=slug,
                    defaults={
                        "name": name,
                        "category": category,
                        "short_description": short_description,
                        "description": description,
                        "price_per_kg": price,
                        "minimum_weight": "0.100",
                        "weight_step": "0.100",
                        "is_published": True,
                    },
                )
                made_products += created
                if product.images.exists():
                    continue

                photo = ProductImage(
                    product=product,
                    alt_text=f"{name} на фоне темного деревянного стола",
                    display_order=0,
                    is_primary=True,
                )
                with (assets_dir / image_name).open("rb") as image_file:
                    photo.image.save(image_name, File(image_file), save=True)
                made_images += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: добавлено категорий — {made_categories}, товаров — "
                f"{made_products}, фотографий — {made_images}."
            )
        )
        self.stdout.write("Цены в этом наборе примерные, для тестирования каталога.")
