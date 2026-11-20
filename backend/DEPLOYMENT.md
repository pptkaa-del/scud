# DEPLOYMENT — Развёртывание ScudDocs Backend в production

Документ для оператора. Описывает автоматическую установку и обновление
(`deploy.sh`), first-time deployment вручную, ротацию секретов,
backup/restore, disaster recovery, rolling restart и observability.

> Все команды предполагают, что вы находитесь в каталоге `backend/`
> (`cd backend`). Хост — Astra Linux SE 1.7+. Все docker-сервисы используют
> образы из `registry.astralinux.ru/library/astra/*` и пакеты только из
> сертифицированного `main` репозитория Astra.

## Содержание

1. [Автоматическая установка и обновление (deploy.sh)](#deploy-sh)
2. [Предварительные требования](#prerequisites)
3. [First-time deployment](#first-time-deployment)
4. [TLS termination](#tls-termination)
5. [Frontend deployment](#frontend-deployment)
6. [MQTT-брокер (Mosquitto)](#mqtt-broker)
7. [SMS-шлюз (Yeastar TG200 через WireGuard)](#sms-gateway)
8. [Backup strategy](#backup-strategy)
9. [Monitoring (Zabbix consumer-стек)](#monitoring-zabbix)
10. [Secret rotation](#secret-rotation)
11. [Disaster recovery](#disaster-recovery)
12. [Rolling restart](#rolling-restart)
13. [Масштабирование и ограничение single-replica](#scaling-single-replica)
14. [PgBouncer (transaction pooling)](#pgbouncer)
15. [Observability](#observability)
16. [Troubleshooting](#troubleshooting)

---

## Автоматическая установка и обновление (deploy.sh) <a id="deploy-sh"></a>

`backend/scripts/deploy.sh` ставит, обновляет и переустанавливает систему
целиком: секреты, `.env`, образы, схему БД, запуск и проверку. Скрипт — один
самодостаточный файл (bash 4+), в `--help` те же команды и опции. Разделы ниже
(«First-time deployment», «Обновление существующего развёртывания») описывают
то же вручную и остаются справочными.

### Требования к серверу

- Linux, bash 4+, Docker 24+, Docker Compose v2.20+ (нужен `up --wait`),
  `curl`, `openssl` (или Docker — секреты можно сгенерировать в контейнере).
  Запускать от root или от пользователя из группы `docker`.
- Если Docker не найден, а в системе есть `apt` (Astra/Debian), скрипт
  предложит `apt install docker.io docker-compose-v2`. На Astra перед этим из
  `/etc/apt/sources.list` убирается несертифицированный репозиторий `extended`
  (копия — `sources.list.deploy.bak`).
- От 20 ГБ свободного места и 8 ГБ ОЗУ — иначе предупреждение, не отказ.
- Свободны порт 80 (и 1883 / 8883, если выбраны плата-по-сети / TLS).
- Для сборки образов — доступ к `registry.astralinux.ru` и apt-зеркалу Astra.
  Без него соберите образы на другой машине (`pack --with-images`, ниже).

### Установка

Из git (достаточно скопировать на сервер один файл `deploy.sh`):

```bash
sudo ./deploy.sh install --git <repo-url> --ref main
```

Из архива релиза. На машине разработчика, из рабочей копии (ревизия берётся
из git, незакоммиченное и неотслеживаемое в архив не попадает):

```bash
./backend/scripts/deploy.sh pack --ref v1.1              # scud-v1.1-<sha8>.tar.gz
./backend/scripts/deploy.sh pack --ref v1.1 --with-images  # + images.tar с готовыми образами
```

На сервере:

```bash
scp scud-v1.1-1a2b3c4d.tar.gz server:
tar -xzf scud-v1.1-1a2b3c4d.tar.gz
sudo ./scud-v1.1-1a2b3c4d/deploy.sh install            # или: install --archive FILE
```

Каталог установки — `/opt/scud` (`--dir DIR`). Скрипт показывает итог
ответов, просит подтверждения, затем: готовит секреты и `.env`, собирает
образы (первая сборка 5–30 минут; с `images.tar` образы загружаются), поднимает
стек с `up -d --wait`, записывает журнал блоков схемы, проверяет `/health` и
вход администратора. В конце печатает адрес, телефон и пароль администратора
(пароль хранится в `backend/secrets/admin_password`) и что сделать дальше
(TLS-терминатор — раздел [TLS termination](#tls-termination)).

Прерванную установку можно продолжить: повторите `install` — незавершённое
состояние (`STATE=installing` в `deploy.conf`) распознаётся, и сохранённые
ответы (в том числе отказ от профилей `backup`, `mqtt-tls`, `monitoring`)
используются как умолчания. Если на сервере
уже есть тома `backend_postgres_*` без такого состояния, `install` отказывается
работать: используйте `update` или `reinstall`.

### Вопросы установки

Каждый вопрос имеет умолчание (Enter) и переменную окружения `SCUD_<ИМЯ>`.

| Вопрос | Умолчание при install | Переменная | Что меняет |
|---|---|---|---|
| Адрес сайта (`http(s)://хост[:порт]`) | `http://<IPv4 сервера>` | `SCUD_PUBLIC_URL` | `CORS_ALLOWED_ORIGIN`; `SECURE_COOKIES=true` только для `https` (для `http` — предупреждение) |
| IP/подсеть TLS-терминатора (через пробел) | `127.0.0.1/8` | `SCUD_TRUSTED_PROXY_CIDR` | `TRUSTED_PROXY_CIDR` ([SEC-07](#tls-termination)) |
| Режим `production` / `development` | `production` | `SCUD_APP_ENV` | `APP_ENV` (fail-closed секретов, см. §3a); `development` — для стендов |
| Телефон администратора | `+79000000000` | `SCUD_ADMIN_PHONE` | `ADMIN_PHONE` |
| Сгенерировать пароль администратора | да | `SCUD_ADMIN_PASSWORD_GENERATE`; готовый пароль — `SCUD_ADMIN_PASSWORD` | `secrets/admin_password`; спрашивается, только если секрета ещё нет; пароль от 8 символов, без пробелов, не `changeme`/`password` |
| Платы подключаются по сети (MQTT 1883 на всех интерфейсах) | да | `SCUD_MQTT_NETWORK` | `MQTT_BIND_ADDR=0.0.0.0` или `127.0.0.1`; при «да» ограничьте 1883 межсетевым экраном (см. [MQTT-брокер](#mqtt-broker)) |
| MQTT по TLS (8883) | нет | `SCUD_MQTT_TLS`; имена и IP для сертификата — `SCUD_MQTT_TLS_SAN` (умолчание — хост из адреса сайта) | профиль `mqtt-tls`, `MQTT_TLS_BIND_ADDR`, выпуск сертификатов `gen-mqtt-tls-certs.sh` |
| Токен и имя бота Max | пусто | `SCUD_MAX_BOT_TOKEN`, `SCUD_MAX_BOT_USERNAME` | `secrets/max_bot_token` (только если секрет пуст), `MAX_BOT_USERNAME`; пусто — OTP не доставляются (degraded) |
| SMS-шлюз Yeastar (WireGuard) | нет | `SCUD_SMS`; пользователь и пароль SMS API — `SCUD_SMS_API_USER`, `SCUD_SMS_API_PASSWORD` (пароль вводится без эха; любая однострочная строка, пробелы допустимы) | профиль `sms` в `PROFILES` (`deploy.conf`) добавляется, только если заполнены четыре секрета `sms_api_*`, `wg_*` и в `.env` заданы `WG_PEER_PUBKEY` и `SMS_GATEWAY_URL`; иначе скрипт перечисляет недостающее и ставит остальное без `sms`, см. [SMS-шлюз](#sms-gateway) |
| Ежедневные дампы БД в томе `backup_data` | да | `SCUD_BACKUP_DAILY`; срок хранения — `SCUD_BACKUP_RETENTION_DAYS` (7) | профиль `backup`, `BACKUP_RETENTION_DAYS` |
| Каталог дампов на хосте | `/var/backups/scud` | `SCUD_BACKUP_DIR` (или `--backup-dir`) | куда пишутся дампы перед обновлением |
| Мониторинг Zabbix | нет | `SCUD_MONITORING` | профиль `monitoring` |
| Итоговое подтверждение | да | `SCUD_CONFIRM` | — |

Да/нет — значения `y`/`n`. Секреты (26 файлов в `backend/secrets/`) создаются
через `openssl rand -hex`, кроме четырёх SMS-секретов (`sms_api_user`,
`sms_api_password`, `wg_private_key`, `wg_preshared_key`): они создаются пустыми
(пусто — SMS выключен), значения вносит оператор. Только отсутствующие или пустые: существующее
непустое значение не перезаписывается, известные dev-значения
(`*-dev-key-do-not-use-in-prod`, `changeme` и т.п.) отвергаются. Права:
каталог `secrets/` — 0700, файлы — 0644 (postgres читает bind-mount под своим
uid), `.env` — 0600. `REDIS_PASSWORD`, `MQTT_PASSWORD`, `ACM_DB_PASSWORD`
дублируются в `.env` из секретов (redis берёт пароль из `.env`).

### Без вопросов

`-y` / `--yes` принимает умолчания и сохранённые ответы; переменные `SCUD_*`
перекрывают умолчания. Вопрос без допустимого умолчания (например, ввод
оборван) — ошибка, а не молчаливый выбор.

```bash
SCUD_PUBLIC_URL=https://scud.example.com SCUD_TRUSTED_PROXY_CIDR=10.0.0.5 \
SCUD_APP_ENV=production SCUD_MAX_BOT_TOKEN=... SCUD_MAX_BOT_USERNAME=scud_bot \
    sudo -E ./deploy.sh install --archive scud-v1.1-1a2b3c4d.tar.gz -y
```

Дополнительно: `SCUD_DIR`, `SCUD_GIT_URL`, `SCUD_REF` — умолчания для `--dir`,
`--git`, `--ref`; `SCUD_KEEP_SECRETS`, `SCUD_KEEP_CONFIG`, `SCUD_UPDATE_CODE`
(reinstall); `SCUD_INSTALL_DOCKER` (ставить ли Docker через apt);
`SCUD_LEGACY_OK` (подтверждение параметров найденной ручной установки);
`SCUD_MENU` (пункт меню). Переустановка без вопросов требует явного
`SCUD_CONFIRM_WIPE=УДАЛИТЬ` — одного `-y` недостаточно.

### Команды

| Команда | Что делает |
|---|---|
| `deploy.sh` (без команды) | меню; по умолчанию `update`, если установка найдена, иначе `install` |
| `install` | чистая установка на новый сервер |
| `update` | обновление с сохранением данных |
| `reinstall` | переустановка с нуля: дамп → удаление томов → установка |
| `rollback` | вернуть предыдущую версию кода и образов; БД не меняется |
| `backup` | разовый дамп всех БД в `$BACKUP_DIR/<UTC>-manual/` |
| `migrate` | только применить блоки схемы БД (`--baseline` — только отметить в журнале) |
| `status` | версия, профили, состояние сервисов, блоки схемы, последний дамп |
| `pack` | собрать архив релиза (на машине разработчика, из git-копии) |

Опции: `--dir DIR`, `--git URL`, `--ref REF`, `--archive FILE`, `-y`,
`--reconfigure`, `--no-build`, `--skip-backup`, `--backup-dir DIR`,
`--baseline`, `--with-images`, `-o FILE`, `-h`. Если скрипт запущен из
существующей установки (`/opt/scud/backend/scripts/deploy.sh`), `--dir` не
нужен.

### Обновление

```bash
sudo /opt/scud/backend/scripts/deploy.sh update --ref v1.1       # git
sudo ./scud-v1.2-5e6f7a8b/deploy.sh update --dir /opt/scud       # архив (или update --archive FILE)
```

Вопросов нет, кроме подтверждения (`--reconfigure` заново задаёт параметры,
текущие значения — умолчания). Порядок:

1. Проверки; подтверждение.
2. Защита от старых БД: нужны роль `acm_ro`, колонки `security_events.event_uid`
   и `lockers.relay_id`, шесть отдельных `postgres_*_password`. БД, созданная
   версией старше 2026-06-17, автоматически не обновляется — ручной перенос или
   `reinstall`.
3. Дамп всех БД и копия конфигурации в `$BACKUP_DIR/<UTC>-pre-update/`. Не
   удался — обновление останавливается, ничего не изменено (`--skip-backup`
   отключает дамп осознанно).
4. В `deploy.conf` записывается `STATE=updating`, текущие образы получают тег
   `:rollback`, прежняя версия запоминается (`PREV_VERSION`). Повтор прерванного
   `update` (`STATE=updating`) эти метки и `PREV_VERSION` не трогает: откат
   по-прежнему ведёт к версии до первой попытки. Если `docker compose config
   --images` не дал ни одного образа проекта, обновление останавливается.
5. Новый код: `git fetch` + checkout `--ref` (по умолчанию сохранённая
   ревизия; изменённые отслеживаемые файлы — отказ; если `git fetch` не
   удался, но ревизия уже есть в локальном клоне, — предупреждение и работа с
   ней) либо распаковка архива
   (файлы, исчезнувшие из релиза, удаляются; `.env`, `secrets/`,
   `docker-compose.override.yaml`, сертификаты MQTT и любые локальные файлы
   не трогаются). Если новый `deploy.sh` отличается, обновление продолжает уже
   он. Установка из архива без нового `--archive` переразвёртывается на
   текущей версии (перенастройка, перезапуск) без пересборки образов;
   `--ref`/`--git` для неё не действуют — нужен архив нужной версии.
6. В `.env` дописываются новые ключи **со значениями, которые уже действовали**
   (например `FRONTEND_PATH=/root/frontend`, `LOGIN_RATE_LIMIT=100`,
   `APP_ENV=development`) — поведение не меняется молча; присутствующие ключи
   не трогаются. Слабые значения (`APP_ENV` не `production`,
   `CORS_ALLOWED_ORIGIN=*`, dev-секреты) — предупреждения; dev-секрет, из-за
   которого production-сервис не стартует, — ошибка до остановки сервисов.
   Сертификаты MQTT TLS выпускаются, только если `server.pem` ещё нет в
   `MQTT_TLS_CERT_DIR` (по умолчанию `backend/mqtt-tls/certs`): уже выпущенные
   не перевыпускаются, и `ca/ca.key` для `update` не нужен.
7. Сборка образов (или `docker load` из `images.tar`) **до** остановки.
8. Остановка 7 API и шлюза (иначе шлюз без API становится unhealthy), перезапуск `postgres_user` (контейнер видит новые
   `init-user-db.sh` и секреты только после перезапуска), применение блоков
   схемы (см. ниже), синхронизация роли `acm_ro` (скрипт с хоста передаётся в
   контейнер через stdin), запуск `history_api`; контейнеры сервисов профилей,
   выключенных в `deploy.conf` (например, после `--reconfigure`),
   останавливаются и удаляются; затем запуск всего стека (`--remove-orphans`).
9. Проверка `/health`; предупреждения: команды старше 2 минут при первом старте
   `locker_api` переходят в `failed` (TTL), шкафы показываются offline до
   следующего stat (до ~5 минут).

Простой — от остановки API до их запуска. Если сбой случился после остановки,
скрипт печатает варианты: повторить `update` или `rollback`, и путь к дампу.

### Откат

```bash
sudo /opt/scud/backend/scripts/deploy.sh rollback
```

Возвращает код (git-коммит или сохранённый релиз из `.deploy/releases/`, там
три последних) и образы `:rollback`, поднимает стек без пересборки. БД не
трогается: блоки схемы аддитивны, прежние образы новые колонки игнорируют
(исключения — в разделе «Обновление существующего развёртывания»). Откат
доступен **один раз** после успешного `update`; затем сохранённая версия
сбрасывается. Данные на момент до обновления — из дампа (скрипт печатает
команду восстановления, подробности — [Disaster recovery](#disaster-recovery)).

### Переустановка с нуля

```bash
sudo /opt/scud/backend/scripts/deploy.sh reinstall [--ref REF | --archive FILE]
```

Удаляет **все** данные: тома БД, redis, `backup_data` (в том числе ежедневные
дампы), `mqtt_config` (пользователи плат `relay-*` — платы придётся
провизионить заново). Подтверждение — ввести слово `УДАЛИТЬ`. Перед удалением
делается дамп в `$BACKUP_DIR/<UTC>-pre-reinstall/`. Затем вопросы: сохранить
секреты (да; при «нет» старые переносятся в `backend/secrets.old-<время>`, а не
удаляются, токен бота Max сохраняется, пароль администратора спрашивается
заново), сохранить параметры `.env`/`deploy.conf` (да), обновить код (нет, если
не указан `--ref`/`--archive`). Новый код получается и проверяется **до**
удаления томов: при ошибке `git fetch` или негодном архиве данные остаются на
месте. Дальше — как при установке.

### Где что лежит

| Что | Где |
|---|---|
| Состояние скрипта | `backend/deploy.conf` (`SOURCE`, `GIT_URL`, `GIT_REF`, `PROFILES`, `BACKUP_DIR`, `PUBLIC_URL`, `VERSION`, `PREV_VERSION`, `TARGET_VERSION`, `STATE` …; 0600, в `.gitignore`) |
| Конфигурация | `backend/.env` (0600), `backend/docker-compose.override.yaml` (если есть, применяется автоматически — скрипт предупреждает) |
| Секреты | `backend/secrets/` (0700, файлы 0644), сертификаты MQTT — `backend/mqtt-tls/` |
| Служебное | `<каталог>/.deploy/`: `releases/` (3 последних релиза), `MANIFEST`, `rollback-images`, `lock` |
| Дампы перед изменениями | `$BACKUP_DIR/<UTC>-pre-update\|pre-reinstall\|manual/`: 6 файлов `<БД>-<время>.dump` и `config.tar.gz` (0600: `.env`, `secrets/`, `deploy.conf`, override, `mqtt-tls`); каталог 0700 |
| Ежедневные дампы (профиль `backup`) | том `backup_data` — отдельно от каталога выше |

Compose всегда запускается из `backend/` как `docker compose -p backend --profile …`
с профилями из `deploy.conf` (имя проекта `backend` задаёт имена томов
`backend_*`, на них опираются процедуры восстановления). `deploy.conf`,
`.env` и `secrets/` храните также вне сервера.

### Блоки схемы БД и журнал

`001_init.sql` выполняется только на пустой БД. Изменения для уже
развёрнутых БД оформлены **именованными идемпотентными блоками** внутри этого
же файла (`-- <имя>:begin` … `-- <имя>:end`, правила — `CONVENTIONS.md`,
«Изменения схемы для существующих БД»). Перечень — в таблице §6.

В каждой из 6 БД скрипт ведёт таблицу `deploy_schema_upgrades(name, checksum,
applied_at)`:

- имя и контрольная сумма блока совпали с журналом — блок пропускается
  (`= уже применён`);
- блока нет в журнале — выполняется `psql -v ON_ERROR_STOP=1` (без `-1`, один
  оператор — одна транзакция) и записывается (`+ применён`);
- контрольная сумма изменилась — блок выполняется повторно, запись
  обновляется (`~ изменился`);
- блока нет в журнале, но для него задана проба (`LEGACY_PROBES` в
  `deploy.sh`) и она показывает, что изменения уже есть в БД, — блок только
  записывается в журнал, не выполняясь (`+ отмечен как применённый без
  выполнения`). Так первое `update` установки без журнала не повторяет блоки
  с побочными эффектами на данных: сейчас это `sensor-type-states`
  (`scud_lockers`; проба — заполнены ли `states` у типа `intrusion`, последнего
  добавленного в блок), иначе он
  вернул бы удалённые администратором типы `net_type`/`error` и заново
  заполнил бы обнулённые `states`;
- при `install` все блоки только отмечаются как применённые (БД создана
  актуальным файлом); при ошибке блока скрипт останавливается, блоки
  идемпотентны — после устранения причины повторите запуск.

`deploy.sh migrate` применяет блоки без остального обновления (требует
запущенных postgres; сервисы не останавливает — остановите API сами, если
блок меняет используемые ими объекты). `migrate --baseline` только отмечает
блоки в журнале, не выполняя их; нужен редко — например, если DDL уже
применён вручную и повторный прогон нежелателен (обычно достаточно обычного
`migrate`: блоки идемпотентны). Старая таблица `schema_migrations` не
используется и не изменяется. `deploy.sh status` показывает у каждого блока
`применён` / `ожидает` / `изменён`.

### Если что-то пошло не так

| Симптом | Что делать |
|---|---|
| Не собираются образы | сборке нужен `registry.astralinux.ru` и apt-зеркало Astra; соберите архив с образами (`pack --with-images`) и ставьте `--archive`, либо `--no-build`, если образы уже есть |
| Сервисы не стали `healthy` | скрипт печатает `ps` и последние 50 строк журнала нездоровых сервисов; устраните причину и повторите команду |
| `git fetch` не удался | если ревизия есть в локальном клоне (например, после переноса через `git bundle`), скрипт предупреждает и продолжает с ней; иначе — нет доступа к репозиторию: `update --archive FILE` (установка перейдёт в режим архива) |
| Дамп не удался | обновление остановлено до изменений; проверьте место и права на `$BACKUP_DIR`, повторите или `--skip-backup` |
| Блок схемы завершился ошибкой | исправьте причину, повторите `update` или `migrate` |
| Обновление прервано после остановки API | повторите `update` (метки `:rollback` сохраняются с первой попытки), либо `rollback`; дамп — путь в сообщении; `status` показывает незавершённое обновление |
| «deploy.sh уже выполняется» | параллельный запуск (замок `.deploy/lock`) |
| Пропущена синхронизация `acm_ro` | `init-user-db.sh` этой версии без `SKIP_MIGRATIONS`; роль не затронута |

### Стенд, установленный вручную (`/root/scud`)

Каталог с `.env`, `secrets/` и git-клоном, но без `backend/deploy.conf`,
скрипт распознаёт как ручную установку: определяет источник кода, профили по
запущенным контейнерам проекта `backend`, адрес сайта из `CORS_ALLOWED_ORIGIN`,
показывает найденное и просит подтвердить.

```bash
sudo bash /tmp/deploy.sh status --dir /root/scud
sudo bash /tmp/deploy.sh update --dir /root/scud --ref backend-dev
```

(В клоне скрипта может ещё не быть — запускайте скопированный файл. После
обновления он лежит в `/root/scud/backend/scripts/deploy.sh`.) Что сохраняется: `.env`, `secrets/` (права
приводятся к 0700/0644), `docker-compose.override.yaml` (его `APP_ENV` и
`SECURE_COOKIES` имеют приоритет над `.env`; скрипт предупредит),
`FRONTEND_PATH=/root/frontend` (чтобы раздавать каталог из репозитория, задайте
`FRONTEND_PATH=/root/scud/frontend` в `.env`). Блоки схемы при первом `update`
выполнятся один раз (идемпотентно) и попадут в журнал; `sensor-type-states`
только отмечается, если его результат уже есть в БД (см.
«Блоки схемы БД и журнал»); роль `acm_ro` будет
синхронизирована. Если `git fetch` на стенде не работает — `update --archive`.

---

## Prerequisites

| Компонент                         | Минимальная версия | Комментарий                                                  |
|-----------------------------------|--------------------|--------------------------------------------------------------|
| Astra Linux SE                    | 1.8+               | host-OS; certified `main` apt репозиторий обязателен         |
| Docker Engine                     | 24.0+              | поддержка Compose v2 plugin                                  |
| Docker Compose                    | v2.20+             | синтаксис `deploy.resources` без Swarm, `--profile`          |
| OpenSSL                           | 1.1.1+             | генерация секретов                                           |
| Доступ к `registry.astralinux.ru` | —                  | pull базовых образов (postgres15, redis7015, python311)      |
| Сетевой outbound для `apt update` | опционально        | только если пересобираете образы                             |
| TLS-серт (wildcard или per-host)  | —                  | для production frontend; терминация на внешнем reverse-proxy |
| Свободное место на диске          | от 20 GB           | postgres volumes + backup volume + docker layers             |

Минимальный сайзинг хоста для пилота (1000 шкафов, ~10 операторов):

- 8 vCPU, 16 GB RAM, 100 GB SSD.

Сервисы по умолчанию резервируют:

| Сервис                                                               | mem_limit | cpus |
|----------------------------------------------------------------------|-----------|------|
| `locker_api`                                                         | 2G        | 2.0  |
| `user_api` / `auth_api` / `history_api` / `acm` / `notification_api` / `reports_api` | 1G        | 1.0  |
| 6 × `postgres_*`                                                     | 512M      | 0.5  |
| `mqtt` (Mosquitto-брокер)                                           | 256M      | 0.25 |
| `mqtt_tls` (TLS-терминатор, профиль `mqtt-tls`)                      | 256M      | 0.25 |
| `redis`                                                              | 256M      | 0.25 |
| `apache_gateway`                                                     | 512M      | 0.5  |
| `backup` (профиль `backup`)                                          | 256M      | 0.25 |

Итого: ~10 GB RAM, ~9 vCPU. С запасом на host-OS — 16 GB RAM достаточно.

---

## First-time deployment

> Ручной порядок на случай, когда `deploy.sh` использовать нельзя. Те же шаги
> (секреты, `.env`, сборка, запуск, проверка) автоматизирует
> [`deploy.sh install`](#deploy-sh).

### 1. Клонировать репозиторий

```bash
git clone <repo-url> scuddocs
cd scuddocs/backend
```

### 2. Подготовить `.env`

```bash
cp .env.example .env
```

Значение и назначение каждой переменной — в комментариях `.env.example`.
Для production обязательно задайте:

- `APP_ENV=production` — включает fail-closed секретов (§3a);
- `CORS_ALLOWED_ORIGIN=https://scud.example.com` — конкретный origin, `*` в
  production запрещён;
- `SECURE_COOKIES=true` — для HTTPS-фронта;
- `TRUSTED_PROXY_CIDR` — адрес или подсеть TLS-терминатора ([SEC-07](#tls-termination));
- `REDIS_PASSWORD` — то же значение, что в `secrets/redis_password`;
- `FRONTEND_PATH` — каталог `frontend/` из репозитория, например
  `/opt/scud/frontend` ([Frontend deployment](#frontend-deployment));
- `LOGIN_RATE_LIMIT`, `VERIFY_RATE_LIMIT` (рекомендуемо `10`) — compose-значения
  подняты ради integration-тестов;
- `MAX_BOT_USERNAME` — username Max-бота для invite-link;
- `ADMIN_PHONE` — по желанию (по умолчанию `+79000000000`).

Остальные секреты (`JWT_SECRET`, `INTERNAL_*_HMAC_KEY`, `MAX_BOT_TOKEN`,
`ADMIN_PASSWORD`, `POSTGRES_PASSWORD`) в production берутся из файлов
`secrets/` (§3); значения в `.env` — fallback для dev и должны совпадать с
файлами, если заданы. Общий `POSTGRES_PASSWORD` в production не используется:
у каждого из 6 postgres-инстансов свой admin-секрет (SEC-24).

`reports_api` (порт 8006, БД `scud_reports`) подходит со значениями compose по
умолчанию; необязательно настраиваются `REPORTS_STORAGE_PATH`,
`REPORT_LINK_TTL_SECONDS` (24 ч; после истечения файл удаляется, скачивание
отвечает `410`), `REPORT_WORKER_INTERVAL_S`, `REPORT_MAX_ATTEMPTS`,
`HISTORY_API_URL`. Новых HMAC-секретов ему не нужно, только
`postgres_reports_password`. Файлы отчётов можно перегенерировать из
history_api, поэтому backup тома `reports_data` необязателен; том должен
существовать и быть доступен на запись.

### 3. Подготовить Docker secrets

Сервисы читают секреты из `/run/secrets/<name>` (`shared.utils.read_secret()`),
с fallback на env; в production используйте файлы — env утекает через
`docker inspect` и crash-dump.

```bash
mkdir -p secrets && cd secrets

for f in jwt_secret internal_auth_hmac_key internal_otp_hmac_key \
         internal_max_binding_hmac_key internal_step_up_hmac_key \
         internal_history_hmac_key internal_user_read_hmac_key \
         internal_session_revoke_hmac_key; do
    openssl rand -hex 32 > "$f"
done

for f in redis_password mqtt_password acm_db_password pgb_admin_password \
         postgres_user_password postgres_locker_password postgres_history_password \
         postgres_auth_password postgres_notification_password postgres_reports_password; do
    openssl rand -hex 16 > "$f"
done

openssl rand -base64 24 > admin_password
echo -n '<MAX_BOT_TOKEN_OR_EMPTY>' > max_bot_token
for f in sms_api_user sms_api_password; do : > "$f"; done

cd ..
chmod 644 secrets/*
chmod 700 secrets
```

- Шесть `postgres_<svc>_password` должны быть **разными** (SEC-24).
- `admin_password` в `-base64`, чтобы его можно было ввести руками при логине.
- `max_bot_token` должен существовать до запуска `notification_api`; без токена
  (пустой файл) сервис стартует в degraded режиме: OTP копятся в outbox.
- `sms_api_user` и `sms_api_password` монтирует `notification_api` всегда, поэтому
  файлы должны существовать; пустые — SMS выключен (значения — в
  [SMS-шлюз](#sms-gateway)).
- `acm_db_password` должен существовать до первого старта `postgres_user`:
  `init-user-db.sh` создаёт из него роль `acm_ro` (SELECT-only).
- `mqtt_password` читают брокер и `locker_api`; значения совпадают.
- `pgb_admin_password` монтируют все шесть `pgbouncer_*` (stats-роль `pgb_admin`).
- Секреты профиля `monitoring` (`postgres_zabbix_password`, `zabbix_db_password`) —
  в [Monitoring](#monitoring-zabbix).
- Какие сервисы читают каждый `internal_*_hmac_key` — таблица в
  [Secret rotation](#internal-hmac-rotation).

> Не ставьте файлам секретов `chmod 600`: файл читается под uid процесса в
> контейнере (например, `postgres`), и контейнер уходит в crash-loop с
> `Permission denied`. Защита — права каталога `secrets/` (0700); тот же режим
> 644 — в командах ротации и Zabbix.

### 3a. Включить fail-closed: `APP_ENV=production` (ОБЯЗАТЕЛЬНО)

`read_secret(required=...)` отвергает слабые секреты **только** при
`APP_ENV ∈ {production, prod}`. Для `jwt_secret` и всех 7 `internal_*_hmac_key`
пустое значение, дефолт или dev-placeholder (`internal-*-dev-key-do-not-use-in-prod`,
`change-me-in-production-...`, значения из `scripts/seed-dev-secrets.sh`) дают
`RuntimeError` на старте. Без `APP_ENV` (compose подставляет `development`)
механизм **молча выключен** — самый частый и опасный misconfiguration.

`docker-compose.yaml` передаёт `APP_ENV=${APP_ENV:-development}` всем 7
backend-сервисам (`user_api`, `auth_api`, `locker_api`, `history_api`,
`notification_api`, `reports_api`, `acm`); `deploy.sh install` пишет
`APP_ENV=production` сам. Перед `up -d` проверьте: `docker compose config |
grep APP_ENV` — у каждого из 7 должно быть `production`.
`docker-compose.override.yaml` перекрывает `.env`. dev/CI стартуют без
`APP_ENV` намеренно.

### 4. Поднять сервисы

```bash
docker compose up -d --build
```

Первый запуск ~3–5 минут; прогресс — `docker compose ps`.

### 5. Verification

```bash
docker compose ps                    # у всех status (healthy)
curl -fsS http://localhost/health    # ok
curl -X POST http://localhost/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"phone":"+79000000000","password":"<значение_admin_password>"}'
# 200 OK + Set-Cookie
```

Если есть фронтенд ([Frontend deployment](#frontend-deployment)) — откройте
`https://<your-domain>/` и войдите.

### 6. Обновление существующего развёртывания (schema)

Init-скрипты (`001_init.sql`) применяются только на чистой БД. Изменения схемы
для уже развёрнутых БД лежат в тех же файлах **именованными идемпотентными
блоками** (`-- <имя>:begin` … `-- <имя>:end`) на месте объекта.
`deploy.sh update` и `deploy.sh migrate` применяют их сами, в порядке файла,
под журналом `deploy_schema_upgrades` (см. [Автоматическая установка и
обновление](#deploy-sh)) — вручную блоки выполняют только без скрипта.

| Блок | БД (инстанс) | Файл (от `backend/services/`) | Зачем |
|---|---|---|---|
| `sensor-type-states` | `scud_lockers` (`postgres_locker`) | `locker_api/app/locker_data/db/migrations/001_init.sql` | колонка `sensor_types.states` и дефолтные состояния датчиков |
| `outbox-dead-letter` | `scud_lockers` | то же | `outbound_history_events.locker_id`, `dead_lettered_at`, индексы outbox |
| `relay-photo-series` | `scud_lockers` | то же | таблица `relay_photo_series` (серии фото релейных плат) |
| `relay-control` | `scud_lockers` | то же | управление блоком реле: enum `relay_*`, `locker_relays`, колонки `lockers` |
| `media-security-event-uid` | `scud_history` (`postgres_history`) | `history_api/app/history_data/db/migrations/001_init_history.sql` | `locker_media.security_event_uid` и индекс |
| `outbound-max-body-nullable` | `scud_notifications` (`postgres_notification`) | `notification_api/app/notification_data/db/migrations/001_init.sql` | SEC-18 |
| `outbound-max-channel` | `scud_notifications` | то же | колонки `channel`/`address` для SMS-доставки OTP |

В `scud_users`, `scud_auth`, `scud_reports` блоков нет.

Ручное применение одного блока (из `backend/`, **без `-1`**: значения enum,
добавленные `ALTER TYPE … ADD VALUE`, нельзя использовать в той же
транзакции). Команда ничего не пишет в журнал `deploy_schema_upgrades`: потом
`deploy.sh migrate` выполнит блок ещё раз (безопасно) или отметьте его через
`migrate --baseline`:

```bash
apply_block() {  # apply_block <postgres-сервис> <БД> <файл> <блок>
    sed -n "/^-- $4:begin\$/,/^-- $4:end\$/p" "$3" \
        | docker compose exec -T "$1" psql -U admin -d "$2" -v ON_ERROR_STOP=1
}
apply_block postgres_notification scud_notifications \
    services/notification_api/app/notification_data/db/migrations/001_init.sql outbound-max-body-nullable
```

Выполняйте после обновления дерева до ревизии релиза: если маркеров блока в
файле нет, команда молча ничего не делает.

Блоки выполняются до пересборки сервиса, который их использует. Если выкатка
охватывает несколько изменений (например, обновление сразу до `backend-dev`),
порядок общий: сначала все блоки, затем `history_api` (дождаться `/ready`),
затем `locker_api` (`deploy.sh update` делает именно так). Новый `history_api` и
`locker_api`, собранные из дерева с блоками, без них работают с ошибками.

Заметки по конкретным релизам (проверки, откат) — в `CHANGELOG.md`, записи
«Обновление существующих установок».

---

## TLS termination

Apache gateway сам по себе **HTTP-only**: терминируется на 80 порту,
никакого `mod_ssl`/443 не настроено. Для production обязательно поставить
TLS-терминатор перед `apache_gateway` — nginx, HAProxy или Traefik с
wildcard- или per-host сертификатом.

### Минимальный nginx-конфиг

```nginx
server {
    listen 443 ssl http2;
    server_name scud.example.com;

    ssl_certificate     /etc/letsencrypt/live/scud.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/scud.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # Стандартные REST-запросы → gateway:80.
    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}

server {
    listen 80;
    server_name scud.example.com;
    return 301 https://$server_name$request_uri;
}
```

> Важно: `SECURE_COOKIES=true` (auth_api) выставляет access-cookie с
> флагом `Secure` — браузер не отправит её по HTTP. Если внешний
> reverse-proxy завершает TLS, а внутренний `apache_gateway` слушает
> HTTP, **обязательно** проксируйте `X-Forwarded-Proto: https`, иначе
> auth_api увидит схему как HTTP и cookie не сохранится.

### Доверенный хоп терминатора и реальный клиентский IP (SEC-07)

`apache_gateway` определяет настоящий IP клиента через **mod_remoteip**
и заголовок `X-Real-IP`, который ставит терминатор
(`proxy_set_header X-Real-IP $remote_addr` выше). Этот IP уходит в
`auth_api` (Tornado `xheaders=True`) и используется как ключ
**rate-limit логина** (`LOGIN_RATE_LIMIT` — теперь на каждого реального
клиента, а не один общий бакет) и для аудита `access_tokens.ip_address`.

Чтобы Apache доверял `X-Real-IP` **только** от терминатора (а не от
любого клиента, который мог бы подделать заголовок и обойти/отравить
rate-limit), задайте адрес/подсеть терминатора через env-переменную
`TRUSTED_PROXY_CIDR` в `.env` сервиса `apache_gateway`:

```env
# Адрес или CIDR TLS-терминатора. Несколько — через пробел.
# Примеры:
TRUSTED_PROXY_CIDR=127.0.0.1/8          # nginx на том же хосте (дефолт)
# TRUSTED_PROXY_CIDR=10.0.0.0/8         # терминатор в приватной подсети
# TRUSTED_PROXY_CIDR=172.18.0.0/16      # отдельный nginx-контейнер в docker-сети
```

Поведение `mod_remoteip` (`RemoteIPHeader X-Real-IP` +
`RemoteIPInternalProxy ${TRUSTED_PROXY_CIDR}`):

- **За терминатором** (соединение пришло с доверенного хопа): Apache
  берёт клиентский IP из `X-Real-IP`, подменяет им `REMOTE_ADDR` →
  каждый клиент получает собственный rate-limit бакет.
- **Прямое (недоверенное) соединение**: `X-Real-IP` от клиента
  **игнорируется**, `REMOTE_ADDR` остаётся реальным IP клиента —
  подделать его нельзя.

> Если `TRUSTED_PROXY_CIDR` не охватывает адрес терминатора, Apache
> сочтёт сам терминатор клиентом: `X-Real-IP` будет проигнорирован и все
> запросы схлопнутся на IP терминатора (один общий rate-limit бакет —
> ровно та регрессия, которую закрывает SEC-07). Сверяйте CIDR с тем, с
> какого адреса терминатор реально подключается к gateway:80.

---

## Frontend deployment

`apache_gateway` отдаёт статику фронтенда + проксирует REST через
`ProxyPass`. Фронтенд лежит в репозитории, в каталоге `frontend/`
(статический SPA без сборки), и приходит вместе с кодом при `git clone`,
`deploy.sh install` и `update`. Каталог монтируется через переменную
`FRONTEND_PATH` в `.env`; для установки из репозитория она указывает прямо на
`frontend/`:

```env
FRONTEND_PATH=/opt/scud/frontend
```

`deploy.sh install` пишет `FRONTEND_PATH=<каталог установки>/frontend` сам;
`update` существующее значение не меняет (на старом стенде это
`/root/frontend` — чтобы перейти на каталог из репозитория, поправьте значение
в `.env` и выполните `update`). Без `FRONTEND_PATH` compose использует
`/root/frontend`.

Все запросы фронтенда к API идут на тот же origin (`/api/v1`), адрес сервера в
коде не прописан — один и тот же каталог работает за любым доменом и
TLS-терминатором.

Раздача **ролевая**: gateway определяет роль из session-cookie (авторитетно
через ACM `/acm/identity`) и отдаёт каждому пользователю только его бандл;
URL плоские, папки чужих ролей недоступны по пути; неаутентифицированный →
редирект на `/login.html`. Настоящая граница авторизации — `/api/v1` через
ACM. `FRONTEND_PATH` монтируется как **четыре read-only тома**:

```yaml
- ${FRONTEND_PATH}/public:/var/www/public:ro       # login.html, script.js, style.css, libs/ — общее
- ${FRONTEND_PATH}/worker:/var/www/roles/worker:ro     # бандл worker (нужен main.html)
- ${FRONTEND_PATH}/operator:/var/www/roles/operator:ro # бандл operator (нужен main.html)
- ${FRONTEND_PATH}/admin:/var/www/roles/admin:ro       # бандл administrator (нужен main.html)
```

Общие ассеты (login, `libs/`) кладутся в `public/`, а НЕ в корень
`FRONTEND_PATH`. Каждая ролевая папка обязана содержать `main.html`
(домашняя; переопределяется env `ROLE_HOME_<ROLE>`/`ROLE_HOME_DEFAULT`).
Тюнинг-ручки (env `apache_gateway`): `ROLE_CACHE_TTL_S`(5),
`ROLE_FAIL_TTL_S`(1), `ROLE_CACHE_MAX`(4096), `ROLE_ID_RATE_LIMIT`(60),
`ROLE_ID_WINDOW_S`(10). **Prod-обязательно**: `limit_req` на TLS-терминаторе
перед gateway (глобальный лимитер; Lua-throttle — лишь per-worker backstop) и
проброс заголовков `Sec-Fetch-*`.

### Шаги установки фронтенда

При установке `deploy.sh` или `git clone` + `FRONTEND_PATH=<клон>/frontend`
ничего копировать не нужно. Если фронтенд кладётся отдельно от репозитория:

1. Скопировать каталоги `public`, `worker`, `operator`, `admin` из `frontend/`
   в целевой каталог на хосте:

   ```bash
   sudo mkdir -p /opt/scud/frontend
   sudo rsync -av --delete frontend/public/   /opt/scud/frontend/public/
   sudo rsync -av --delete frontend/worker/   /opt/scud/frontend/worker/
   sudo rsync -av --delete frontend/operator/ /opt/scud/frontend/operator/
   sudo rsync -av --delete frontend/admin/    /opt/scud/frontend/admin/
   ```
2. Указать каталог в `FRONTEND_PATH` (`.env`).
3. Если каталоги заменялись целиком (а не обновлялись на месте), перезапустить
   gateway, чтобы он перечитал bind-mount:

   ```bash
   docker compose restart apache_gateway
   ```

Если каталог не существует или пуст — apache отдаст только REST через
ProxyPass, статика будет недоступна. Это допустимо для headless-API
сценариев.

---

## MQTT-брокер (Mosquitto) <a id="mqtt-broker"></a>

Релейные платы шкафов общаются с `locker_api` по MQTT, а не по WebSocket.
Брокер — собственный образ `scud-mqtt` (Mosquitto 2.0.23, собирается из
`backend/mqtt/`, `target: production`), сервис `mqtt` в `docker-compose.yaml`.
Протокол сообщений описан в
`backend/services/locker_api/openapi/openapi_relay.md`.

### Сетевой контур

- Слушает `1883/tcp`, публикуется **только на loopback**:
  `${MQTT_BIND_ADDR:-127.0.0.1}:1883:1883`. Backend ходит к брокеру по
  `backend_net` (`mqtt:1883`), host-порт нужен лишь для локальной отладки.
- **Не публикуйте `0.0.0.0:1883` в production** — это раскрывает брокер на LAN
  (INF-01 / SEC-04). `MQTT_BIND_ADDR=0.0.0.0` переопределяется только в
  `.env.fast` для system-e2e харнесса.
- Healthcheck — read-only TCP-проба listener'а (не трогает passwordfile).
- `locker_api` зависит от `mqtt` через `depends_on: condition: service_healthy`.
- **TLS-листенер `8883`** обслуживает отдельный sidecar `mqtt_tls`
  (haproxy 2.6.23 из сертифицированного `main`, `backend/mqtt-tls/`): сам
  брокер собран `WITH_TLS=no`. Sidecar живёт за профилем `mqtt-tls` —
  обычный `docker compose up` его НЕ поднимает. Публикуется на
  `${MQTT_TLS_BIND_ADDR:-127.0.0.1}:8883`.
- Перед первым подъёмом sidecar'а выпустить сертификаты (от root, чтобы файл
  на bind-mount остался root-owned):
  `bash backend/scripts/gen-mqtt-tls-certs.sh --dns <имя> --ip <адрес> --ip 127.0.0.1`.
  В плату заливается `backend/mqtt-tls/ca/ca.crt`.
- PROXY protocol между sidecar'ом и брокером не используется — mosquitto 2.0.23
  его не поддерживает; все TLS-платы видны брокеру с IP контейнера haproxy,
  атрибуция идёт по username `relay-{id}`.

### Конфиг-том и provisioning

- `mosquitto.conf`, `passwordfile`, `aclfile` лежат на именованном volume
  `mqtt_config` (`/mosquitto/config`), доступном на запись и брокеру, и
  `locker_api`. Дефолты копируются из image ТОЛЬКО когда отсутствуют.
- При регистрации релейной платы `locker_api` дописывает в `passwordfile`
  пользователя `relay-{id}` и его ACL-блок (право `write` на `stat`/`rep`/`photos`,
  `read` только на свой `com/{id}`). Watcher в контейнере брокера хеширует
  изменения и шлёт `SIGHUP` для reload (~2 с). Подробности —
  `backend/services/locker_api/app/mqtt/provisioning.py`.
- Per-device ACL нельзя затирать: при пересоздании тома релейные платы
  потеряют доступ и потребуют повторного provisioning.

### Backend-учётка `scud_backend`

- `locker_api` подключается к брокеру под `scud_backend`; пароль — Docker secret
  `mqtt_password` (`read_secret("mqtt_password", env_var="MQTT_PASSWORD")` →
  env → dev-fallback).
- `entrypoint.sh` брокера на каждом старте перезаписывает строку `scud_backend:`
  в `passwordfile` из того же секрета, поэтому ротация секрета сводится к
  «перезаписать файл + перезапустить брокер» (см. раздел
  [Ротация MQTT-пароля брокера](#mqtt-broker-password-rotation)).

### Тюнинг offline-детекции

- `MQTT_OFFLINE_TIMEOUT_S` (default 350) — плата считается offline, если по ней
  не было `stat` дольше этого окна.
- `MQTT_OFFLINE_CHECK_INTERVAL_S` (default 30) — период фонового свипа в
  `locker_api`.

---

## SMS-шлюз (Yeastar TG200 через WireGuard) <a id="sms-gateway"></a>

`notification_api` доставляет OTP-коды по SMS через шлюз Yeastar TG200,
доступный только внутри WireGuard-туннеля (`10.8.0.0/24`). Sidecar
`wg_gateway` (`monitoring/wg-gateway/`) поднимает туннель и форвардит порт
шлюза в `backend_net` — только этот контейнер получает `NET_ADMIN` и
`/dev/net/tun`, `notification_api` обращается к нему как к обычному TCP-хосту
(`SMS_GATEWAY_URL=http://wg_gateway:7777`). Без профиля `sms` OTP уходит по
MAX (или в очередь) — SMS не является жёсткой зависимостью стека.

С профилем `sms` коды входа и подтверждения команд у всех пользователей с
включённой 2FA уходят SMS на телефон учётной записи, привязка MAX для этого не
нужна. Перед включением проверьте номера: у учёток с тестовыми или чужими
номерами исправьте телефон или выключите 2FA.

### Подъём профиля `sms`

Через `deploy.sh`: на `install` или `update --reconfigure` ответьте «да» на
вопрос «SMS-шлюз Yeastar (WireGuard)» (`SCUD_SMS=y`). Скрипт создаёт четыре
секрета пустыми (`sms_api_user`, `sms_api_password`, `wg_private_key`,
`wg_preshared_key`) и добавляет профиль `sms` только когда `wg_gateway`
способен запуститься: все четыре секрета непусты и в `.env` заданы
`WG_PEER_PUBKEY` и `SMS_GATEWAY_URL`. Если чего-то не хватает, установка или
обновление проходят без профиля `sms`, а скрипт печатает предупреждение с
именами недостающего (значения не выводятся). Тогда заполните секреты и `.env`
по шагам 1–2 ниже и выполните `deploy.sh update --reconfigure`, ответив «да» на
вопрос про SMS-шлюз (без вопросов: `SCUD_SMS=y deploy.sh update --reconfigure -y`):
скрипт поднимет `wg_gateway` и пересоберёт `notification_api`. Профиль `sms`,
уже записанный в `deploy.conf`, при незаполненных значениях пропускается только
на время запуска с тем же предупреждением; обычный `deploy.sh update` включит
его, как только всё заполнено.

`wg_gateway` не входит в ожидание `--wait`: его healthcheck требует handshake,
и недоступный пир не должен ронять `install`, `update` и `rollback`. Скрипт
запускает `wg_gateway` отдельно, до 90 с ждёт `healthy` и при неудаче печатает
предупреждение (SMS недоступны, остальной стек работает).

Шаги ниже — то же вручную.

1. Создать WG- и SMS-секреты (значения — из WireGuard-конфигурации,
   выданной администратором туннеля, и из настроек API шлюза; НЕ `openssl rand` —
   это не сгенерированные, а выданные значения).

   HTTP-CGI шлюза проверяет учётку из веб-интерфейса шлюза: **SMS → API-настройки**
   (не учётка администратора веб-интерфейса и не «SMS Account Setting»). Там же
   включите API и добавьте в список разрешённых IP адрес узла в туннеле
   (`WG_ADDRESS` без маски, маска `255.255.255.255`). Учётку по умолчанию
   (`apiuser`/`apipass`) замените своей.
   ```bash
   cd backend

   # Приватный ключ этого узла (WireGuard-конфиг стороны туннеля).
   printf '%s' '<wg_private_key>' > secrets/wg_private_key
   # Preshared key: сайдкару не обязателен (пустой файл = без PSK), но deploy.sh
   # включает профиль sms только при непустом файле.
   printf '%s' '<wg_preshared_key>' > secrets/wg_preshared_key

   # Учётка API шлюза Yeastar TG200 (SMS → API-настройки).
   printf '%s' '<sms_api_user>' > secrets/sms_api_user
   printf '%s' '<sms_api_password>' > secrets/sms_api_password

   chmod 644 secrets/wg_private_key secrets/wg_preshared_key \
             secrets/sms_api_user secrets/sms_api_password
   ```
2. Задать в `.env` параметры туннеля и адрес шлюза (см. таблицу переменных
   `wg_gateway` в `monitoring/wg-gateway/README.md`):
   ```env
   WG_ADDRESS=10.8.0.14/24
   WG_PEER_PUBKEY=<публичный ключ удалённого пира>
   WG_ENDPOINT=94.103.85.33:29156
   WG_ALLOWED_IPS=10.8.0.0/24
   SMS_GATEWAY_HOST=10.8.0.19
   SMS_GATEWAY_URL=http://wg_gateway:7777
   ```
3. Поднять `wg_gateway` и пересобрать/перезапустить `notification_api`,
   чтобы он увидел `SMS_GATEWAY_URL` и секреты:
   ```bash
   docker compose --profile sms up -d --build wg_gateway notification_api
   docker compose --profile sms logs -f wg_gateway   # "wg0 up; forwarding :7777 -> ..."
   docker compose exec wg_gateway wg show wg0 latest-handshakes   # свежий timestamp
   ```
   Профиль `sms` не стартует при обычном `docker compose up` — на dev/CI
   без туннеля это ожидаемо (OTP уходит по MAX/в очередь).

Колонки `outbound_max_messages.channel`/`address` приходят блоком схемы
`outbound-max-channel` (см. [§6](#6-обновление-существующего-развёртывания-schema)),
который применяют `deploy.sh update` и `deploy.sh migrate`.

### Таймауты цепочки доставки OTP

Доставка OTP синхронна: логин и step-up ждут ответа шлюза. Шлюз отвечает от
~3 с до ~15 с (при `Commit Timeout` SMS остаётся в очереди шлюза и всё равно
уходит). Параллельные отправки идут через FIFO-пейсер с интервалом
`SMS_MIN_INTERVAL_SECONDS` (каждая ждёт только свой слот); если ожидание слота
больше `SMS_MAX_QUEUE_WAIT_SECONDS`, SMS не отправляется и доставка сразу
переходит на MAX (или отдаёт 503). Инвариант таймаутов цепочки (значения по
умолчанию):

`SMS_MAX_QUEUE_WAIT_SECONDS=10` + `SMS_GATEWAY_TIMEOUT_SECONDS=25` +
`MAX_SEND_TIMEOUT_SECONDS=8` (= 43 с, худший случай notification_api) <
`OTP_DELIVERY_TIMEOUT_SECONDS=45` (auth_api → notification_api) <
`STEP_UP_ISSUE_TIMEOUT_SECONDS=50` (locker_api → auth_api) <
`ProxyPass ... timeout=60` для `/api/v1/auth` и `/api/v1/locker*` в
`gateway/apache.conf`. При увеличении внутреннего значения поднимайте внешние.
Эти POST на таймауте не повторяются, чтобы не отправить второй код.

### Split-tunnel — обязательный инвариант

`wg_gateway` — **split-tunnel**, не full-tunnel:
`WG_ALLOWED_IPS=10.8.0.0/24` (только подсеть шлюза). Никогда не
выставляйте `WG_ALLOWED_IPS=0.0.0.0/0` — full-tunnel завернул бы в туннель
весь трафик контейнера, включая путь управления стендом (`:443` внешнего
терминатора → `:22` SSH хоста), и при обрыве/переконфигурации туннеля
можно потерять доступ к хосту (lockout). Split-tunnel ограничивает blast
radius этого sidecar ровно одной сетью назначения — остальной трафик
`backend_net` (DNS, inter-service HTTP, `apt` при пересборке образов) его
не проходит.

---

## Backup strategy

Шесть PostgreSQL-серверов держат всё состояние системы. Потеря volume —
потеря данных. Стратегия: ежедневный логический бэкап (pg_dump custom
format) во внешний docker-volume `backup_data`, с TTL-ротацией.

### Архитектура

- Sidecar-контейнер `backup` (профиль `backup` — НЕ стартует автоматически).
- Бесконечный bash-loop запускает `/scripts/backup.sh` раз в
  `BACKUP_INTERVAL_SECONDS` (default 86400 = сутки).
- Скрипт делает `pg_dump -F c` каждой из 6 БД (`scud_users`, `scud_lockers`,
  `scud_history`, `scud_auth`, `scud_notifications`, `scud_reports`) и складывает в
  `/backups/<db>-<UTC-timestamp>.dump`. Список пар host:db — в `backend/scripts/backup.sh`.
- После каждого цикла удаляются дампы старше `BACKUP_RETENTION_DAYS`
  (default 7).

Файлы: `backend/scripts/backup.sh`, `backend/Dockerfile.backup`,
сервис `backup` в `docker-compose.yaml`.

> Замечание про cron: пакет `cron` не входит в сертифицированный `main`
> репозиторий Astra Linux, поэтому планировщик реализован обычным
> bash-loop'ом с `sleep`. Для нерегулярного расписания (например,
> raз в неделю по средам) переходите на `python3-croniter` (есть в `main`)
> или внешний `systemd timer` на хосте.

### Запуск backup-сервиса

Daemon-режим (бэкапы по расписанию):

```bash
docker compose --profile backup up -d --build backup
docker compose --profile backup logs -f backup
```

Разовый дамп (для CI / before-migration / smoke-test):

```bash
docker compose --profile backup run --rm backup /scripts/backup.sh
```

Список существующих дампов:

```bash
docker compose --profile backup run --rm backup ls -la /backups/
```

Разовый дамп прямо на хост (в `$BACKUP_DIR/<UTC>-manual/`, вместе с копией
`.env` и `secrets/`) делает `deploy.sh backup`; такой же дамп скрипт снимает
перед `update` и `reinstall` (см. [Автоматическая установка и
обновление](#deploy-sh)).

### Параметры (env, через `.env`)

- `BACKUP_RETENTION_DAYS=7` — TTL дампов в днях.
- `BACKUP_INTERVAL_SECONDS=86400` — период между запусками (default
  раз в сутки).
- `BACKUP_DIR=/backups` — путь внутри контейнера (volume `backup_data`).

### Размер volume

`backup_data` — обычный named docker volume. Расположен в
`/var/lib/docker/volumes/backend_backup_data/_data` (зависит от
docker-config). Для production:

- Замените на host-bind: `- /var/backups/scud:/backups` (тогда дампы
  попадают на host filesystem; rsync в offsite-storage становится
  тривиальным).
- Либо настройте `docker volume` поверх ZFS/btrfs со снапшотами.

### Off-site копирование (рекомендация)

Backup-сидекар сам по себе НЕ копирует дампы наружу — только локально на
docker-volume. Для real-prod добавьте cron-job на хосте:

```bash
# /etc/cron.d/scud-backup-offsite (на хосте, не в контейнере)
30 3 * * *  root  rsync -a /var/lib/docker/volumes/backend_backup_data/_data/ \
                  backup-server:/srv/backups/scud/$(date +\%F)/
```

или используйте `restic` / `borg` для дедупликации и шифрования.

### Restore

См. раздел [Disaster recovery](#disaster-recovery).

---

## Monitoring (Zabbix consumer-стек) <a id="monitoring-zabbix"></a>

Профиль `monitoring` поднимает `postgres_zabbix` + `zabbix_server` + `zabbix_web`
в той же `backend_net`, где живут backend-сервисы. `/metrics` каждого сервиса
доступен внутри сети; Apache gateway намеренно не проксирует его наружу.

### Подъём

1. Секрет БД Zabbix (prod):
   ```bash
   openssl rand -hex 32 > backend/secrets/postgres_zabbix_password
   chmod 644 backend/secrets/postgres_zabbix_password
   ```
   В dev `backend/scripts/seed-dev-secrets.sh` создаёт его автоматически.
2. Запустить профиль:
   ```bash
   cd backend && docker compose --profile monitoring up -d
   ```
   Web-UI: `http://127.0.0.1:8091` (Admin/zabbix по умолчанию — сменить пароль
   при первом входе).
3. Импортировать шаблон: см. `monitoring/zabbix/README.md` (автоматически через
   `provision.py` или вручную через UI).

### Zabbix agent2 (инфра-метрики, Этап 2) <a id="zabbix-agent2"></a>

Контейнер `zabbix_agent2` запускается в том же профиле `monitoring` — дополнительных
команд не требуется:

```bash
cd backend && docker compose --profile monitoring up -d
```

Контейнер работает **без привилегий** в режиме `active` (`ServerActive=zabbix_server`),
без публикуемых портов. Находится в `backend_net` и самостоятельно подключается к
`zabbix_server:10051` для отправки метрик.

**Что собирает:**

- **Redis** — единственный HTTP-item `scud.infra.redis.up` (gauge 0/1): acm-сервис
  пингует Redis каждые 15 с и пишет результат в свой `/metrics`; agent2 забирает его
  как зависимый item. Полный Redis-мониторинг (used_memory, ops/sec, connected_clients)
  недоступен — встроенный redis-плагин agent2 отсутствует в Astra-сборке 7.0.10
  (см. `monitoring/CERT_GATE.md` §A2-2).
- **PgBouncer ×6** — UserParameter `scud.dbprobe[pgb,<host>,<metric>]` → `dbprobe.sh`
  выполняет `SHOW STATS`/`SHOW POOLS` на `:6432 -U pgb_admin`. Метрики: `cl_waiting`,
  `sv_active`, `maxwait`, `avg_query_time`. Обнаружение хостов — LLD.
- **PostgreSQL ×6** — UserParameter `scud.dbprobe[pg,<host>,<metric>]` → `psql` к
  `pg_stat_*`. Метрики: `numbackends`, `max_connections`, `deadlocks`, `xact_rollback`,
  `longest_txn_seconds`, `history_partition_bytes` и `history_orphan_media` (только
  history-инстанс; второй до DDL из §6 отдаёт 0). Роль для
  подключения — **`admin`** (`POSTGRES_USER=admin`), не `postgres`.

**Монтируемые секреты:** `pgb_admin_password` и 6 секретов `postgres_<svc>_password`.
Redis-credentials не монтируются (плагин отсутствует — они не нужны).

Метрики MQTT-брокера (`scud_mqtt_connected`, `scud_mqtt_relays_online`,
`scud_mqtt_broker_*`) agent2 не собирает — `locker_api` уже публикует их
через `$SYS/broker/#` → `/metrics`, и services-шаблон Zabbix их скрейпит.

Перекрёстная ссылка: контракт `/live` / `/ready` / `/health` — см. раздел
[Контракт проб live / ready / health](#observability) ниже; Zabbix алертирует
по `/ready` (degraded/unavailable), docker healthcheck гейтируется по `/health`.

### Формат логов

Все 7 backend-сервисов читают переменные окружения `LOG_FORMAT` и `LOG_LEVEL`
через `shared.observability.configure_logging(service_name)`:

- `LOG_FORMAT=json` (по умолчанию) — структурированные JSON-строки; каждая строка
  несёт поле `request_id` (значение `X-Request-ID` ContextVar) для сквозной
  корреляции запросов между сервисами. Рекомендуется для production.
- `LOG_FORMAT=text` — человекочитаемый формат; удобен при локальной отладке
  (`docker compose logs -f`).
- `LOG_LEVEL=INFO` по умолчанию; переопределяется в `.env` (например,
  `LOG_LEVEL=DEBUG` для диагностики одного сервиса без перезапуска всего стека).

Новые метрики (добавлены в рамках Этап-0 наблюдаемости):
`scud_command_lifecycle_total`, `scud_command_queue_latency_seconds`,
`scud_command_ack_latency_seconds`, `scud_login_attempts_total`, `scud_otp_total`,
`scud_security_events_total`, `scud_interservice_requests_total`,
`scud_db_errors_total`, `scud_db_pool_connections_{in_use,idle,waiters,max}`.
Каждая серия несёт label `service` первым; кардинальность намеренно ограничена
(перечисляемые типы, без user-id / locker-id в labels).

---

## Secret rotation

### `JWT_SECRET` (rolling rotation)

Access-токен подписан старым ключом → действителен до истечения
(`ACCESS_TOKEN_TTL_HOURS=24` default). Новый токен — на новом ключе.

Procedure:

1. Сгенерировать новый ключ:
   ```bash
   openssl rand -hex 32 > backend/secrets/jwt_secret.new
   ```
2. Перезаписать файл секрета:
   ```bash
   mv backend/secrets/jwt_secret.new backend/secrets/jwt_secret
   chmod 644 backend/secrets/jwt_secret
   ```
3. Перезапустить только `auth_api`:
   ```bash
   docker compose up -d --no-deps --force-recreate auth_api
   ```
4. Подождать `ACCESS_TOKEN_TTL_HOURS` (24h по дефолту) — все старые
   токены истекут, пользователи будут вынуждены залогиниться заново
   уже на новом ключе.
5. Если хочется немедленной revocation всех сессий — после шага 3 пройти
   по таблице `auth_api.sessions` и удалить активные. Все клиенты получат
   401 на следующий запрос.

> ⚠️ Если rotate'ите при компрометации ключа — НЕ ждите 24h, делайте
> hard revocation (шаг 5). Старый ключ остаётся валидным до истечения
> TTL.

### Внутренние HMAC-ключи (синхронная замена, окно ~10–60 с) <a id="internal-hmac-rotation"></a>

Rolling rotation невозможен: ключ должен совпадать у подписывающего и
проверяющего сервисов, поэтому все сервисы строки пересоздаются **одной
командой**. В окне между перезапуском первого и готовностью последнего
подписанные запросы получают 401.

| Ключ (файл в `secrets/`) | Пересоздать вместе | Что не работает в окне |
|---|---|---|
| `internal_auth_hmac_key` | `auth_api user_api` | `/internal/auth/check` → 401: login (~10–30 с) |
| `internal_otp_hmac_key` | `auth_api notification_api` | `/internal/api/v1/otp/send` → 401: login пользователей с 2FA (~10–30 с) |
| `internal_max_binding_hmac_key` | `notification_api user_api` | привязка Max-аккаунта (`/internal/user/by-invite`); события `bot_started` ставятся в очередь, Max API повторяет push, данные не теряются |
| `internal_step_up_hmac_key` | `auth_api user_api locker_api` | `POST /locker/{id}/commands` для `require_2fa=true` → 503; пользователи без 2FA и команды `close` не затрагиваются (~10–30 с) |
| `internal_user_read_hmac_key` | `auth_api reports_api user_api` | PII-read (by-phone/by-id/names) → 401: подбор пользователя по телефону при login и отчёты с именами (~10–30 с) |
| `internal_session_revoke_hmac_key` | `user_api auth_api` | отзыв сессий best-effort: `auth_api` вернёт 401 на несовпавшую подпись, отзыв в окне может не примениться (~10–30 с) |
| `internal_history_hmac_key` | `locker_api auth_api history_api` | записи в history_api (`/internal/api/v1/{events/security,telemetry,media}`) → 401; события из outbox `locker_api` доставятся повторно, а step-up-события аудита (`command_2fa_*`) от `auth_api`/`locker_api`, попавшие в окно, теряются |

```bash
KEY=internal_auth_hmac_key        # файл из таблицы
SERVICES="auth_api user_api"      # сервисы из той же строки
openssl rand -hex 32 > backend/secrets/$KEY
chmod 644 backend/secrets/$KEY
docker compose up -d --no-deps --force-recreate $SERVICES
```

Проверка после замены (для `internal_auth_hmac_key` и `internal_otp_hmac_key` —
login):

```bash
curl -X POST http://localhost/api/v1/auth/login \
    -H 'Content-Type: application/json' \
    -d '{"phone":"+79000000000","password":"<admin>"}'
# 200 OK
```

Если после замены login отвечает 500 (`Invalid signature` в логах `auth_api`),
один из сервисов остался со старым ключом — пересоздайте все сервисы строки,
см. [Troubleshooting](#login-500-after-rotation).

### `MAX_BOT_TOKEN`

Токен бота можно отозвать через `@MasterBot` (команда `/revoke`).
После отзыва — выпустить новый, записать в `backend/secrets/max_bot_token`,
перезапустить `notification_api`. Старый long-polling-цикл получит 401
и завершится по `UpstreamUnavailableError` (см. логи `MaxBotPoller`),
новый запустится с новым токеном.

### `REDIS_PASSWORD`

Redis держит ACM-кэш ролей. Замена пароля → все клиенты теряют
коннект; кэш ACM пересоберётся за `CACHE_TTL_SECONDS` (30s default)
из postgres.

Procedure:

1. `openssl rand -hex 16 > backend/secrets/redis_password` (и в `.env`).
2. Перезапустить redis + всех клиентов:
   ```bash
   docker compose up -d --no-deps --force-recreate \
       redis user_api acm
   ```
3. Подождать 1 минуту (heartbeat-cache rebuild). Проверить логин.

### `POSTGRES_*_PASSWORD` (по одному admin-секрету на инстанс, SEC-24)

Postgres хранит хеш в системной таблице, не читает env при каждом
запросе. Поэтому замена секрет-файла + restart НЕ меняет пароль — нужен
`ALTER USER`.

SEC-24: у каждого из 6 инстансов СВОЙ admin-пароль (`postgres_<svc>_password`),
независимый от остальных. Поэтому ротацию можно делать поинстансно — менять
пароль одной БД, не трогая остальные. Соответствие инстанс → секрет → клиент:

| инстанс | секрет | БД | клиент-сервис |
|---|---|---|---|
| `postgres_user` | `postgres_user_password` | `scud_users` | `user_api` |
| `postgres_locker` | `postgres_locker_password` | `scud_lockers` | `locker_api` |
| `postgres_history` | `postgres_history_password` | `scud_history` | `history_api` |
| `postgres_auth` | `postgres_auth_password` | `scud_auth` | `auth_api` |
| `postgres_notification` | `postgres_notification_password` | `scud_notifications` | `notification_api` |
| `postgres_reports` | `postgres_reports_password` | `scud_reports` | `reports_api` |

> `acm` ходит в `scud_users` под отдельной least-privilege ролью `acm_ro`
> (секрет `acm_db_password`) — её admin-пароль `postgres_user_password` НЕ
> касается; ротация acm_ro описана ниже.

Procedure (на примере `postgres_user`; для остальных инстансов подставьте
host/секрет/клиент из таблицы):

1. Поменять admin-пароль на нужном инстансе:
   ```bash
   docker compose exec postgres_user psql -U admin -d postgres \
       -c "ALTER USER admin WITH PASSWORD '<new_password>';"
   ```
2. Обновить соответствующий секрет-файл (и `.env`, если используется
   env-fallback `POSTGRES_PASSWORD` для этого сервиса):
   ```bash
   echo '<new_password>' > backend/secrets/postgres_user_password
   ```
3. Перезапустить ТОЛЬКО клиента этой БД:
   ```bash
   docker compose up -d --no-deps --force-recreate user_api
   ```

Чтобы провести ротацию всех 6 admin-паролей за один проход (каждый —
со своим новым значением):

```bash
for triple in \
    postgres_user:postgres_user_password:user_api \
    postgres_locker:postgres_locker_password:locker_api \
    postgres_history:postgres_history_password:history_api \
    postgres_auth:postgres_auth_password:auth_api \
    postgres_notification:postgres_notification_password:notification_api \
    postgres_reports:postgres_reports_password:reports_api; do
    HOST=${triple%%:*}; rest=${triple#*:}; SECRET=${rest%%:*}; CLIENT=${rest#*:}
    NEW=$(openssl rand -hex 16)
    docker compose exec "$HOST" psql -U admin -d postgres \
        -c "ALTER USER admin WITH PASSWORD '$NEW';"
    printf '%s' "$NEW" > "backend/secrets/$SECRET"
    docker compose up -d --no-deps --force-recreate "$CLIENT"
done
```

> Не делайте `docker compose down -v` для смены пароля postgres —
> это удалит volume вместе со всеми данными.

### `ACM_DB_PASSWORD` (роль acm_ro, SEC-24)

Пароль выделенной least-privilege роли `acm_ro` (SELECT-only) в `scud_users`,
под которой ходит ACM. Как и admin-пароль, хранится в системной таблице
postgres — поэтому смена секрет-файла без `ALTER ROLE` ничего не меняет.

Procedure (затрагивает только ACM):

1. Сменить пароль роли в `scud_users` под admin:
   ```bash
   docker compose exec postgres_user psql -U admin -d scud_users \
       -c "ALTER ROLE acm_ro WITH PASSWORD '<new_password>';"
   ```
2. Обновить секрет-файл (и `.env`, если используется env-fallback `ACM_DB_PASSWORD`):
   ```bash
   echo '<new_password>' > backend/secrets/acm_db_password
   chmod 644 backend/secrets/acm_db_password
   ```
3. Перезапустить ТОЛЬКО ACM:
   ```bash
   docker compose up -d --no-deps --force-recreate acm
   ```

### `ADMIN_PASSWORD`

Используется только при первом старте `user_api` для создания
начального администратора. Изменение env после первого запуска
ни на что не влияет — пароль админа хранится как bcrypt-хеш в
`scud_users.user_auth.password_hash`. Меняется через UI
/api/v1/user/{id}/password.

> **Как именно хранится пароль.** Итоговое значение в
> `scud_users.user_auth.password_hash` — это `bcrypt(sha256(plaintext))`.
> Клиент/транспорт шлёт SHA-256-пред-хэш (`shared.utils.hash_password` —
> историческое имя: это **client/transport-side** SHA-256, а не финальное
> хранилище), сервер дополнительно оборачивает его в bcrypt
> (`user.py` → `bcrypt.hashpw`; проверка — `internal_auth.py` →
> `bcrypt.checkpw`). Двухслойность исторична; функцию `hash_password` не
> переименовываем, чтобы не ломать совместимость транспорта.

### Ротация MQTT-пароля брокера (`mqtt_password`) <a id="mqtt-broker-password-rotation"></a>

Пароль backend-пользователя `scud_backend` в брокере Mosquitto. Один и тот
же секрет читают:

- **брокер** (`mqtt`) — `entrypoint.sh` при каждом старте перезаписывает
  строку `scud_backend:` в `passwordfile` из `/run/secrets/mqtt_password`
  (или env `MQTT_PASSWORD`, dev-fallback в последнюю очередь);
- **locker_api** — `read_secret("mqtt_password", env_var="MQTT_PASSWORD")`
  при подключении MQTT-клиента.

> **Caveat (INF-06): config-том брокера переживает рестарт.** `passwordfile`
> и `aclfile` лежат на именованном volume `mqtt_config`. `mosquitto.conf` и
> `aclfile` копируются из image-дефолтов ТОЛЬКО когда отсутствуют (provisioning
> дописывает per-device ACL — их нельзя затирать). Строку `scud_backend:` в
> `passwordfile` entrypoint **реконсайлит на каждом старте**, поэтому смена
> секрета + рестарт брокера достаточно — НЕ нужно удалять volume. Если же
> нужно сбросить ACL/per-device записи к дефолтам — удалите volume
> (`docker volume rm backend_mqtt_config`) и переподнимите брокер; устройства
> придётся переподготовить (provision) заново.

Procedure (downtime брокера ~несколько секунд):

1. Сгенерировать новый пароль и записать в секрет (и в `.env`, если используется
   env-fallback):
   ```bash
   openssl rand -hex 16 > backend/secrets/mqtt_password
   chmod 644 backend/secrets/mqtt_password
   ```
2. Перезапустить брокер (перепишет `passwordfile` из нового секрета) и
   locker_api (подхватит новый секрет при реконнекте):
   ```bash
   docker compose up -d --no-deps --force-recreate mqtt locker_api
   ```
3. Проверить, что locker_api переподключился к брокеру (логи `MqttClient`),
   и что relay-устройства снова online.

> Порт `1883` публикуется только на `127.0.0.1` (loopback) — брокер
> недоступен извне хоста; backend ходит к нему по `backend_net` (`mqtt:1883`).
> Не публикуйте `0.0.0.0:1883` в production (это раскрывает брокер на LAN).
> TLS терминируется sidecar'ом `mqtt_tls` на `8883` (см. «MQTT-брокер →
> Сетевой контур»); listener самого брокера остаётся plaintext и только внутри
> `backend_net`. Публикация `0.0.0.0:1883` допустима лишь на демо-стенде для
> платы без TLS и требует ограничения по source-IP: пароль устройства выводится
> из публичного `relay_id`.

### Ротация TLS-сертификата MQTT <a id="mqtt-tls-cert-rotation"></a>

Плановый перевыпуск серверного листа (CA не меняется, платы не трогаем):

```bash
bash backend/scripts/gen-mqtt-tls-certs.sh --force --dns <имя> --ip <адрес> --ip 127.0.0.1
docker compose --profile mqtt-tls restart mqtt_tls
```

`restart` обязателен: haproxy читает `crt` один раз, на разборе конфига.
`kill -USR2` мастеру (PID 1) тоже работает — образ запускает haproxy с `-W`.

Ротация CA (`--force-ca`) означает заливку нового `ca.crt` во ВСЕ платы ДО
переключения и проводится только в окно простоя.

Срок листа — 825 дней: `openssl x509 -in backend/mqtt-tls/server/server.crt -noout -enddate`.
`ca/ca.key` не монтируется ни в один контейнер. Штатное место его хранения —
офлайн, вне сервера: утечка ключа означает возможность подделать брокер для
всего парка плат. Для ротации серверного листа ключ временно возвращают в
`ca/`, а после — снова убирают (`shred -u ca.key`). Если `ca.crt` есть, а
ключа нет, генератор отказывается работать и не выпускает новый корень:
смена CA потребовала бы перепрошивки каждой платы.
Сертификаты генерировать на хосте от root: bind-mount сохраняет uid файла,
а `server.pem` имеет режим 0600.

### Ротация `pgb_admin_password` (stats-роль PgBouncer) <a id="pgb-admin-password-rotation"></a>

`pgb_admin_password` — Docker secret, используемый для двух независимых целей:

- **PgBouncer entrypoint** (`pgbouncer/entrypoint.sh`) вписывает его в `userlist.txt`
  как пароль локальной stats-роли `pgb_admin` при каждом старте контейнера.
- **`zabbix_agent2`** (`dbprobe.sh`) подключается к admin-консоли каждого пулера
  (`psql -p 6432 -U pgb_admin pgbouncer`) для `SHOW STATS` / `SHOW POOLS`.

`pgb_admin` — это **pgbouncer-локальная** роль (`admin_users`/`stats_users`);
она НЕ существует в PostgreSQL и не требует `ALTER USER` при ротации.

Процедура ротации (downtime пулеров ~5–10 с на каждый):

1. Сгенерировать новый пароль:
   ```bash
   openssl rand -hex 16 > backend/secrets/pgb_admin_password
   chmod 644 backend/secrets/pgb_admin_password
   ```
2. Пересоздать все 6 пулеров и `zabbix_agent2` (они перечитают секрет при старте):
   ```bash
   docker compose up -d --force-recreate \
       pgbouncer_user pgbouncer_locker pgbouncer_history \
       pgbouncer_auth pgbouncer_notification pgbouncer_reports \
       zabbix_agent2
   ```
3. Проверить, что agent2 успешно опрашивает пулеры (логи `zabbix_agent2`, или
   убедиться, что `scud.dbprobe[pgb,...]` items обновились в Zabbix UI).

> `pgb_admin_password` в dev создаётся `backend/scripts/seed-dev-secrets.sh`
> (как и прочие dev-секреты). В production — `openssl rand -hex 16`.

---

## Disaster recovery

### Сценарий 1: повреждение одного postgres-volume

Если `postgres_user_data` повреждён, а у нас есть свежий backup в
`backup_data`:

```bash
# 1. Остановить клиентов БД (чтобы не писали в восстанавливаемую БД).
docker compose stop user_api auth_api acm

# 2. Удалить старый volume и поднять чистый postgres.
docker compose rm -sf postgres_user
docker volume rm backend_postgres_user_data
docker compose up -d postgres_user

# 3. Дождаться healthy.
docker compose ps postgres_user

# 4. Найти нужный дамп.
docker compose --profile backup run --rm backup ls -la /backups/

# 5. Восстановить (имя файла подставить из шага 4). PGPASSWORD — admin-секрет
#    ИМЕННО этого инстанса (SEC-24): для scud_users это postgres_user_password.
docker compose --profile backup run --rm \
    -e PGPASSWORD=$(cat secrets/postgres_user_password) \
    backup pg_restore -h postgres_user -U admin -d scud_users \
        --clean --if-exists \
        /backups/scud_users-YYYYMMDD-HHMMSS.dump

# 6. Поднять клиентов обратно.
docker compose up -d user_api auth_api acm
```

### Сценарий 2: полная потеря host'а

1. Поднять новый host с Astra Linux SE 1.7+ и Docker 24+.
2. `git clone` репозиторий, скопировать `.env` и `secrets/` из off-site
   backup.
3. Скопировать последние дампы из off-site в host: `/tmp/backups/*.dump`.
4. Поднять только postgres-серверы:
   ```bash
   docker compose up -d \
       postgres_user postgres_locker postgres_history \
       postgres_auth postgres_notification postgres_reports
   ```
5. Подождать `healthy` → восстановить каждую БД. PGPASSWORD берётся из
   admin-секрета ИМЕННО этого инстанса (SEC-24 — отдельный пароль на инстанс):
   ```bash
   for triple in postgres_user:scud_users:postgres_user_password \
                 postgres_locker:scud_lockers:postgres_locker_password \
                 postgres_history:scud_history:postgres_history_password \
                 postgres_auth:scud_auth:postgres_auth_password \
                 postgres_notification:scud_notifications:postgres_notification_password; do
       HOST=${triple%%:*}; rest=${triple#*:}; DB=${rest%%:*}; SECRET=${rest#*:}
       LATEST=$(ls -1t /tmp/backups/${DB}-*.dump | head -1)
       echo "Restoring $DB from $LATEST"
       docker run --rm \
           --network backend_backend_net \
           -v /tmp/backups:/backups:ro \
           -e PGPASSWORD=$(cat backend/secrets/$SECRET) \
           registry.astralinux.ru/library/astra/ubi18-postgresql15:1.8.5 \
           pg_restore -h $HOST -U admin -d $DB --clean --if-exists \
               /backups/$(basename $LATEST)
   done
   ```
   > `scud_reports` восстанавливать не обязательно: сервис создаёт схему при
   > первом старте, а сами отчёты регенерируются из history_api. Если дамп
   > `scud_reports-*.dump` есть в off-site backup — можно добавить пару
   > `postgres_reports:scud_reports` в цикл выше; иначе достаточно чистой БД.
6. Поднять остальной стек:
   ```bash
   cd backend && docker compose up -d
   ```
7. Проверить смок-тест: `curl /health`, login flow.

### Restart-order (при холодном старте после catastrophic shutdown)

`docker compose up -d` сам соблюдает порядок через `depends_on:
condition: service_healthy`. Полный graph:

```
postgres_*  →  redis  →  user_api  →  auth_api  →  acm
            →  mqtt   →  locker_api / history_api / notification_api / reports_api
                                                       →  apache_gateway
```

`locker_api` зависит от `mqtt` (`depends_on: condition: service_healthy`) — без
живого брокера релейный транспорт не поднимется.

Cold start полного стека — ~30–60s до первого `healthy gateway`.

---

## Rolling restart

Перезапустить один сервис без полного `docker compose restart`:

```bash
docker compose up -d --no-deps --build user_api
```

`--no-deps` гарантирует, что зависимые сервисы (postgres, redis) НЕ
рестартуют. `--build` пересобирает образ — нужно при апдейте кода или
Dockerfile.

Apache gateway сам по себе не fail'ит на одном из upstream'ов — он
вернёт `502/503` для трафика на тот сервис, остальные продолжат
работать. depends_on срабатывает только при первоначальном старте
gateway, не при runtime-restart upstream'ов.

При апдейте `apache_gateway`:

```bash
docker compose up -d --no-deps --build apache_gateway
```

Downtime ~5–10s (graceful shutdown apache + старт нового контейнера).
TLS-терминатор перед gateway вернёт 502 на это окно.

При апдейте `locker_api` (1000 реле по MQTT):

```bash
docker compose up -d --no-deps --build locker_api
```

`stop_grace_period` (см. compose) даёт locker_api время корректно закрыть
MQTT-сессию и дренировать in-process состояние (реестр команд, ingest). Реле
переподключаются к брокеру и продолжают обслуживаться новым контейнером через
несколько секунд.

---

## Масштабирование и ограничение single-replica <a id="scaling-single-replica"></a>

`locker_api` разворачивается **строго в одну реплику**. Сама по себе доставка
MQTT-команд брокер-опосредована и НЕ привязана к конкретной реплике: команда
пишется `pending` в `locker_commands`, публикуется в топик `com/{relay_id}` через
брокер, а гонку двух отправителей снимает атомарный CAS-переход `pending → sent`
(`UPDATE ... WHERE status='pending' RETURNING`) — отправит ровно одна реплика.
Реестр `CommandQueue._connections` теперь наполняется на КАЖДОЙ реплике из общей
подписки на `stat` (это уже не эксклюзивный владелец транспорта, каким он был
на WebSocket).

Тем не менее несколько частей сервиса пока НЕ идемпотентны между репликами,
поэтому **>1 реплики ломает систему**:

- **Фиксированный MQTT `client_id` (`scud-locker-api`)** — брокер по спецификации
  вытесняет старую сессию при коннекте новой с тем же id; две реплики уходят в
  бесконечную войну reconnect'ов → `stat`-ingest не стабилизируется → команды и
  телеметрия не доставляются.
- **Приём `stat`/`rep`/`photos` идёт по обычным (не `$share`) подпискам** — каждая
  реплика видит сообщения каждого реле и независимо шлёт дубли телеметрии и
  device-security-событий (edge-детект по in-process кэшам `_last_power` /
  `_last_alarm` / `_last_status_error`).
- **Стартовые DB-сбросы на каждую реплику** — `reset_stale_online_lockers` и
  `reset_stale_sent_commands` затирают connectivity/команды реле, которые
  обслуживает другая реплика; `StateMonitor` шлёт ALARM без CAS-защиты (дубли).

Путь доставки событий **outbox → history_api** (`OutboundDispatcher` +
`outbound_history_events`), напротив, **multi-replica-safe**: `fetch_pending`
заявляет строки атомарно через `UPDATE ... RETURNING` поверх
`FOR UPDATE SKIP LOCKED` с lease-сдвигом `next_attempt_at` в будущее, поэтому
две реплики dispatcher'а никогда не захватывают одни и те же строки. (Даже при
гонке повторная доставка безвредна: history_api дедуплицирует по `event_uid`
через `ON CONFLICT DO NOTHING`.)

**Правило развёртывания:** разворачивайте `locker_api` строго как **одну
реплику**. Multi-replica scale-out потребует per-pod MQTT `client_id`,
идемпотентного/партиционированного ingest и защищённых стартовых сбросов —
сейчас этого нет. Остальные шесть сервисов масштабируются свободно.

### Осознанные вынужденные ограничения

Два архитектурных ограничения ниже — следствие сертификации Astra, а не
технический долг. Учитывайте их при тюнинге и масштабировании.

- **DB-слой — psycopg2 + ThreadPoolExecutor, а не нативный asyncpg.**
  `shared.database.AsyncPostgres` оборачивает синхронный `psycopg2` поверх
  `ThreadedConnectionPool` в `ThreadPoolExecutor` (см.
  `backend/shared/database/connection.py`). Это **не** нативный async-драйвер:
  `asyncpg` ОТСУТСТВУЕТ в сертифицированном `main` Astra (есть только
  `python3-psycopg2`, см. `available_packets.txt`) и не может попасть в
  production-образ. Сознательный cert-trade-off: пропускная способность
  ограничена числом потоков пула (`DB_MAX_WORKERS`), но образ остаётся
  сертифицируемым. Не «чините» это переходом на asyncpg — он недопустим в
  `production`-стадии Dockerfile.

- **Rate-limit (login) — in-memory `TokenBucket`, НЕ shared между
  репликами/процессами.** `shared.utils.rate_limit.TokenBucket`
  (`backend/shared/utils/rate_limit.py`) хранит счётчик в памяти процесса; он
  **не** реплицируется через Redis или иной shared store. Практическое
  следствие: при горизонтальном масштабировании сервиса эффективный лимит
  умножается на число реплик (каждая реплика держит собственный бакет). При
  тюнинге `LOGIN_RATE_LIMIT` делите целевое значение на
  ожидаемое число реплик. (`auth_api` сейчас фактически single-instance, а
  `locker_api` обязан быть single-replica по причине выше, поэтому на текущей
  топологии умножение = ×1 — но при будущем масштабировании учитывайте его.)

---

## PgBouncer (transaction pooling) <a id="pgbouncer"></a>

### Топология

Перед каждым из 6 PostgreSQL-инстансов стоит **отдельный** пулер PgBouncer
(`pgbouncer_user`, `pgbouncer_locker`, `pgbouncer_history`, `pgbouncer_auth`,
`pgbouncer_notification`, `pgbouncer_reports`), слушающий порт **6432**.
Production-сервисы подключаются к БД не напрямую, а через свой пулер
(`DB_HOST=pgbouncer_<instance>`, `DB_PORT=6432`). ACM ходит в `scud_users`
через `pgbouncer_user` под least-privilege ролью `acm_ro` (SEC-24) — userlist
user-пулера содержит обе роли (`admin` и `acm_ro`).

Один пулер на инстанс (а не один мульти-БД пулер) сохраняет blast-radius
SEC-24: каждый пулер монтирует **только** секрет своего инстанса
(`postgres_<svc>_password`; user-пулер — дополнительно `acm_db_password`).
Утечка одного пулера не открывает остальные 5 БД.

`*_tests` / `gateway_tests` контейнеры намеренно подключаются к postgres
**напрямую** (сидируют данные raw psycopg2-conftest'ами в обход пулинга);
app-through-pgbouncer путь при этом всё равно покрыт — живые сервисы под
тестом ходят через свой пулер.

### Почему transaction-режим безопасен

`pool_mode=transaction` возвращает серверное соединение в пул на границе каждой
транзакции, поэтому потолок коннектов к postgres равен сумме `default_pool_size`
пулеров и **не растёт** с числом реплик сервисов. Режим безопасен, потому что
весь код держит инвариант **single-statement транзакций** (один `execute` =
одна транзакция = один checkout): нет session-prepared statements, advisory
locks, `LISTEN`/`NOTIFY`, session-level `SET`. Инвариант зафиксирован в
`backend/CONVENTIONS.md → "База данных: single-statement транзакции"` — нарушение
сломает пулинг тихо.

`ignore_startup_parameters = extra_float_digits,application_name` **обязателен**:
psycopg2 шлёт `application_name` в startup-пакете (ACM — `acm`, остальные —
`async-postgres-connector`/SERVICE_NAME). Без игнора transaction-режим отклоняет
подключение с `unsupported startup parameter`.

### Размер пула vs postgres max_connections

`postgres max_connections` оставлен дефолтным (100) на инстанс. Сумма
`default_pool_size + reserve_pool_size` пулеров одного инстанса должна
укладываться в этот потолок с запасом на superuser/служебные подключения.
Дефолты (docker-compose, env-override `PGB_DEFAULT_POOL_SIZE_<INSTANCE>`):

| Инстанс | default_pool_size | Обоснование |
|---|---|---|
| user | 15 (×2 роли: admin + acm_ro) | low-QPS, но две роли |
| locker | 30 | hot-path (1000+ WS, телеметрия) |
| history | 25 | hot-path (батч-инсёрты телеметрии) |
| auth / notification / reports | 10 | low-QPS |

`reserve_pool_size=5` (общий, env `PGB_RESERVE_POOL_SIZE`) — доп. соединения при
всплеске. `max_client_conn=200` (env `PGB_MAX_CLIENT_CONN`) — потолок клиентских
подключений к пулеру.

### Ротация userlist (синхронно с ротацией пароля postgres)

PgBouncer читает userlist при старте; entrypoint генерирует его из
смонтированных секретов. При ротации пароля роли в postgres
(`ALTER USER admin PASSWORD ...` или замена секрета `postgres_<svc>_password` /
`acm_db_password` — см. [Secret rotation](#secret-rotation)) **обязательно**
пересоздайте соответствующий пулер, чтобы он перегенерировал userlist:

```bash
docker compose up -d --force-recreate pgbouncer_<instance>
```

Порядок: сначала меняем пароль в postgres и секрет-файл, затем пересоздаём
пулер. Между этими шагами новые подключения через пулер падают с auth-ошибкой —
делайте в окно обслуживания или с rolling-перезапуском зависимых сервисов.

### Диагностика

```bash
docker compose exec pgbouncer_user \
    psql -h 127.0.0.1 -p 6432 -U pgb_admin pgbouncer -c "SHOW POOLS;"
docker compose exec pgbouncer_user \
    psql -h 127.0.0.1 -p 6432 -U pgb_admin pgbouncer -c "SHOW STATS;"
docker compose logs -f pgbouncer_user
```

`sv_active` в `SHOW POOLS` не превышает `default_pool_size` — это и есть кап
серверных соединений; при насыщении растёт `cl_waiting` (клиенты ждут).

### Admin-консоль

Виртуальная роль `pgb_admin` (`admin_users`/`stats_users`) даёт доступ только к
`SHOW POOLS`/`SHOW STATS` внутренней БД `pgbouncer` — это НЕ роль PostgreSQL и не
видит прикладных данных. Консоль доступна лишь внутри `backend_net` (порт 6432
наружу не публикуется). В production задайте непустой `PGB_ADMIN_PASSWORD`
(env/secret) для каждого пулера вместо dev-дефолта; ротация — вместе с
`--force-recreate pgbouncer_<instance>`.

> **Замечание про locker_api.** PgBouncer ограничивает только коннекты к БД и
> НЕ снимает правило single-replica для `locker_api`: состояние транспорта
> устройств (MQTT) живёт in-process (см. [раздел выше](#scaling-single-replica)).
> Масштабировать `locker_api` в >1 реплику по-прежнему нельзя.

---

## Observability

### Логи

Отдельного структурированного логгер-модуля в `shared` нет. Логирование
настроено через stdlib: каждый модуль заводит `logging.getLogger(__name__)`,
уровень задаётся переменной окружения `LOG_LEVEL` (по умолчанию `INFO`). Logs
пишутся в stdout, ротация — через docker `json-file` driver: 10M × 5 файлов =
50M на сервис. Пароли, OTP-коды, содержимое JWT и сессионные токены никогда не
логируются.

```bash
# tail логов одного сервиса
docker compose logs -f --tail=200 locker_api

# all-in-one
docker compose logs -f --tail=50

# фильтр по уровню
docker compose logs --tail=1000 user_api | grep ERROR
```

### Healthchecks

```bash
docker compose ps
# смотреть колонку STATUS — должна быть "(healthy)" у всех долгоживущих сервисов
```

Каждый backend сервис экспортирует `/health` (200 OK / 503).

### Контракт проб live / ready / health

Каждый из 7 backend-сервисов отдаёт три эндпоинта (без аутентификации, только
внутри backend_net; Apache их наружу не проксирует):

- **`/live`** — liveness. Отдаёт `200 {"status":"alive"}` пока процесс жив.
  НЕ трогает БД и не зависит ни от чего внешнего. Это единственная проба, на
  которую завязывается **рестарт** оркестратора. Высокочастотная и дешёвая:
  не минтит X-Request-ID и не пишет в `scud_http_*` (в обход JSONHandler).
- **`/ready`** — readiness. Прогоняет зависимости конкурентно и отдаёт:
  `200 {"status":"ok", ...}` когда все здоровы; `200 {"status":"degraded", ...}`
  когда отказала **мягкая** зависимость (Redis у user_api/acm, MQTT у
  locker_api — сервис деградирует, но обслуживает); `503
  {"status":"unavailable", ...}` только когда отказала **критическая**
  зависимость (БД через PgBouncer). `/ready` — сигнал для **трафика/алертов**
  (Zabbix), его НЕЛЬЗЯ привязывать к kill-действию.
  Матрица зависимостей: user_api=[db,redis], acm=[db,redis],
  locker_api=[db,mqtt], auth_api/history_api/notification_api/reports_api=[db].
- **`/health`** — DB-only (`SELECT 1`), `200/503`. Остаётся как есть для
  **docker healthcheck** в compose: именно его стерегут `depends_on:
  condition: service_healthy` при гейтинге порядка старта (например auth_api
  ждёт `service_healthy` у user_api и notification_api).

Никакого синхронного cross-service fan-out в `/ready` нет: сводку по всем
сервисам даёт Zabbix, скрейпя каждый отдельно. Порядок холодного старта
гейтится один раз на буте через compose `depends_on` + `/health`, а не
runtime-связью между `/ready`-эндпоинтами.

### Prometheus metrics

Каждый backend-сервис экспортирует `/metrics` в Prometheus
exposition-формате. Пример scrape-конфига:

```yaml
scrape_configs:
  - job_name: scud_backend
    scrape_interval: 15s
    static_configs:
      - targets:
          - 'user_api:8000'
          - 'locker_api:8001'
          - 'history_api:8002'
          - 'auth_api:8003'
          - 'acm:8004'
          - 'notification_api:8005'
          - 'reports_api:8006'
```

`/metrics` НЕ должен быть доступен снаружи — apache_gateway
не проксирует его (либо требует internal-net auth). Prometheus
запускайте в той же docker-сети `backend_net` или скрейпьте с хоста
по проброшенным портам.

### Resource usage

```bash
docker stats --no-stream
```

Если `locker_api` упирается в 2G на 1000 WS — увеличьте `mem_limit`
и `DB_MAX_WORKERS`/`DB_MIN_CONNECTIONS` (см. `.env.example`).

---

## Troubleshooting

### Postgres не стартует

**Симптом:** `postgres_user` падает с `FATAL: password authentication
failed` или `unhealthy` в `docker compose ps`.

**Причины и решения:**

- Файл секрета отсутствует/пустой (SEC-24: у каждого инстанса свой
  `postgres_<svc>_password`; для `postgres_user` это `postgres_user_password`):
  ```bash
  ls -l backend/secrets/postgres_user_password
  cat backend/secrets/postgres_user_password   # должен быть непустой
  ```
  Если файл пуст — пересоздать (см. First-time deployment §3).

- Volume содержит старый пароль (если postgres уже инициализировался
  с другим значением). Postgres хранит пароль внутри volume и НЕ
  перечитывает env при последующих стартах. Решение: либо
  `ALTER USER` (см. Secret rotation), либо пересоздать volume
  (потеря данных — проверьте, что есть backup):
  ```bash
  docker compose down
  docker volume rm backend_postgres_user_data
  docker compose up -d postgres_user
  ```

### Gateway возвращает 502

**Симптом:** `curl http://localhost/api/v1/users` → `502 Bad Gateway`.

**Решения:**

1. Проверить, какие upstream'ы upstream:
   ```bash
   docker compose ps
   # все должны быть (healthy)
   ```
2. Если один из backend сервисов unhealthy — посмотреть его логи:
   ```bash
   docker compose logs --tail=200 user_api
   ```
3. Стандартные причины `unhealthy`: БД недоступна, secret-файл пустой,
   порт занят, OOM (см. `mem_limit`).

### OTP-код не приходит

**Симптом:** `/auth/login` отвечает 503 или 200, но код не приходит.

OTP уходит по SMS (основной канал), при неудаче — по Max (fallback). Login
отвечает 503, если код не доставлен ни одним каналом.

**Причины:**

- SMS: `SMS_GATEWAY_URL` пуст, пусты секреты `sms_api_user`/`sms_api_password`
  или не поднят профиль `sms`. Проверьте туннель:
  ```bash
  docker compose exec wg_gateway wg show wg0 latest-handshakes
  docker compose logs --tail=200 notification_api | grep -i sms
  ```
- Max: `MAX_BOT_TOKEN` пуст / неверный — поллер не стартует:
  ```bash
  docker compose logs --tail=200 notification_api | grep -E "MAX_BOT_TOKEN|Max bot poller"
  ```
- Пользователь не привязал Max-бот (нужен только для fallback): проверьте, что
  он нажал «Начать» по deep-link `https://max.ru/<bot_username>?start=<invite_token>`
  (invite-токен выдаётся в response `POST /api/v1/users`).
- Max API недоступен — в логах `notification_api`
  `UpstreamUnavailableError(upstream=max_bot_api)`; проверьте доступ к
  `https://platform-api.max.ru`.

### Backup-контейнер не запускается

**Симптом:** `docker compose --profile backup up -d backup` → exit 1.

**Решения:**

1. Проверить, что 6 postgres-серверов healthy:
   ```bash
   docker compose ps | grep postgres
   ```
2. Проверить, что все 6 секретов `secrets/postgres_<svc>_password`
   существуют и непустые (см. Postgres troubleshooting выше).
3. Запустить разовый дамп вручную, чтобы увидеть конкретную ошибку:
   ```bash
   docker compose --profile backup run --rm backup /scripts/backup.sh
   ```

### Login возвращает 500 после ротации `INTERNAL_AUTH_HMAC_KEY` <a id="login-500-after-rotation"></a>

**Симптом:** после ротации внутренних ключей `/auth/login` отвечает
500, в логах `auth_api` — `Invalid signature` от user_api.

**Причина:** auth_api перезапустился с новым ключом, user_api ещё
работает со старым.

**Решение:** перезапустить **оба** сервиса одновременно
(`docker compose up -d --force-recreate auth_api user_api`). См.
[Secret rotation: внутренние HMAC-ключи](#internal-hmac-rotation).

---

