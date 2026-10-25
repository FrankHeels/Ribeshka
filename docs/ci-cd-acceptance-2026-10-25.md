# Проверка и включение CI/CD 25.10.2026

Процесс включён на `186.246.28.83` для `https://ribeshka39.ru/`. Владелец согласовал приватный
GitHub Free без обязательного второго reviewer. Прямой/force push технически доступен владельцу;
сервер допускает только подтверждённые merged PR и зелёные проверки. Полный порядок эксплуатации
и восстановления находится в [CI/CD](ci-cd.md).

## Первый проверенный выпуск

- PR [#2](https://github.com/FrankHeels/Ribeshka/pull/2) добавил процесс; PR
  [#3](https://github.com/FrankHeels/Ribeshka/pull/3) исправил обработку GitHub API после merge
  и права каталога статики под закрытым umask серверного процесса.
- Первая baseline: `b3617546fe4d5274ac457c9f59ec39232e7e574a`.
- Образ: `ghcr.io/frankheels/ribeshka@sha256:9f5e5c653c76e68514da3ba30c20d258d632dad684db26daf52f36d022c366b1`,
  архитектура `linux/amd64`, пользователь `app`.
- [Release 40226917653](https://github.com/FrankHeels/Ribeshka/actions/runs/40226917653)
  повторно проверил итоговый commit, собрал и опубликовал образ и manifest.
- `initial_release` создал backup, проверил миграции через `fishsite_owner`, собрал статику,
  обновил web, ранее работавший notification worker и Nginx. МойСклад worker остался выключен.
- HTTPS `/healthz/`, главная, `/catalog/categories/kopchenaya-ryba/` и `/static/release.json`
  вернули HTTP 200; health и static manifest сообщили тот же SHA, checksum CSS совпал.
- `fishsite-deploy.timer` включён; первый запуск systemd успешно подтвердил `healthy` и равенство
  main/running SHA. GitHub commit status `production` — `success`.

Это исторический SHA первичного включения. Актуальный SHA всегда проверяется через `/healthz/`,
серверный state и `main`; следующие обычные merge меняют его автоматически.

## Проверенные сценарии

| Сценарий | Доказательство и граница проверки |
|---|---|
| Успешный выпуск | Реальные Release, GHCR pull по digest, первоначальный запуск на VPS и внешние HTTPS-проверки |
| Отказ CI | [Первый CI 40223734993](https://github.com/FrankHeels/Ribeshka/actions/runs/40223734993) завершился ошибкой, `CI required` был красным; merge выполнен после исправления и зелёного CI |
| Отказ миграции | Unit-сценарий возвращает старый runtime и блокирует новые выпуски; контейнерная проверка отдельно подтверждает отказ для неправильной DB-роли |
| Недоступность нового приложения | В disposable Docker smoke остановка web даёт отказ HTTPS; повторный запуск восстанавливает страницы. State machine проверяет переход в recovery после ошибки health |
| Автооткат | Unit-тесты проверяют runtime rollback, forward revert, ожидание recovery CI, потерю ответа API, гонки и restart. Реальный App создал commit с нужным деревом, обновил временную ветку с `force=false` и записал статус; временная ветка удалена |
| Два последовательных merge | PR #2 и #3 проверены сервером как полная цепочка от legacy SHA; unit-тест дополнительно проверяет последовательные переключения и блокировку второго выпуска до окончания recovery |
| Прямой push и поддельный revert | Unit-тесты отклоняют прямой commit, включая commit перед корректным PR, чужой SHA/ветку/repository, неуспешный rerun и чужого Bot |

Итого в проверках: **451 Django-тест, 5 JavaScript-тестов, 41 тест деплоя**, missing migrations,
сборка amd64 и изолированные PostgreSQL 18.6/HTTPS контейнеры. Production-секреты в GitHub CI
не передавались. Рабочий production намеренно не ломали для отказных сценариев; его БД из
копии не восстанавливали. Создание revert в main проверяется тестами состояния, а реальные
права App — на отдельной временной ветке.

## Резервная копия и доступ

Первый backup: `20261025T194317Z-0dcb0dc3`. Зашифрованная пара сохранена на VPS и перенесена
на Mac в `backups/deployment-20261025-cicd/20261025T194317Z-0dcb0dc3/` (вне Git).
Проверены checksum шифротекста, расшифровка существующим offline-ключом, все файлы manifest
и media. Dump восстановлен в одноразовый PostgreSQL 18.6 с `--network none`, без web/workers
и доступа к SMTP/МойСклад; проверены 79 записей истории миграций. Проверочный контейнер удалён,
незашифрованный dump на Mac не сохранялся. `verification.json` рядом с копией содержит результат.

App `fishsite-rollback-frankheels` (ID `5470359`, installation `175227037`) установлен только
в `FrankHeels/Ribeshka`. Ключ, config и Docker credential на сервере имеют права `0600`.
GHCR credential ограничен `read:packages`; срок и замена описаны в инструкции эксплуатации.
GitHub runners не получают SSH-доступ на VPS; новые задания сервер получает по исходящему HTTPS.

Постоянное внешнее хранилище backup, автоматическая внешняя выгрузка, retention и независимые
уведомления остаются отдельными условиями постоянной эксплуатации. Эта разовая копия на Mac
не заменяет их; данный протокол подтверждает включение CI/CD, а не выполнение всех требований
[общего контракта production](public-deployment-spec.md).
