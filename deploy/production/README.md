# VPS: закрытая приёмка и HTTPS

Для последующих выпусков используй [CI/CD](../../docs/ci-cd.md). При работе через агент
`FISHSITE_RELEASE_SHA` и `FISHSITE_STATIC_DIR` задаются для каждого SHA; прежний общий
`fishsite_static_files` не очищается и не используется новыми релизами. Ниже описан ручной
bootstrap инфраструктуры. Перед ручными командами также задай эти две переменные из `.env.example`.

Контракт запуска — [public-deployment-spec.md](../../docs/public-deployment-spec.md).
Эти файлы сами по себе не подтверждают готовность VPS. Нужен Docker Compose с поддержкой
`env_file.format: raw` (v2.30+). Используй этот standalone Compose; не объединяй его с корневым.
Рабочий каталог команд ниже — каталог релиза в `/opt/fishsite/releases/`.

1. Сохрани три утверждённых образа с digest в `deploy/production/.env`, взяв только имена переменных
   из `.env.example`. Образ приложения уже собран из утверждённого commit и использует `USER app`.
   Создай на хосте `/opt/fishsite/shared/{media,import,secrets}`, `/etc/letsencrypt` и
   `/var/lib/fishsite-acme/.well-known/acme-challenge`. Права media/static должны разрешать запись UID/GID
   `app` из конкретного образа, чтение Nginx; каталог import и его содержимое доступны `app` для чтения.
   Не меняй владельца media на случайный локальный UID.
   Для ACME обеспечь проход Nginx через каждый родительский каталог: `/var/lib/fishsite-acme`,
   `.well-known` и `acme-challenge` должны иметь режим `0755`, challenge-файлы — `0644`.

2. Создай отдельные серверные env-файлы, режим `0600`, закрытый каталог `secrets`:

   - `db.env`: только `POSTGRES_DB=fishsite`, `POSTGRES_USER=bootstrap_admin`, `POSTGRES_PASSWORD`.
   - `runtime.env`: Django settings и `DJANGO_DB_PASSWORD` роли `fishsite_runtime`.
   - `migration.env`: необходимые Django settings и `DJANGO_DB_PASSWORD` роли `fishsite_owner`.

   Bootstrap credentials не добавляй в runtime/migration. Compose явно задаёт имена ролей и адрес БД.
   Значения секретов в этих файлах записываются буквально, без shell-кавычек/интерполяции.
   Для закрытой приёмки добавь в `DJANGO_ALLOWED_HOSTS` основной ASCII-hostname
   `ribeshka39.ru`, `localhost`, `127.0.0.1`; задай `DJANGO_TRUST_X_REAL_IP=true`.
   Приватный listener сохраняет клиентский Host и перезаписывает HTTPS-признак фактической схемой HTTP.
   До TLS `DJANGO_TRUST_X_FORWARDED_PROTO`, redirect и secure cookies выключены только для SSH-туннеля.
   CSRF остаётся включённым; браузерный тест должен использовать согласованный Host/origin туннеля.

3. Установи bootstrap-конфигурацию и подними БД:

   ```sh
   sudo install -o root -g root -m 0644 deploy/production/bootstrap.conf /opt/fishsite/shared/nginx.conf
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml config --quiet
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml up -d db
   ```

   Отдельной закрытой SQL-процедурой создай owner/runtime-роли, схему и default privileges из контракта.
   Runtime не должен владеть БД/схемой или иметь DDL, superuser и наследуемые административные права.
   Затем выполни миграции, импорт утверждённого пакета и сборку статики:

   ```sh
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml --profile migration run --rm migration
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml --profile migration run --rm migration python manage.py loaddata /imports/content.json
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml --profile migration run --rm migration python manage.py collectstatic --noinput
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml up -d web nginx
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml exec -T nginx nginx -t
   ```

   Worker-профили `notifications` и `moysklad` на этом этапе выключены. Не задавай `COMPOSE_PROFILES`.
   Проверяй приложение через SSH-туннель к `127.0.0.1:8080`; публичный HTTP обслуживает ACME и `503`.
   Проверь HTTP 200 главной, CSS/media, CSRF/POST, кабинет и отсутствие опубликованных портов web/БД.

4. После готовности DNS/сети выпусти доверенный сертификат Certbot на хосте через webroot
   `/var/lib/fishsite-acme`. Замени `__DOMAIN__` и `__OPERATOR_IP__` в `https.conf.template` на
   утверждённый ASCII-domain и адрес/CIDR оператора; сохрани результат в закрытый временный файл.
   Не используй staging-сертификат для публичного режима. Проверь результат отдельным Nginx-контейнером
   с production mounts, затем установи его как root-owned `/opt/fishsite/shared/nginx.conf` и
   **пересоздай** Nginx: атомарная замена bind-mounted файла требует нового mount.

   Только после разрешённой приёмки и подготовки runtime HTTPS settings:

   ```sh
   sed -e 's|__DOMAIN__|ribeshka39.ru|g' -e 's|__OPERATOR_IP__|{{IP_OR_CIDR}}|g' deploy/production/https.conf.template > /tmp/fishsite-nginx.conf
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml run --rm --no-deps -v /tmp/fishsite-nginx.conf:/etc/nginx/nginx.conf:ro nginx nginx -t
   sudo install -o root -g root -m 0644 /tmp/fishsite-nginx.conf /opt/fishsite/shared/nginx.conf
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml up -d --force-recreate web nginx
   docker compose --env-file deploy/production/.env -f deploy/production/compose.yaml exec -T nginx nginx -t
   ```

   Перед открытием включи в runtime env `DJANGO_TRUST_X_FORWARDED_PROTO`, HTTPS redirect и secure cookies;
   пересоздай web. После этого private HTTP listener подходит для curl, а браузерные POST/session
   проверяются на настоящем HTTPS-домене.
   Начальный HSTS — `300`, includeSubDomains/preload — `false`, после внешней проверки TLS/cookies/CSRF.
   После замены web пересоздай Nginx, чтобы upstream получил текущий адрес. Проверь `nginx -t`,
   `manage.py check --deploy`, неизвестные Host/SNI, IPv4/IPv6, redirect на фиксированный HTTPS-hostname,
   закрытый кабинет, certificate renew/deploy-hook. Hook запускает `nginx -t`, затем reload.
   Установи готовый hook на хосте и проверь продление:

   ```sh
   sudo install -o root -g root -m 0755 deploy/production/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/fishsite-nginx
   sudo certbot renew --dry-run --run-deploy-hooks
   ```

   Hook берёт общий operations lock, находит единственный Nginx по Compose labels, проверяет его и отправляет reload
   и записывает UTC-время в `/opt/fishsite/shared/certbot-last-reload.txt`.
   До выпуска сертификата проверь не только A, но и AAAA: адрес другого сервера нужно убрать;
   HTTP-01 доступен проверяющим центрам по обоим семействам адресов.
   Публичные формы открываются только после полной приёмки, backup/restore и разрешения владельца.

Стабильный том БД: `fishsite_postgres_data`; media находится вне релиза, static — в каталоге каждого SHA.
Не выполняй `down -v`. Храни предыдущий образ и соответствующую статику для допустимого rollback.
Сеть БД внутренняя; Nginx подключён только к frontend. Frontend и worker_egress — обычные bridge-сети:
исходящие соединения web технически доступны; ограничения SMTP/API/DNS требуют firewall на хосте.
Worker остаются выключены до отдельной приёмки.

При смене основного домена сначала подготовь HTTP ACME listener нового имени, сохранив старый сайт.
После готовности публичного DNS выпусти отдельный сертификат. Согласованно обнови `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS`, Host в healthcheck Compose и материализованный Nginx; проверь кандидат
до переключения. Для перенаправления старого домена добавь отрендеренные блоки
`legacy-domain.conf.template` **внутрь `http {}`**. Сохраняй старый сертификат и его HTTP ACME route,
иначе HTTPS-перенаправление или его продление перестанут работать. Проверяй точный `200` нового сайта,
`308` со старого с сохранением path/query и renew dry-run обеих certificate lineages.
Cookies старого домена не переносятся на другой домен: корзина и вход начинают новую сессию.
Не ослабляй CSRF ради отправки форм, открытых до переезда; пользователю нужно заново открыть форму.

Если провайдер разблокирует всю группу SMTP вместо одного согласованного порта 465, установи
ограничение новых соединений на 25/587/2525 для хоста и Docker (iptables backend):

```sh
sudo install -o root -g root -m 0755 deploy/production/restrict-smtp-egress.sh /usr/local/sbin/fishsite-smtp-egress
sudo install -o root -g root -m 0644 deploy/production/fishsite-smtp-egress.service /etc/systemd/system/fishsite-smtp-egress.service
sudo systemctl daemon-reload
sudo systemctl enable --now fishsite-smtp-egress.service
```

Сервис запускается после Docker/UFW, повторный запуск не дублирует правила. После ручного `ufw reload`
повторно запусти `systemctl restart fishsite-smtp-egress.service`. Он сохраняет доступ к 465,
не заменяет общий белый список исходящих SMTP/API/DNS и не предназначен для Docker nftables backend.

Access logs содержат метод, нормализованный URI и статус без query, Referer или User-Agent.
Для webhook и страниц подтверждения с токеном в URL logs выключены; Nginx error log отключён целиком, поскольку формат ошибок может содержать
полный URI. Контролируй `nginx -t`, startup/reload и внешний HTTP отдельным мониторингом.
Ротация Docker logs — `10m` × `3` на контейнер. Полный вывод `compose config` с секретами не сохраняй.
