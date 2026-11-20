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
sudo bash <(curl -fsSL https://raw.githubusercontent.com/pptkaa-del/scud/main/install.sh)
```

Другая ветка или тег — вторым аргументом:
`sudo bash <(curl -fsSL .../install.sh) https://github.com/pptkaa-del/scud.git v1.1`.

Дальше `deploy.sh install` задаст несколько вопросов (адрес сайта, телефон администратора и т.д.;
Enter — значение по умолчанию), сгенерирует секреты, соберёт образы (5–30 минут), поднимет стек
и в конце напечатает адрес и пароль администратора. Код лежит в `/opt/scud`.

Без вопросов:

```bash
SCUD_PUBLIC_URL=https://scud.example.com SCUD_ADMIN_PHONE=+79001234567 \
    sudo -E bash <(curl -fsSL https://raw.githubusercontent.com/pptkaa-del/scud/main/install.sh) \
    https://github.com/pptkaa-del/scud.git main -y
```

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
