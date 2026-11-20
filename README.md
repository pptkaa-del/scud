# СКУД

Backend (микросервисы в Docker Compose), фронтенд, MQTT-брокер, мониторинг.
Архитектура — [Architecture.md](Architecture.md), технологии — [Technologies.md](Technologies.md),
полное руководство по развёртыванию — [backend/DEPLOYMENT.md](backend/DEPLOYMENT.md).

## Быстрое развёртывание на сервере

Требования: Linux (Astra/Debian/Ubuntu), root, от 8 ГБ ОЗУ и 20 ГБ диска, свободный порт 80,
доступ к `registry.astralinux.ru` для сборки образов. Docker скрипт поставит сам, если его нет.

### 1. Установка (один раз)

На сервере одной командой:

```bash
curl -fsSL https://raw.githubusercontent.com/pptkaa-del/scud/main/install.sh -o install.sh && sudo bash install.sh
```

Другая ветка или тег — вторым аргументом:
`sudo bash install.sh https://github.com/pptkaa-del/scud.git v1.1`.

Дальше `deploy.sh install` задаст несколько вопросов (адрес сайта, телефон администратора и т.д.;
Enter — значение по умолчанию), сгенерирует секреты, соберёт образы (5–30 минут), поднимет стек
и в конце напечатает адрес и пароль администратора. Код лежит в `/opt/scud`.

Без вопросов:

```bash
SCUD_PUBLIC_URL=https://scud.example.com SCUD_ADMIN_PHONE=+79001234567 \
    sudo -E bash install.sh https://github.com/pptkaa-del/scud.git main -y
```

### HTTPS (nginx + Let's Encrypt)

Нужен домен, A-запись которого указывает на сервер, и открытые порты 80 и 443:

```bash
sudo bash /opt/scud/backend/scripts/setup-tls.sh scud.example.com admin@example.com
```

Скрипт переносит `apache_gateway` на `127.0.0.1:8080`, ставит nginx и certbot, получает
сертификат (продлевается автоматически), включает редирект на HTTPS и переводит СКУД
на адрес `https://домен` (`deploy.sh update --reconfigure`). Подробности — раздел
«TLS termination» в [backend/DEPLOYMENT.md](backend/DEPLOYMENT.md).

### Эмуляция подключённой платы

Для проверки без железа — одна эмулированная релейная плата (шкаф `RELAY-5001`):

```bash
sudo bash /opt/scud/emulator/run-device.sh            # создать шкаф и подключить плату
sudo bash /opt/scud/emulator/run-device.sh --events   # то же + события безопасности
sudo bash /opt/scud/emulator/run-device.sh logs       # журнал
sudo bash /opt/scud/emulator/run-device.sh stop       # остановить
```

Несколько плат: `RELAY_COUNT=10`; другой номер: `RELAY_ID=6001`. Подробнее — [emulator/README.md](emulator/README.md).

### 2. Обновление

На сервере — та же команда или напрямую:

```bash
sudo /opt/scud/backend/scripts/deploy.sh update --ref main
```

Перед обновлением делается дамп всех БД, после — применяются блоки схемы и проверяется `/health`.
Откат: `deploy.sh rollback`. Состояние: `deploy.sh status`.

### 3. Обновление из GitHub одной кнопкой

Workflow [.github/workflows/deploy.yml](.github/workflows/deploy.yml): **Actions → Deploy → Run workflow**,
указать ветку или тег. Нужны секреты репозитория (**Settings → Secrets and variables → Actions**):

| Секрет | Значение |
|---|---|
| `DEPLOY_HOST` | IP или имя сервера |
| `DEPLOY_USER` | пользователь SSH (root или с `sudo` без пароля) |
| `DEPLOY_SSH_KEY` | приватный SSH-ключ этого пользователя |
| `DEPLOY_PORT` | порт SSH, если не 22 |

Ключ для GitHub Actions можно сделать так (на своей машине), публичную часть добавить на сервер
в `~/.ssh/authorized_keys`, приватную — в секрет `DEPLOY_SSH_KEY`:

```bash
ssh-keygen -t ed25519 -N '' -C github-actions -f scud_actions
```
