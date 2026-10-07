# Локальный деплой через Docker Compose

Этот сценарий нужен для отработки развёртывания FishSite в Ubuntu VM и на
небольшом VPS. Compose поднимает PostgreSQL, Django/Gunicorn, Nginx и отдельный
worker уведомлений. Сейчас он рассчитан на тест в частной VM: HTTPS и
автоматическое резервное копирование ещё не настроены. Не публикуйте его в
интернет до настройки домена, HTTPS, секретов и резервного копирования.

## Что запускается

- `db` — PostgreSQL 18.6; данные хранятся в именованном томе `postgres_data`.
- `web` — Django под Gunicorn; использует Python 3.13 и зависимости из `uv.lock`.
- `nginx` — проксирует запросы к Django и раздаёт собранную статику и медиа.
- `worker` — отдельный процесс `process_notifications`, включаемый профилем
  `notifications` после настройки SMTP.

Статика, медиа и PostgreSQL хранятся в отдельных Docker volumes. Обычная команда
`docker compose down` их сохраняет; `docker compose down -v` удаляет, включая
локальную базу и загруженные фотографии.

## Подготовить проект в Ubuntu

Код проекта должен находиться внутри VM. Если рабочие изменения уже доступны в
Git-репозитории, клонируйте его в Ubuntu. Если ещё не доступны, перенесите код
отдельно, не копируя `.env`, `.env.test`, `.venv`, `.test-postgres` и `media/`.
Секреты нужно создать отдельно внутри VM.

В каталоге проекта создайте локальный env-файл:

```sh
cp .env.compose.example .env
chmod 600 .env
nano .env
```

Задайте уникальные значения для `POSTGRES_PASSWORD` и `DJANGO_SECRET_KEY`.
Сгенерировать случайное значение можно командой `openssl rand -hex 32`;
выполните её дважды и перенесите результаты в `.env`. Пароли не присылайте
и не добавляйте `.env` в Git.

Получите адрес VM:

```sh
hostname -I
```

Добавьте первый показанный адрес к `DJANGO_ALLOWED_HOSTS`, например:

```text
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.64.5
```

В примере замените адрес на текущий IP вашей VM. Для будущего VPS укажите его
доменное имя в этом же параметре.

## Собрать и запустить

Из корня проекта в Ubuntu выполните:

```sh
docker compose build
docker compose up -d db
docker compose run --rm web python manage.py migrate --noinput
docker compose run --rm web python manage.py collectstatic --noinput
docker compose run --rm web python manage.py createsuperuser
docker compose up -d web nginx
docker compose ps
```

Nginx слушает порт `8000` по умолчанию. Откройте в браузере на Mac
`http://<IP-адрес-VM>:8000/`; для админки добавьте `/admin/`. При необходимости
измените `WEB_PORT` в `.env`.

В профиле по умолчанию worker не стартует. Сначала заполните в `.env` SMTP-параметры
и адреса отправителя/получателя. Проверьте соединение без отправки письма:

```sh
docker compose run --rm web python manage.py check_notification_smtp
```

Затем включите worker:

```sh
docker compose --profile notifications up -d worker
docker compose logs -f worker
```

Перед проверкой реальной почты убедитесь, что база содержит только тестовые
заявки. Worker обрабатывает outbox в указанной базе.

## Полезные команды

```sh
docker compose logs -f web nginx
docker compose ps
docker compose down
```

Не используйте `docker compose down -v`, если хотите сохранить тестовую БД и
загруженные файлы. На VPS эта же конфигурация потребует настроить HTTPS,
постоянные резервные копии БД и media, правила firewall и реальные
`DJANGO_ALLOWED_HOSTS` до публикации сайта.

[Индекс проекта](../AGENTS.md)
