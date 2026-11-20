# Фронтенд СКУД

Статический SPA без сборки. Гейтвей (`backend/gateway`) монтирует каталоги по ролям:

| Каталог     | Путь в контейнере       | Кому отдаётся                     |
|-------------|-------------------------|-----------------------------------|
| `public/`   | `/var/www/public`       | всем (логин, общие `/libs/*`)     |
| `admin/`    | `/var/www/roles/admin`  | administrator                     |
| `operator/` | `/var/www/roles/operator` | operator                        |
| `worker/`   | `/var/www/roles/worker` | worker                            |

Корень задаётся `FRONTEND_PATH` (по умолчанию `/root/frontend`), подробнее — `backend/DEPLOYMENT.md`.

Выкладка на стенд:

```bash
tar -C frontend -czf /tmp/frontend.tgz public admin operator worker
scp -P 443 /tmp/frontend.tgz root@<стенд>:/tmp/
ssh -p 443 root@<стенд> 'tar -xzf /tmp/frontend.tgz -C /root/frontend'
```

Все запросы к API идут на тот же origin (`/api/v1`), адрес стенда в коде не прописан. `FRONTEND_PATH` может указывать прямо на каталог `frontend/` репозитория; скрипт развёртывания выставляет его сам.
