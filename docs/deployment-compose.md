# Локальный деплой через Docker Compose

Этот сценарий описывает развёртывание FishSite в Ubuntu VM и подготовку к VPS.
Compose поднимает PostgreSQL, Django/Gunicorn, Nginx и отдельный worker
уведомлений. Текущая конфигурация по умолчанию остаётся HTTP для частной VM.
Для публичного запуска сначала выполните VPS-чеклист ниже: HTTPS, домен,
секреты, firewall и проверенные внешние резервные копии.

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
загруженные файлы.

## Чеклист перед публичным запуском на VPS

Эти значения зависят от домена и выбранного способа выпуска TLS-сертификата;
не включайте HTTPS-переключатели, пока HTTPS ещё не обслуживается Nginx.

1. Направьте DNS `A`-запись домена на VPS. Добавляйте `AAAA` только если IPv6
   настроен и доступен. В firewall разрешите SSH только с нужных адресов, а
   публично откройте `80/tcp` и `443/tcp`. Не публикуйте `5432/tcp`.
2. Настройте TLS-сертификат и Nginx для домена. Текущий `default.conf` — только
   HTTP-конфигурация; выпуск и обновление сертификата ещё нужно настроить для
   выбранного VPS и домена. Убедитесь, что Nginx перезаписывает
   `X-Forwarded-Proto` значением `$scheme` (это уже делает текущий proxy-блок).
3. В `.env` задайте домен в `DJANGO_ALLOWED_HOSTS`, например
   `shop.example.ru`, и HTTPS-origin в `DJANGO_CSRF_TRUSTED_ORIGINS`, например
   `https://shop.example.ru`. Для каждого дополнительного домена укажите
   отдельное значение через запятую. Оставьте `DJANGO_DEBUG=false`.
4. После включения HTTPS в Nginx установите:

   ```dotenv
   DJANGO_TRUST_X_FORWARDED_PROTO=true
   DJANGO_SECURE_SSL_REDIRECT=true
   DJANGO_SESSION_COOKIE_SECURE=true
   DJANGO_CSRF_COOKIE_SECURE=true
   DJANGO_SECURE_HSTS_SECONDS=300
   DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=false
   DJANGO_SECURE_HSTS_PRELOAD=false
   ```

   Сначала проверьте сайт и формы по HTTPS, затем начните с короткого HSTS
   срока и увеличивайте `DJANGO_SECURE_HSTS_SECONDS` постепенно. Не включайте
   `include_subdomains` или `preload`, пока все поддомены не гарантированно
   работают по HTTPS: браузеры будут принудительно применять эту политику.
5. Проверьте настройки Django внутри контейнера:

   ```sh
   docker compose run --rm web python manage.py check --deploy
   ```

   Разберите каждое предупреждение. При намеренно выключенных HSTS
   `include_subdomains` и `preload` Django может показать `security.W005` и
   `security.W021`; включайте их только когда все поддомены постоянно работают
   по HTTPS и вы осознанно хотите политику preload. Предупреждение о слабом
   `SECRET_KEY` нужно устранить. Команда не проверяет DNS, TLS-сертификат,
   firewall, резервное копирование или доставку писем.
6. Настройте резервирование PostgreSQL и `/media/` на отдельное хранилище вне
   VPS. Защитите архивы от постороннего доступа и зафиксируйте расписание,
   срок хранения и ответственного. Одной копии на том же VPS недостаточно.
   Перед публикацией выполните пробное восстановление БД и фотографий в
   отдельную тестовую среду и проверьте, что сайт открывается.
7. Настройте SMTP и адрес получателя, проверьте соединение командой
   `check_notification_smtp`, затем включите worker. До запуска убедитесь, что
   в очереди нет заявок, которые нельзя отправлять.
8. Выполните тестовую розничную и оптовую заявку, проверьте письмо, запись в
   админке, журналы `web`/`worker` и поведение при временной ошибке SMTP.
   Убедитесь, что `sent` трактуется как принятие письма SMTP-сервером.

Секреты `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD` и SMTP должны быть уникальными
для VPS, храниться только на сервере с правами `0600` и никогда не попадать в
Git или сообщения. Для выпуска на VPS обновляйте код контролируемо: сначала
резервная копия, затем сборка образа, миграции, `collectstatic`, перезапуск и
проверка сайта/логов. `docker compose down -v` удаляет данные и не является
командой обновления.

[Индекс проекта](wiki/INDEX.md)

## Процесс МойСклад

В `compose.yaml` добавлен отдельный `moysklad-worker` с командой
`process_moysklad` и профилем `moysklad`. Он использует тот же образ и БД;
почтовый `worker` остаётся независимым профилем `notifications`.

```sh
docker compose --profile notifications --profile moysklad config --quiet
docker compose --profile notifications --profile moysklad up -d
```

Перед запуском подтвердите итоговую конфигурацию Compose, backup и миграции,
серверные `MOYSKLAD_*`, целевой аккаунт и постоянный namespace. По умолчанию
все потоки выключены. Текущий репозиторный Nginx остаётся HTTP для частной VM;
рабочему вебхуку нужен отдельный проверенный HTTPS-вход. Его location ограничивает
тело 1 МиБ и отключает access/error-логирование секретного маршрута. Настройка
TLS и доставка реального вебхука проверяются на целевом прокси.

Порядок включения, административные права команды регистрации, остановка и
восстановление — [эксплуатация обмена](moysklad-operations.md). При откате до
версии без приёмника сначала отключите собственный вебхук; чужие регистрации
не трогайте. Синтаксический разбор YAML не заменяет `docker compose config` и
`nginx -t` на машине с установленными инструментами.
