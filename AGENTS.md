# FishSite: индекс LLM Wiki

## О проекте

FishSite — витрина рыбной продукции с розничными и оптовыми заявками.
Продавец согласовывает продажу и оплату с покупателем вне сайта.

Реализованы интерфейс Django Templates, модели и миграции PostgreSQL,
Django Admin и редактируемые тексты главной и информационных страниц.
Главная показывает все категории опубликованных товаров; страницы категорий
и товаров открываются отдельно. Добавление, просмотр, изменение и удаление
позиций розничной корзины работают через анонимную Django-сессию; цены и
опубликованность перечитываются из каталога. Сохранение заявок через публичные
формы и отправка уведомлений ещё не реализованы: оформление и оптовая форма
отключены, их POST отвечает 503. Документы Wiki также содержат требования
полного MVP, поэтому сверяй описание текущего состояния с кодом.

## Технический стек

- Модульный монолит: Python 3.13, актуальное исправление Django 5.2 LTS, PostgreSQL. Приложения по смыслу: `catalog`, `orders` (розница), `inquiries` (опт), `content`, `integrations`. Не дробить их на сервисы без необходимости.
- Публичный HTML через Django Templates и серверный рендеринг; HTMX для частичных HTML-обновлений, немного обычного JavaScript, Tailwind CSS 4. Основной сценарий обязан работать без JavaScript. Отдельный React/Next runtime и DRF на старте не нужны.
- Django Admin — управление ассортиментом, фото, текстами страниц, заявками и повтором уведомлений.
- Уведомления: PostgreSQL Transactional Outbox + `LISTEN/NOTIFY`, Psycopg 3; отдельный worker `process_notifications`.
- Целевое развёртывание: Docker Compose, Gunicorn, Nginx, VPS с HTTPS.

## Как читать Wiki

Выбирай документы по задаче из таблицы; не загружай всю Wiki заранее.
Связанные страницы внутри документа читай только при необходимости.
Согласованные требования находятся в тематических страницах, сведения
о текущей реализации — в коде и документах слоя данных/контракта шаблонов.
При изменении правил обновляй соответствующую страницу вместе с реализацией;
при добавлении или переименовании документа обновляй этот индекс.

| Когда читать | Документ | Что внутри |
|---|---|---|
| Бизнес-правила, границы задачи, наполнение | [Границы MVP](docs/wiki/mvp-scope.md) | Возможности и исключения, килограммы, публикация, допущения, материалы заказчика и ограничения референсов. |
| Маршруты, ассортимент, навигация | [Карта страниц](docs/wiki/page-map.md) | URL, состав экранов, действия, общие требования и SEO. |
| Модели, миграции, Django Admin, контент | [Сущности](docs/wiki/entities.md) | Каталог, фиксированные страницы, заявки, снимки и границы хранения. |
| Схема и ограничения уже реализованных моделей | [Слой данных](docs/data-layer.md) | Поля, связи, индексы, Decimal, ограничения ORM/БД, миграции и результаты проверок. |
| Корзина, оформление, оптовая форма, защита POST | [Сценарии заявок](docs/wiki/order-flow.md) | Сессия, расчёты, подтверждение цены/состава, атомарность, идемпотентность, CSRF и персональные данные. |
| SMTP, очередь, worker, повтор из админки | [Уведомления](docs/wiki/notifications.md) | Outbox, LISTEN/NOTIFY, переподключение, захват задач, повторы и содержимое писем. |
| Views, шаблоны, поля и ошибки форм | [Контракт frontend](docs/frontend-contract.md) | Имена URL, контекст шаблонов, состояния, безопасные сообщения и редактируемый контент. |
| Оформление, CSS, адаптивность, доступность | [Дизайн-система](docs/design-system.md) | Утверждённое тёмное дерево, токены, шрифты, логотип, изображения, меню, движение и визуальная проверка. |
| VPS, хранилище, бэкапы, кеш, внешний адаптер | [Эксплуатация](docs/wiki/operations.md) | Развёртывание, мониторинг, отложенный Redis и граница будущей интеграции. |
| Следующий этап, проверки и приёмка | [План и приёмка](docs/wiki/implementation-plan.md) | Порядок реализации, полный сценарий, PostgreSQL, конкурентность, сбои и восстановление. |
| Локальный запуск и работа сотрудника | [README](README.md) | Зависимости, окружение, миграции, сборка CSS, редактирование материалов и команды проверки. |
| Исследование структуры референса | [Исследование Fish Empire](docs/research/fish-empire39-site-structure.md) | Справка об устройстве страниц; ограничения использования — в границах MVP. |

## Context7

<!-- context7 -->
Use Context7 MCP to fetch current documentation whenever the user asks about a library, framework, SDK, API, CLI tool, or cloud service — even well-known ones like React, Next.js, Prisma, Express, Tailwind, Django, or Spring Boot. This includes API syntax, configuration, version migration, library-specific debugging, setup instructions, and CLI tool usage. Use even when you think you know the answer — your training data may not reflect recent changes. Prefer this over web search for library docs.

Do not use for: refactoring, writing scripts from scratch, debugging business logic, code review, or general programming concepts.

## Steps

1. Always start with `resolve-library-id` using the library name and what to look up in the library's documentation, unless the user provides an exact library ID in `/org/project` format
2. Pick the best match (ID format: `/org/project`) by: exact name match, description relevance, code snippet count, source reputation (High/Medium preferred), and benchmark score (higher is better). If results don't look right, try alternate names or queries (e.g., "next.js" not "nextjs", or rephrase the question). Use version-specific IDs when the user mentions a version
3. `query-docs` with the selected library ID and what to look up in the library's documentation (not single words), scoped to a single concept. If the question spans multiple distinct concepts (e.g. routing and auth and caching), make a separate `query-docs` call per concept with the same library ID, unless the question is about how the concepts interact — combined queries dilute ranking and return shallow results for each topic
4. Answer using the fetched docs
<!-- context7 -->
