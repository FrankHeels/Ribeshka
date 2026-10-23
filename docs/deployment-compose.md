# Локальная VM через Docker Compose

Профиль Compose предназначен для частной Ubuntu VM и проверки полного стека. Nginx публикует
HTTP на порту хоста 8000 по умолчанию; TLS и production-ограничения здесь не настроены. Не
открывай стенд в интернет.

## Первый запуск

Перенеси чистый Git checkout в Ubuntu. Не копируй `.env`, `.env.test`, `.venv`,
`.test-postgres/` или локальный `media/`. Создай новую среду из примера; локальные secrets не
публикуй:

~~~sh
cp .env.compose.example .env
chmod 600 .env
hostname -I
nano .env
docker compose config --quiet
docker compose build
docker compose up -d db
docker compose run --rm web python manage.py migrate --noinput
docker compose run --rm web python manage.py collectstatic --noinput
docker compose run --rm web python manage.py createsuperuser
docker compose up -d web nginx
docker compose ps
~~~

В `.env` добавь доступный с Mac адрес VM в `DJANGO_ALLOWED_HOSTS`, сохрани localhost значения
и не оставляй примерный адрес из документа. Если `hostname -I` показал несколько адресов,
выбери интерфейс частной сети, к которому подключается Mac. Открой
`http://<IP-адрес-VM>:8000/`. Переменную `WEB_PORT` можно изменить в окружении Compose. Docker
публикует порт на сетевых интерфейсах VM: ограничь его firewall частной сетью. Не открывай
стенд в интернет. `.env.compose.example` содержит defaults для частной VM; он не является
источником production secrets.

## Сервисы и фоновые процессы

- `db` — PostgreSQL с постоянным именованным томом.
- `web` — Django/Gunicorn; `nginx` — HTTP proxy и чтение static/media. Nginx здесь не завершает
  TLS.
- `worker` (`process_notifications`) включается только профилем `notifications`.
- `moysklad-worker` (`process_moysklad`) включается только профилем `moysklad`; его условия
  указаны в [руководстве МойСклад](moysklad-operations.md).

Сначала проверь SMTP-профиль, не подключая worker:

~~~sh
docker compose run --rm web python manage.py check_notification_smtp
~~~

Preflight проверяет TLS/AUTH, но не отправляет письмо и не читает очередь. До запуска worker
проверь фактическую БД, получателя и готовые записи: worker обработает всю готовую очередь
выбранной базы. Ручная реальная отправка Mail.ru в изолированной БД описана в [практической
процедуре](mailru-smtp-practical-test.md).

~~~sh
docker compose --profile notifications up -d worker
docker compose logs -f worker
~~~

## Остановка и данные

~~~sh
docker compose logs -f web nginx
docker compose ps
docker compose --profile notifications --profile moysklad stop --timeout 120 worker moysklad-worker
docker compose down
~~~

`docker compose down` останавливает и удаляет контейнеры, но оставляет именованные тома. `docker
compose down -v` удалит PostgreSQL, static и media; не запускай его, пока данные нужны.

## Перед публичным VPS

Сейчас Nginx слушает HTTP; Compose публикует только `WEB_PORT`. До production нужны HTTPS, DNS,
ограниченные firewall правила, Django secure/proxy settings, разделённые migration/runtime роли,
внешние зашифрованные backup и испытанное восстановление. Смена environment требует пересоздать
контейнеры, а не только `restart`. Полный целевой контракт, включая Certbot и приемку, — в
[спецификации публичного запуска](public-deployment-spec.md); требования к backup/restore — в
[отдельном контракте](backup-restore.md).

[Индекс Wiki](wiki/INDEX.md)
