# Локальная демонстрация

Для показа страниц и форм используй отдельную демонстрационную БД с синтетическим каталогом.
Формы сохраняют обращения даже без worker. Не запускай это на БД с рабочими заявками.

Убедись, что fishsite_demo ещё не существует. Если существует, сначала проверь её назначение;
неизвестную БД не очищай и не переиспользуй. Для createdb задай в shell локальные параметры
PostgreSQL из .env: DJANGO_DB_HOST, DJANGO_DB_PORT и DJANGO_DB_USER. createdb запросит пароль
PostgreSQL, если он нужен.

~~~sh
createdb --host "$DJANGO_DB_HOST" --port "$DJANGO_DB_PORT" --username "$DJANGO_DB_USER" --owner "$DJANGO_DB_USER" fishsite_demo
DJANGO_DB_NAME=fishsite_demo .venv/bin/python manage.py migrate
DJANGO_DB_NAME=fishsite_demo .venv/bin/python manage.py seed_demo_catalog
DJANGO_DB_NAME=fishsite_demo .venv/bin/python manage.py shell -c "from django.db import connection; cursor = connection.cursor(); cursor.execute('SELECT current_database()'); print(cursor.fetchone()[0]); cursor.close()"
DJANGO_DB_NAME=fishsite_demo DJANGO_DEBUG=true .venv/bin/python manage.py runserver 127.0.0.1:8000
~~~

Последняя проверка должна напечатать fishsite_demo. Открой http://127.0.0.1:8000/. Оставь SMTP-
и МойСклад-worker остановленными: они выполняют внешние действия и обрабатывают готовые записи
выбранной БД.

Для реального тестового письма используй [изолированную процедуру
Mail.ru](mailru-smtp-practical-test.md); для МойСклад — [руководство
интеграции](moysklad-operations.md). Для Ubuntu VM используй [Compose](deployment-compose.md).

Останови runserver через Ctrl+C.

[Индекс проекта](wiki/INDEX.md)
