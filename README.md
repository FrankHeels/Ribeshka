# FishSite

Витрина с розничной корзиной и отдельной оптовой формой. Сайт сохраняет заявки;
продавец согласовывает наличие, продажу и оплату с покупателем. Публичные страницы
работают на Django Templates, данные — в PostgreSQL, оформление — Tailwind CSS.

Под задачу выбирайте документ в [LLM Wiki](docs/wiki/INDEX.md).

## Локальный запуск

Нужны Python 3.13, `uv` и PostgreSQL. Node.js/npm нужны для сборки CSS.
Версии зависимостей закреплены в [pyproject.toml](pyproject.toml), [uv.lock](uv.lock)
и [frontend/package-lock.json](frontend/package-lock.json).

```sh
uv sync --frozen --python 3.13
cp .env.example .env
```

Команду копирования выполняйте только при отсутствии своего `.env`.
Создайте локальную БД и роль PostgreSQL; заполните `DJANGO_DB_*` в `.env`.
Задайте уникальный `DJANGO_SECRET_KEY`, а для приватного `runserver` —
`DJANGO_DEBUG=true`. Секрет можно сгенерировать локально:

```sh
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(64))'
```

Результат храните в локальном `.env` или secret store. `.env` исключён из Git,
загружается каждым Django-процессом; переменные процесса имеют приоритет.

```sh
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Сайт — [localhost:8000](http://127.0.0.1:8000/), панель —
[localhost:8000/admin](http://127.0.0.1:8000/admin/).
Фотографии хранятся в `media/` и не входят в Git. Для нового изолированного
демо-окружения есть `manage.py seed_demo_catalog`; его цены и материалы
демонстрационные. Команда не заменяет подтверждённое наполнение.

## CSS

```sh
npm --prefix frontend ci
npm --prefix frontend run build:css
# Во время разработки:
npm --prefix frontend run watch:css
```

Редактируйте исходники в `frontend/`; `static/css/site.css` — результат сборки.
Правила оформления и проверки мобильного экрана — в
[дизайн-системе](docs/design-system.md).

## Тесты

Автотестам нужен отдельный PostgreSQL из [инструкции тестового окружения](docs/test-environment.md).
После первичной настройки:

```sh
.venv/bin/python scripts/test_postgres.py start
.venv/bin/python manage.py test tests --settings=fishsite.settings_test --noinput
```

## Настройка отдельных возможностей

| Задача | Инструкция |
|---|---|
| Создать обычного владельца, настроить TOTP, управлять заявками | [Панель владельца](docs/backoffice.md) |
| Настроить SMTP, проверить соединение, запустить почтовый worker | [Уведомления](docs/wiki/notifications.md) |
| Проверить настоящее письмо Mail.ru на отдельной БД | [Ручная проверка SMTP](docs/mailru-smtp-practical-test.md) |
| Связать товары, включить цены и передачу розничных заявок в МойСклад | [Эксплуатация обмена](docs/moysklad-operations.md) |
| Настроить карты и ключ Яндекса | [Карты](docs/delivery-map.md) |
| Запустить приватный Compose-стенд | [Docker Compose](docs/deployment-compose.md) |
| Подготовить публичный сервер, HTTPS и восстановление | [Спецификация запуска](docs/public-deployment-spec.md) |
