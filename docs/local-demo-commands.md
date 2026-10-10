# Команды для живой демонстрации

Перед показом проверьте, что в корневом `.env` настроен SMTP, а в рабочей очереди нет старых готовых заявок, которые не должны быть отправлены. Worker обрабатывает все готовые уведомления выбранной БД.

Откройте две вкладки терминала.

**Вкладка 1 — проверка SMTP и запуск сайта:**

```sh
cd /Users/frankheels/FishSite
uv run python manage.py check_notification_smtp && uv run python manage.py runserver 127.0.0.1:8000
```

Сайт: <http://127.0.0.1:8000/>.

**Вкладка 2 — постоянный worker:**

```sh
cd /Users/frankheels/FishSite
uv run python manage.py process_notifications
```

Оставьте обе вкладки работающими во время показа. Для остановки каждого процесса нажмите `Ctrl+C` в его вкладке.
