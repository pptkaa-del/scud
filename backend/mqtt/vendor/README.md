# Vendored: Eclipse Mosquitto 2.0.23 (broker source)

Брокер собирается из **вендоренного upstream-исходника**, закоммиченного как есть
в `mosquitto-2.0.23/` (раньше здесь лежал `mosquitto-2.0.23.tar.gz` — заменён на
распакованное содержимое). Это та же политика «first-party source в образе», что и
у `backend/shared/_vendor/gmqtt`: никакого `pip`/`apt` из несертифицированных
репозиториев. `../Dockerfile` копирует это дерево в сертифицированный Astra-образ и
компилирует с `WITH_TLS=no WITH_CJSON=no WITH_WEBSOCKETS=no WITH_DOCS=no` (ноль
внешних библиотек).

Почему распакованный исходник, а не архив: дерево диффится и ревьюится в git
(аудит/сертификация ФСТЭК видят каждую строку и любой будущий патч), а сборка не
тратит шаг `tar xzf` и не зависит от бинарного блоба в истории.

## Происхождение и целостность

- Upstream: <https://mosquitto.org/files/source/mosquitto-2.0.23.tar.gz>
- Версия: 2.0.23
- Лицензия: EPL-2.0 / EDL-1.0 (см. `mosquitto-2.0.23/LICENSE.txt`)
- SHA-256 оригинального архива:
  `47d66fc98d9f8714bdc700dfe690dbe80c9fbe93c00a21b04dc61d00e1667b34`

Каталог — побайтовая распаковка этого архива (`tar xzf`, дерево целиком LF, без
симлинков и коллизий имён). Проверка соответствия upstream:

```sh
curl -fsSLO https://mosquitto.org/files/source/mosquitto-2.0.23.tar.gz
sha256sum mosquitto-2.0.23.tar.gz   # должен совпасть с хэшем выше
tar xzf mosquitto-2.0.23.tar.gz
diff -r mosquitto-2.0.23 /path/to/backend/mqtt/vendor/mosquitto-2.0.23
```

## Обновление версии

1. Скачать новый архив, сверить SHA-256.
2. Распаковать поверх: удалить старый `mosquitto-2.0.23/`, положить новый каталог.
3. Обновить версию + SHA-256 в этом файле и путь `mosquitto-<ver>` в `../Dockerfile`
   и правило в корневом `.gitattributes`.
