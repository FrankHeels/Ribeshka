# Практическая проверка Mail.ru SMTP

Эта проверка отправляет реальные письма, но использует отдельную БД с синтетическими заявками.
Не запускай проверочный worker на основной БД fishsite: он обрабатывает все готовые задачи
выбранной БД.

## Подготовка

- В локальном .env настрой Mail.ru по [шаблону параметров](../.env.example). Внешние переменные
  окружения имеют приоритет; не используй старую вкладку shell с устаревшими SMTP-параметрами.
- Выбери отдельный тестовый адрес получателя. Переопредели NOTIFICATION_TO_EMAIL только в
  командах SMTP ниже; не меняй основной получатель в .env ради теста.
- Убедись, что БД fishsite_mailru_validation ещё не существует. Если она существует, остановись
  и проверь её назначение; не очищай и не переиспользуй её вслепую.
- Для createdb используй локальную PostgreSQL-роль владельца с правом CREATEDB. Параметры
  подключения задавай в shell отдельно; пароль не помещай в команду.

~~~sh
createdb --host "$DJANGO_DB_HOST" --port "$DJANGO_DB_PORT" --username "$DJANGO_DB_USER" --owner "$DJANGO_DB_USER" fishsite_mailru_validation
DJANGO_DB_NAME=fishsite_mailru_validation .venv/bin/python manage.py migrate
DJANGO_DB_NAME=fishsite_mailru_validation .venv/bin/python manage.py seed_demo_catalog
DJANGO_DB_NAME=fishsite_mailru_validation .venv/bin/python manage.py shell -c "from django.db import connection; cursor = connection.cursor(); cursor.execute('SELECT current_database()'); print(cursor.fetchone()[0]); cursor.close()"
DJANGO_DB_NAME=fishsite_mailru_validation DJANGO_DEBUG=true .venv/bin/python manage.py runserver 127.0.0.1:8000
~~~

Перед продолжением подтверди, что база в терминале Django — fishsite_mailru_validation. Создай
синтетические розничную и, при необходимости, оптовую заявки через браузер. Используй тестовые
контактные данные; пометь комментарий как тестовый.

## Preflight и отправка

Preflight не отправляет письмо и не обращается к БД/outbox. Перед его запуском выбери
контрольный ящик:

~~~sh
DJANGO_DB_NAME=fishsite_mailru_validation NOTIFICATION_TO_EMAIL=test-recipient@example.com .venv/bin/python manage.py check_notification_smtp
~~~

Замени test-recipient@example.com в командах на контролируемый реальный ящик. Успех подтверждает
соединение, TLS и AUTH для текущего профиля Mail.ru, но не доставку письма. Перед worker проверь
фактическую БД и убедись, что в outbox находятся только подготовленные синтетические задачи.
Команда ниже обрабатывает все готовые задачи этой БД:

~~~sh
DJANGO_DB_NAME=fishsite_mailru_validation NOTIFICATION_TO_EMAIL=test-recipient@example.com .venv/bin/python manage.py shell -c "from integrations.models import NotificationOutbox; print(list(NotificationOutbox.objects.exclude(status='sent').order_by('pk').values('id', 'status', 'attempt_count', 'last_error')))"
DJANGO_DB_NAME=fishsite_mailru_validation NOTIFICATION_TO_EMAIL=test-recipient@example.com .venv/bin/python manage.py process_notifications --once
~~~

Для проверки постоянного worker сначала создай ровно одну новую синтетическую заявку, затем
запусти процесс без --once:

~~~sh
DJANGO_DB_NAME=fishsite_mailru_validation NOTIFICATION_TO_EMAIL=test-recipient@example.com .venv/bin/python manage.py process_notifications
~~~

Замени адрес получателя на контрольный. Не запускай параллельный worker, направленный на эту БД.

После отправки в новом терминале проверь только безопасные поля outbox:

~~~sh
DJANGO_DB_NAME=fishsite_mailru_validation .venv/bin/python manage.py shell -c "from integrations.models import NotificationOutbox; print(list(NotificationOutbox.objects.order_by('pk').values('id', 'status', 'attempt_count', 'last_error', 'sent_at')))"
~~~

## Результат

Сверь соответствующий outbox со статусом и безопасным событием delivery_attempt. Для завершения
проверки получи сообщение в контрольном почтовом ящике и проверь папку «Спам». Статус sent
означает только принятие письма SMTP-сервером. При неопределённом результате возможна повторная
отправка.

Для остановки процессов нажми Ctrl+C. Тестовая БД остаётся на месте и не затрагивает основную
fishsite; не удаляй её без отдельной проверки идентичности и подтверждения, что она больше не
нужна.

Автоматические сценарии используют тестовую PostgreSQL и loopback SMTP/mock, без внешней
отправки; см. [тестовое окружение](test-environment.md).
