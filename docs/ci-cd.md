# CI/CD и автоматическое восстановление FishSite

Рабочая схема: ветка → PR → проверка актуального diff и `CI required` → squash merge
в `main` → повторная проверка итогового SHA → GHCR → серверный pull-agent. После merge
отдельного подтверждения выпуска нет. GitHub runners не получают SSH, БД, SMTP, МойСклад
или ключ бота. Сервер сам обращается к GitHub/GHCR по HTTPS.

## Граница готовности

Код процесса находится в `.github/workflows/`, `deploy/agent/` и `deploy/production/`.
Наличие файлов не означает, что первый выпуск выполнен и timer включён.
До включения нужны все условия раздела «Первый выпуск».

25.10.2026 владелец согласовал вариант **без GitHub Pro**, с приватным репозиторием и без
обязательного второго reviewer. `repository_policy: private-free` выбирается явно в хостовом
конфиге. В GitHub нет технического запрета direct/force push и нет обязательного review/CI
перед merge. Процесс работы через PR соблюдает владелец; сервер независимо проверяет допуск
к выпуску. Это осознанное отличие от первоначального требования защиты main, не её эмуляция.
Режим `protected` сохранён для будущего перехода на платные rulesets; ошибка API никогда
автоматически не переключает его в `private-free`.

25.10.2026 проверено: приватный `FrankHeels/Ribeshka` возвращает HTTP 403 при обращении
к rulesets с требованием GitHub Pro. Публичность репозитория ради обхода ограничения не меняется.
Сервер `186.246.28.83`, amd64, обслуживает `https://ribeshka39.ru/`. До включения агента он
работал на локально собранном `486a60fb267614ca5a1b629e2b52ce4c106562c5`.
Production-файлы и исторические протоколы перенесены с сервера в Git без env и ключей.
App `fishsite-rollback-frankheels` (ID `5470359`, installation `175227037`) установлен только
в `Ribeshka`; ключ перенесён на VPS с режимом `0600`, подключение проверено. Первая проверенная
baseline `b3617546fe4d5274ac457c9f59ec39232e7e574a` установлена 25.10.2026, timer включён.
Результаты и границы проверки — в [протоколе запуска](ci-cd-acceptance-2026-10-25.md).

## GitHub

`ci.yml` проверяет каждый PR, без фильтра путей. `verify.yml` запускает:

1. Сценарии состояния выпуска/восстановления и проверку происхождения артефактов.
2. Проверку совместимости новых миграций.
3. Существующие Django-тесты на отдельном `postgres:18.6`: loopback `15432`, отдельный
   cluster_name, несуперпользователь `fishsite_tester` с CREATEDB, отдельная `test_fishsite`.
   Существующие guards БД/сети не ослаблены. Синтетические пароли находятся в CI открыто;
   production secrets в job отсутствуют. Тестовая БД удаляется после job.
4. `makemigrations --check --dry-run`, существующие JS-тесты, сборку `linux/amd64`.

`CI required` зелёный только при успехе всего reusable workflow, включая Docker build.
`release.yml` после каждого push в main повторяет проверки, собирает итоговый SHA, публикует
`ghcr.io/frankheels/ribeshka:sha-<40-символьный SHA>` и manifest с digest. Тег служит указателем;
сервер всегда использует `image@sha256:…`. Actions и базовый Docker-образ закреплены SHA/digest.
`release-<SHA>-<run_attempt>` хранится 90 дней. Сервер проверяет repository, event `push`,
ветку main, conclusion, SHA, номер попытки, digest ZIP и все поля manifest. Неудачный новый
rerun отменяет пригодность предыдущего успешного запуска того же SHA.

Перед получением образа сервер проверяет, что SHA — именно `merge_commit_sha` закрытого,
слитого PR в main этого репозитория, а последняя попытка `ci.yml` для сохранённого head SHA
этого PR успешна. Затем проверяется успешный `release.yml` для итогового SHA.
Связь с CI задаётся точным head SHA, исходной веткой и репозиторием; GitHub после merge
может очистить `workflow_run.pull_requests`, поэтому пустой массив сам по себе не означает отказ.
Для всех новых коммитов по первой родительской цепочке от работающего релиза требуется такая же связь с PR:
прямой push нельзя скрыть последующим корректным merge. Переписанная или слишком длинная
история блокирует выпуск. В GitHub включён squash merge; rebase merge не поддерживается.
После прямого push требуется разбор и исправление истории оператором, агент не делает force push.

Исключение доступно только во время уже записанного recovery: сохранённый revert SHA,
push от настроенного Bot, единственный родитель, точный `FishSite-Restore`, ancestry и полное
совпадение дерева с рабочим релизом. Одна строка в сообщении коммита не даёт обхода PR.

Создай отдельный private GitHub App, установи только в этот репозиторий. Нужны repository
permissions: Contents write, Workflows write (откат может возвращать workflow-файлы), Actions read,
Commit statuses write, Administration read, Pull requests read. Webhook выключен. Рабочий private key
на сервере имеет режим `0600`; агент через OpenSSL получает installation token на час. Для pull приватного
GHCR настрой отдельный read:packages credential через `docker login --password-stdin`, в
`DOCKER_CONFIG=/etc/fishsite/docker`. Не используй персональный токен владельца для обхода main.

Для согласованного варианта без Pro:

```sh
python3 deploy/configure_github.py --policy private-free --app-id 5470359 --bot-login 'fishsite-rollback-frankheels[bot]' --dry-run
python3 deploy/configure_github.py --policy private-free --app-id 5470359 --bot-login 'fishsite-rollback-frankheels[bot]'
```

Команда включает только squash merge и записывает `ROLLBACK_BOT_LOGIN`; rulesets не создаёт.
Ревью кода выполняется перед merge, формального одобрения второго пользователя не требуется.

Для отдельного перехода на `protected` нужны платный тариф, второй reviewer и явное изменение
хостовой политики. С `--policy protected` создаются два active ruleset для `main`: запрет удаления/force push без исключений и
PR + одно одобрение + сброс старых одобрений + одобрение последнего push другим человеком +
разрешение обсуждений + актуальный `CI required` от GitHub Actions. Во втором ruleset только
rollback App может пройти без PR/CI. Даже бот не может переписать историю. Администраторы
не добавляются в bypass. Единственный автор репозитория не может одобрить свой PR:
нужен второй уполномоченный reviewer. Бот предназначен для аварийного revert, не ревью.

## Серверный процесс

Хостовая копия агента находится в `/opt/fishsite/deployer`, независимо от образа приложения.
Её исходники и Compose устанавливаются оператором из проверенного commit; релиз не может
подменить процесс с доступом к Docker socket. Изменения самого агента, сетей, секретов или
Nginx-конфига требуют отдельного обновления установленной хостовой конфигурации.

Timer раз в 30 секунд после завершения предыдущего запуска вызывает `python3 -m deploy.agent once`.
Все деплои, backup/import и Certbot reload используют `/opt/fishsite/shared/operations.lock`.
Нет webhook listener, SSH из Actions или self-hosted runner, исполняющего PR на VPS.

Порядок обычного выпуска:

1. Проверить выбранную хостовую политику, связь с merged PR и CI, выбрать успешно проверенный текущий head. Готовые старые релизы
   пропускаются, если main уже ушёл вперёд. Проверить полный diff от реально работающего SHA,
   включая миграции пропущенных merge. Слишком большой/неполный API diff блокирует выпуск.
2. Pull по digest, проверка revision label, архитектуры и `USER app`; подготовка отдельного
   каталога статики. Если за это время main изменился, выбрать его на следующем проходе.
3. Сохранить durable state и список действительно запущенных workers. Остановить Nginx,
   дождаться остановки web и этих workers. МойСклад не включается, если до выпуска он не работал.
4. Создать согласованную пару custom-format `pg_dump` + media, migrations list, manifest с
   размерами, версиями PG и SHA-256 файлов/media. Проверить dump и checksums, зашифровать age
   публичным recipient из существующего файла. Приватный age-ключ остаётся вне VPS.
5. Выполнить миграции через `fishsite_owner` с ограниченными lock/statement timeouts;
   runtime web/worker остаётся `fishsite_runtime`. Собрать статику в каталог нового SHA.
6. Пересоздать web и прежние workers, затем Nginx, чтобы обновились upstream IP и static mount.
   Проверить контейнеры, отсутствие перезапуска worker, HTTPS `/healthz/`, главную, опубликованную категорию каталога,
   manifest статики и checksum CSS. Redirect не считается успехом; TLS проверяется обычным
   доверенным клиентом, без `-k`.
   `catalog_path` в конфигурации задаёт существующую категорию: отдельной страницы `/catalog/`
   у FishSite нет. При намеренном удалении контрольной категории сначала обнови этот путь.
7. Записать current/previous, SHA/digest и статус. Сохранить предыдущий образ и статику.

Это выпуск с коротким периодом недоступности, а не zero-downtime. Во время backup/миграций
Nginx остановлен. Если main обновится уже после начала выпуска, текущий выпуск заканчивается,
а следующий обрабатывается отдельно. Одновременных миграций/переключений не будет.

Копии находятся в `shared/deployer/backups/`. `backup_upload_command`, если задан,
вызывается как argv без shell с путём зашифрованного файла и checksum. Он должен подтвердить
запись и контрольное чтение удалённой копии; его ошибка блокирует миграции. На этапе настройки
пустой массив означает только локальную зашифрованную копию. Это не выполняет требование
постоянного внешнего backup из [контракта](backup-restore.md). Постоянное внешнее хранилище,
retention и независимые аварийные уведомления всё ещё нужно настроить до постоянной эксплуатации;
предыдущий тестовый запуск имел отдельное ограниченное исключение. Сетевая передача внутри
деплоя продлевает окно обслуживания; для большого объёма требуется отдельный проверенный uploader.
Успех следующей выгрузки никогда не удаляет последнюю пригодную копию автоматически.

## Миграции и аварийный откат

Первый этап использует строгую положительную политику: новые таблицы со стандартными scalar
полями, nullable scalar AddField без unique/primary_key и обычные AddIndex. Изменение/удаление
старых миграций, Rename/Alter/Remove, RunPython/RunSQL, custom imports, squashes, ограничения
на старых столбцах и новые FK запрещены. Python `default=` не заменяет SQL DEFAULT для старого
кода. Некоторые безопасные случаи намеренно тоже требуют отдельной процедуры. Смысловую
совместимость поведения приложения дополнительно проверяет reviewer.
Перед `migrate` также проверяется фактический план Django, включая миграции обновлённых
сторонних пакетов. Первичное создание пустой БД со всей историей миграций — отдельный bootstrap,
не обычное автоматическое обновление.

При сбое backup, миграции, статики, запуска или smoke:

1. Состояние переходит в recovery; новые релизы заблокированы. После падения/перезагрузки агент
   продолжает восстановление по atomic state, а не считает оборванный выпуск успешным.
2. Остановить возможный оставшийся migration-контейнер и writers. Вернуть previous image,
   static, web, прежние workers и Nginx; подтвердить их здоровье.
   После прерывания эта локальная часть выполняется до получения GitHub App token:
   недоступность GitHub не мешает вернуть сохранённый рабочий образ.
3. Бот создаёт новый commit с деревом последнего рабочего SHA и родителем текущего main.
   Это **revert всего ещё не принятого диапазона**, включая второй merge, пришедший во время
   неудачного выпуска. История сохраняется; перенести отменённый PR повторно можно после разбора.
   Ref обновляется только fast-forward; гонка с новым merge приводит к повторному чтению head.
4. Дождаться зелёного Release для revert SHA, проверить совпадение его дерева со старым релизом,
   установить пересобранный образ этого SHA без повторных/обратных миграций. Только после smoke
   разрешить следующие деплои. Пока CI/GitHub недоступен, старый рабочий сайт остаётся запущенным,
   а recovery сохраняет блокировку.

Состояния `reverted_runtime`, `reverting_main` и `awaiting_recovery_ci` означают, что старое
приложение уже восстановлено. Каждый повтор сначала проверяет его здоровье; исправные контейнеры
не останавливаются заново из-за ошибки GitHub. Если проверка не проходит, локальное восстановление
повторяется до работы с GitHub. Образы и статику предыдущего релиза нельзя удалять вручную.

БД из копии автоматически не восстанавливается. Дополнительные совместимые столбцы/таблицы
остаются, записи `django_migrations` сохраняются. Durable migration ledger запрещает повторно
использовать уже применённое имя миграции с другим содержимым после revert. Ручной restore БД
всегда отдельная процедура с анализом новых заявок и outbox.

## Первый выпуск

1. Проверить изменения PR, получить зелёный CI, создать App/GHCR read credential и выполнить
   настройку GitHub для `private-free`. Rulesets и второе одобрение в этом режиме не требуются.
   Не включать timer с заглушками ID или до создания первой проверенной baseline.
2. Установить проверенную хостовую копию в `/opt/fishsite/deployer`; сохранить старый каталог
   релиза и его Compose/env/static для первоначального rollback. Не заменять production checkout
   произвольным текущим working tree. Установить `age`, Python 3, OpenSSL, Docker/Compose.
3. Создать `/etc/fishsite/deployer.json` из примера, режим `0600`. Указать `repository_policy: private-free`,
   точный `rollback_bot_login`, App ID, installation ID
   и пути. Перенести проверенные POSTGRES_IMAGE/NGINX_IMAGE с digest в `shared/secrets/images.env`;
   приложение и static агент задаёт отдельно. Рабочие runtime/migration/db env остаются на сервере.
4. После merge дождаться успешного `release.yml`. Выполнить первоначальное переключение:

   ```sh
   cd /opt/fishsite/deployer
   sudo DOCKER_CONFIG=/etc/fishsite/docker python3 -m deploy.initial_release install \
     --legacy-compose /opt/fishsite/current/deploy/production/compose.yaml \
     --legacy-env /opt/fishsite/current/deploy/production/.env
   ```

   Команда берёт общий lock, сохраняет старые Compose/env, делает backup, миграции и static,
   переключает контейнеры, проверяет HTTPS и создаёт baseline только для текущего проверенного main.
   При ошибке возвращает старый Compose со старым static volume. После прерывания процесса повторный
   запуск сначала восстанавливает старый стек; явная команда — `python3 -m deploy.initial_release recover`.
   Рабочая БД из копии не восстанавливается. Старый каталог релиза и static volume сохраняй.
5. После успеха первоначального выпуска включи timer:

   ```sh
   cd /opt/fishsite/deployer
   sudo install -m 0644 deploy/systemd/fishsite-deploy.service /etc/systemd/system/
   sudo install -m 0644 deploy/systemd/fishsite-deploy.timer /etc/systemd/system/
   sudo install -m 0755 deploy/production/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/fishsite-nginx
   sudo systemctl daemon-reload
   sudo systemctl enable --now fishsite-deploy.timer
   ```

   Для уже вручную установленного проверенного образа можно вместо install выполнить
   `python3 -m deploy.agent bootstrap --sha <FULL_MAIN_SHA>`: команда только проверяет/adopts
   работающий релиз, не развёртывает его и не принимает локальный образ на веру.
6. Проверить следующий PR и два merge подряд. Отказные сценарии проводить в изолированной среде
   с синтетической БД и выключенными внешними интеграциями. Не ломать рабочий production ради теста.

## Наблюдение и ручное вмешательство

```sh
cd /opt/fishsite/deployer
sudo python3 -m deploy.agent status
sudo systemctl status fishsite-deploy.timer fishsite-deploy.service
sudo journalctl -u fishsite-deploy.service --since today
```

`shared/deployer/state.json` содержит phase, current/previous/candidate, SHA/digest, observed main,
backup receipt, recovery commit, активные workers и ledger. `current` — последний принятый релиз;
во время switching фактические процессы проверяются отдельно через `/healthz/` и Docker inspect.
В GitHub commit status `production` показывает phase и принятый SHA; он не является PR gate.
`blocked_reason` отдельно хранит этап и класс последней ошибки, например
`image preparation: OperationFailed`. Ошибка подготовки оставляет текущий рабочий релиз и
`phase=healthy`, но выставляет `production=error` с причиной блокировки и текущим SHA.
Ошибка восстановления сохраняет recovery-фазу и запрет новых выпусков. Тексты исключений,
ответы API и секреты в публичный статус не передаются. Поле `error` сохраняет класс исходного
сбоя выпуска; `blocked_reason` описывает препятствие на последней попытке продолжения.
Если GitHub недоступен, причина остаётся на диске, а публикация повторяется при следующем проходе.
После успешного прохода `blocked_reason` очищается: статус становится `pending`, если ещё ждём
проверенный релиз/восстановление, или `success`, когда работающий SHA совпадает с main.
Статус последнего выпуска не заменяет непрерывный мониторинг здоровья приложения.
HTTP `/healthz/` показывает SHA ответившего процесса и выполняет запрос к БД. Логи операций
доступны только root в `operations.log`; не пересылай их без проверки на чувствительные данные.
Старый `/opt/fishsite/current` сохранён как legacy checkout для первичного восстановления;
он больше не показывает работающую версию. Источник истины — `state.json`, `/healthz/` и digest
контейнеров. Хостовой агент установлен отдельно в `/opt/fishsite/deployer`.

Токен GHCR имеет только `read:packages`, хранится в `/etc/fishsite/docker/config.json` (`0600`)
и истекает **23.01.2027 в 19:28 UTC**. До этой даты замени его отдельным classic-токеном с тем же
scope через `docker --config /etc/fishsite/docker login ghcr.io --username FrankHeels --password-stdin`.
Ошибка pull блокирует новый выпуск до остановки работающего приложения.

Если recovery не завершается, сначала исправь указанную причину (registry, GitHub App, старый
контейнер, HTTPS, storage), затем повтори `python3 -m deploy.agent once`. Не удаляй state/lock,
не подставляй вручную `phase=healthy`, не запускай миграции мимо lock. Для обслуживания останови
timer и дождись завершения service. Остановка timer сама по себе не прерывает текущий деплой.
Обновление хостового агента: остановить timer, дождаться/завершить recovery, проверить новую
копию, заменить host checkout под общим lock, возобновить timer. Кандидат из PR не получает
доступ к Docker production до принятия.

Проверки реализации:

```sh
.venv/bin/python -m unittest discover -s deploy/tests -v
.venv/bin/python manage.py test tests --settings=fishsite.settings_test --noinput
.venv/bin/python -m deploy.ci.check_model_state
node --test tests/js/*.test.cjs
docker build --platform linux/amd64 -t fishsite:ci .
python3 -m deploy.ci.container_smoke fishsite:ci
```

Unit-сценарии моделируют успех, отказ CI, ошибку миграции, неработающее приложение, возврат
старого релиза, два merge, гонку revert, потерянный ответ GitHub, crash/restart и блокировку recovery.
Тесты настоящей CLI-точки входа с изолированным state проверяют недоступность App token при
прерванном деплое, повтор revert без перезапуска старого сайта, сохранение/публикацию ошибок,
недоступность API статусов и снятие блокировки после успеха. Docker/GitHub в них заменены
синтетическими адаптерами; production не затрагивается.
Отдельно проверяются отклонение прямого push (включая push перед корректным PR), неслитого PR,
чужого SHA/репозитория, неуспешного rerun, неизвестной политики и поддельного revert.
Они не заменяют отдельную проверку реального GitHub App и первичного запуска на VPS.

Официальные справки: [reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
[публикация GHCR](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images),
[repository rulesets](https://docs.github.com/en/rest/repos/rules),
[Git commits/refs](https://docs.github.com/en/rest/git/commits),
[Django migration operations](https://docs.djangoproject.com/en/5.2/ref/migration-operations/).
