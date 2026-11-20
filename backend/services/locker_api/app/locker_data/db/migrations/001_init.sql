-- Статусы команд
CREATE TYPE command_status AS ENUM (
    'pending',    -- Ожидает отправки
    'sent',       -- Отправлена на устройство
    'delivered',  -- Доставлена устройству
    'executed',   -- Выполнена устройством
    'failed'      -- Ошибка выполнения
);

-- Типы команд
CREATE TYPE command_type AS ENUM (
    'open',       -- Открыть шкаф
    'close',      -- Закрыть шкаф
    'reboot',     -- Перезагрузить контроллер
    'config',     -- Обновить конфигурацию
    'relay_on',
    'relay_off',
    'relay_restart'
);

-- Связность шкафа (управляется WS-соединением)
CREATE TYPE locker_connectivity AS ENUM ('online', 'offline');

-- Состояние двери (управляется результатами команд open/close)
CREATE TYPE locker_door AS ENUM ('closed', 'open');

-- Операционный статус (управляется оператором / StateMonitor)
CREATE TYPE locker_op_status AS ENUM ('normal', 'maintenance', 'alarm');

-- Транспорт устройства: 'ws' (Ed25519/JSON, legacy) или 'mqtt' (релейные платы)
CREATE TYPE locker_transport AS ENUM ('ws', 'mqtt');

-- Таблица шкафов
CREATE TABLE lockers
(
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Инвентарный номер (например, "СО №777" или "ЩУ ТОИ №777").
    -- UNIQUE применяется только к state='active' (см. ux_lockers_inventory_active ниже),
    -- чтобы можно было пересоздать шкаф с прежним номером после soft-delete.
    inventory_number  VARCHAR(100) NOT NULL,

    -- Местоположение
    location_address  TEXT,
    location_lat      DOUBLE PRECISION,
    location_lon      DOUBLE PRECISION,

    -- Ed25519 публичный ключ устройства (base64url без паддинга).
    -- Устанавливается оператором при вводе шкафа в эксплуатацию.
    -- NULL означает, что устройство ещё не зарегистрировало ключ.
    public_key        TEXT,

    -- Транспорт устройства: 'mqtt' (релейные платы) для новых шкафов; 'ws'
    -- (Ed25519/JSON) остаётся только у legacy-записей и больше не создаётся.
    transport         locker_transport NOT NULL DEFAULT 'mqtt',

    -- Целочисленный идентификатор релейной платы (поле `id` в payload stat/rep,
    -- топик com/{relay_id}, broker-user relay-{relay_id}). NULL для legacy ws-шкафов.
    relay_id          INTEGER,

    -- Из stat платы; NULL до первого stat.
    board_version     SMALLINT,
    has_relays        BOOLEAN,

    -- Метаданные
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by        BIGINT,

    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_by        BIGINT,

    -- Текущее состояние (три ортогональных поля)
    connectivity       locker_connectivity NOT NULL DEFAULT 'offline',
    door               locker_door         NOT NULL DEFAULT 'closed',
    operational_status locker_op_status    NOT NULL DEFAULT 'normal',
    last_seen_at      TIMESTAMPTZ,
    last_opened_at    TIMESTAMPTZ,

    -- ID пользователя, последним успешно открывшего шкаф (см. LockerRepository.update_door).
    -- Записывается одной транзакцией с last_opened_at — оба поля согласованы.
    -- NULL — никогда не открывали либо последний OPEN был system-issued (created_by IS NULL).
    who_open_last     BIGINT,

    -- Дополнительная информация
    description       TEXT,
    metadata          JSONB        DEFAULT '{}',

    -- Soft-delete: 'active' — рабочий шкаф; 'archived' — удалён оператором
    -- (телеметрия и история по нему всё ещё доступны по UUID)
    state             VARCHAR(16)  NOT NULL DEFAULT 'active'
                       CHECK (state IN ('active','archived'))
);

CREATE INDEX idx_lockers_connectivity ON lockers (connectivity);
CREATE INDEX idx_lockers_door ON lockers (door);
CREATE INDEX idx_lockers_op_status ON lockers (operational_status);
CREATE INDEX idx_lockers_inventory_number ON lockers (inventory_number);
CREATE INDEX idx_lockers_last_seen ON lockers (last_seen_at);
CREATE INDEX idx_lockers_state ON lockers (state);

-- Partial unique: уникальность inventory_number только среди активных шкафов.
-- Архивные записи освобождают номер для повторного создания.
CREATE UNIQUE INDEX ux_lockers_inventory_active
    ON lockers (inventory_number) WHERE state = 'active';

-- relay_id уникален среди заданных значений (NULL допускают дубликаты для ws-шкафов).
CREATE UNIQUE INDEX ux_lockers_relay_id ON lockers (relay_id) WHERE relay_id IS NOT NULL;

-- Справочник типов датчиков
CREATE TABLE sensor_types
(
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(50)  NOT NULL UNIQUE,
    name        VARCHAR(100) NOT NULL,
    description TEXT,
    unit        VARCHAR(20),
    value_type  VARCHAR(20)  NOT NULL,
    created_at  TIMESTAMPTZ  DEFAULT now()
);

COMMENT ON TABLE sensor_types IS 'Справочник типов датчиков в системе';
COMMENT ON COLUMN sensor_types.code IS 'Уникальный код типа датчика (используется в API)';
COMMENT ON COLUMN sensor_types.name IS 'Человекочитаемое название типа датчика';
COMMENT ON COLUMN sensor_types.unit IS 'Единица измерения для отображения значений';
COMMENT ON COLUMN sensor_types.value_type IS 'Тип значения датчика: numeric, boolean, string';

INSERT INTO sensor_types (code, name, description, unit, value_type)
VALUES
    ('temperature', 'Температура', 'Датчик температуры', '°C', 'numeric'),
    ('humidity', 'Влажность', 'Датчик влажности', '%', 'numeric'),
    ('shock', 'Удар/Вибрация', 'Датчик удара и вибрации', NULL, 'boolean'),
    ('power', 'Питание', 'Источник питания релейной платы: 0 — от батареи, 1 — от сети', NULL, 'numeric'),
    ('intrusion', 'Взлом', 'Датчик несанкционированного доступа', NULL, 'boolean'),
    ('door', 'Состояние двери', 'Датчик состояния двери (открыта/закрыта)', NULL, 'boolean'),
    (
        'tamper', 'Вскрытие корпуса',
        'Датчик вскрытия корпуса шкафа (концевик/шлейф). Срабатывание → security event door_tamper.',
        NULL, 'boolean'
    ),
    ('battery', 'Заряд батареи', 'Уровень заряда батареи релейной платы', '%', 'numeric'),
    ('signal', 'Уровень сигнала', 'Уровень GSM-сигнала релейной платы', NULL, 'numeric'),
    (
        'net_type', 'Тип подключения',
        'Канал связи релейной платы: 0 — не определён, 1 — Ethernet, 2 — GSM', NULL, 'numeric'
    ),
    ('error', 'Ошибка платы', 'Плата сообщила об ошибке (stat = 2)', NULL, 'boolean');

-- sensor-type-states:begin
-- Идемпотентно: на существующей БД запускается как runbook (DEPLOYMENT.md §6);
-- states заполняются только если пусты, правки администратора не затираются.
ALTER TABLE sensor_types ADD COLUMN IF NOT EXISTS states JSONB
    CONSTRAINT sensor_types_states_is_array
    CHECK (states IS NULL OR jsonb_typeof(states) = 'array');

COMMENT ON COLUMN sensor_types.states IS
    'Подписи перечислимых значений: [{value, label, severity}]; NULL — не перечислимый';

INSERT INTO sensor_types (code, name, description, unit, value_type)
VALUES
    (
        'net_type', 'Тип подключения',
        'Канал связи релейной платы: 0 — не определён, 1 — Ethernet, 2 — GSM', NULL, 'numeric'
    ),
    ('error', 'Ошибка платы', 'Плата сообщила об ошибке (stat = 2)', NULL, 'boolean')
ON CONFLICT (code) DO NOTHING;

UPDATE sensor_types SET states = '[
    {"value": 0, "label": "От батареи", "severity": "warning"},
    {"value": 1, "label": "От сети", "severity": "normal"}
]'::jsonb
WHERE code = 'power' AND states IS NULL;

UPDATE sensor_types SET states = '[
    {"value": 0, "label": "Не определён", "severity": "warning"},
    {"value": 1, "label": "Ethernet", "severity": "normal"},
    {"value": 2, "label": "GSM", "severity": "normal"}
]'::jsonb
WHERE code = 'net_type' AND states IS NULL;

UPDATE sensor_types SET states = '[
    {"value": false, "label": "Норма", "severity": "normal"},
    {"value": true, "label": "Ошибка", "severity": "alert"}
]'::jsonb
WHERE code = 'error' AND states IS NULL;

UPDATE sensor_types SET states = '[
    {"value": false, "label": "Закрыта", "severity": "normal"},
    {"value": true, "label": "Открыта", "severity": "alert"}
]'::jsonb
WHERE code = 'door' AND states IS NULL;

UPDATE sensor_types SET states = '[
    {"value": false, "label": "Норма", "severity": "normal"},
    {"value": true, "label": "Взлом", "severity": "alert"}
]'::jsonb
WHERE code = 'intrusion' AND states IS NULL;

UPDATE sensor_types
SET description = 'Источник питания релейной платы: 0 — от батареи, 1 — от сети', unit = NULL
WHERE code = 'power' AND unit = 'V' AND description = 'Датчик напряжения питания';
-- sensor-type-states:end

-- Таблица датчиков
CREATE TABLE sensors
(
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    locker_id     UUID         NOT NULL REFERENCES lockers (id) ON DELETE CASCADE,

    -- Серийный номер датчика
    serial_number VARCHAR(100) NOT NULL,

    -- Тип датчика (FK на sensor_types)
    sensor_type_id INTEGER     NOT NULL REFERENCES sensor_types(id) ON DELETE RESTRICT,

    -- Текущее значение датчика как JSONB-обёртка {"v": <typed_value>}.
    -- Тип хранимого значения определяется sensor_types.value_type:
    --   numeric → {"v": 25.5}, boolean → {"v": true}, string → {"v": "abc"}.
    -- Репозиторий упаковывает Python-значение при записи и распаковывает при чтении.
    current_value JSONB,

    -- Метаданные
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Состояние датчика
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,

    -- Дополнительная информация
    metadata      JSONB        DEFAULT '{}'
);

-- Partial unique: уникальность серийника на шкафу только среди активных датчиков.
-- Soft-deleted (is_active=false) датчик освобождает серийный номер для повторного создания.
CREATE UNIQUE INDEX ux_sensors_serial_locker_active
    ON sensors (locker_id, serial_number) WHERE is_active = TRUE;
CREATE INDEX idx_sensors_locker ON sensors (locker_id);
CREATE INDEX idx_sensors_sensor_type_id ON sensors (sensor_type_id);

-- Таблица пороговых значений для датчиков
CREATE TABLE sensor_thresholds
(
    id              BIGSERIAL PRIMARY KEY,

    sensor_id       UUID            NOT NULL REFERENCES sensors (id) ON DELETE CASCADE,

    -- Минимальное допустимое значение
    min_value       DOUBLE PRECISION,

    -- Максимальное допустимое значение
    max_value       DOUBLE PRECISION,

    -- Фиксированное допустимое значение (для дискретных датчиков)
    fixed_value     TEXT,

    -- Метаданные
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    created_by      BIGINT,

    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_by      BIGINT,

    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Описание правила
    description     TEXT,

    CONSTRAINT check_threshold_values CHECK (
        (min_value IS NOT NULL OR max_value IS NOT NULL OR fixed_value IS NOT NULL)
    )
);

CREATE INDEX idx_sensor_thresholds_sensor ON sensor_thresholds (sensor_id);
CREATE INDEX idx_sensor_thresholds_active ON sensor_thresholds (is_active);

-- Таблица команд управления
CREATE TABLE locker_commands
(
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    locker_id     UUID           NOT NULL REFERENCES lockers (id) ON DELETE CASCADE,

    -- Тип команды
    type          command_type   NOT NULL,

    -- Статус выполнения
    status        command_status NOT NULL DEFAULT 'pending',

    -- Параметры команды (JSON)
    parameters    JSONB          DEFAULT '{}',

    -- Метаданные выполнения
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),
    created_by    BIGINT,

    sent_at       TIMESTAMPTZ,
    delivered_at  TIMESTAMPTZ,
    executed_at   TIMESTAMPTZ,
    failed_at     TIMESTAMPTZ,

    -- Результат выполнения / ошибка
    result        JSONB          DEFAULT '{}',
    error_message TEXT,

    -- Количество попыток отправки
    retry_count   INTEGER        NOT NULL DEFAULT 0,
    max_retries   INTEGER        NOT NULL DEFAULT 3,

    -- uint32 cmd_id, отправляемый плате в com/{id} и эхом возвращаемый в rep.
    -- Назначается при отправке MQTT-команды; NULL для ws-команд. Per-locker uniqueness.
    relay_cmd_id  BIGINT
);

CREATE INDEX idx_locker_commands_locker ON locker_commands (locker_id);
CREATE INDEX idx_locker_commands_status ON locker_commands (status);
CREATE INDEX idx_locker_commands_created ON locker_commands (created_at);

-- relay_cmd_id уникален в пределах локера (NULL допускают дубликаты для ws-команд).
CREATE UNIQUE INDEX ux_locker_commands_relay_cmd_id
    ON locker_commands (locker_id, relay_cmd_id) WHERE relay_cmd_id IS NOT NULL;

-- Таблица истории состояний шкафов (снапшоты)
CREATE TABLE locker_state_history
(
    id            BIGSERIAL PRIMARY KEY,

    locker_id     UUID         NOT NULL REFERENCES lockers (id) ON DELETE CASCADE,

    connectivity       locker_connectivity,
    door               locker_door,
    operational_status locker_op_status,

    -- Временная метка
    recorded_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Дополнительная информация
    metadata      JSONB        DEFAULT '{}'
);

CREATE INDEX idx_locker_state_history_locker ON locker_state_history (locker_id, recorded_at DESC);
CREATE INDEX idx_locker_state_history_recorded ON locker_state_history (recorded_at);

-- Persistent outbound queue для гарантированной доставки событий в history_api.
--
-- Каждый продьюсер (TelemetryProducer, CommandHistoryProducer, OfflineEventProducer,
-- ThresholdViolationProducer, IntrusionProducer, DoorTamperProducer, MediaProducer)
-- атомарно с бизнес-операцией пишет запись в эту очередь.
--
-- Фоновый воркер OutboundDispatcher:
--   1) каждые ~2с выбирает порцию недоставленных записей по `next_attempt_at`;
--   2) шлёт через RetryHTTPClient на `endpoint`;
--   3) при успехе ставит `delivered_at = now()`;
--   4) при ошибке инкрементирует `attempt_count`, выставляет
--      `next_attempt_at = now() + exp_backoff(attempt_count)`,
--      сохраняет `last_error`.
--
-- Записи с `delivered_at IS NOT NULL` старше OUTBOUND_RETENTION_DAYS чистятся
-- ретеншеном (тот же воркер, отдельный шаг).
CREATE TABLE outbound_history_events
(
    id              BIGSERIAL PRIMARY KEY,

    -- Доменный канал. Используется для метрик и для маршрутизации в dispatcher
    -- (например, channel='media' требует multipart-сборки из payload).
    channel         VARCHAR(40)  NOT NULL,

    -- Полный путь endpoint'а в history_api, например '/internal/api/v1/telemetry'.
    -- Хост/порт берётся из env HISTORY_API_URL.
    endpoint        TEXT         NOT NULL,

    -- Тело запроса как JSON. Для channel='media' содержит form_fields и сам файл
    -- в base64 (file_bytes_b64 / file_name / file_mime); dispatcher собирает из
    -- этого multipart/form-data.
    payload         JSONB        NOT NULL,

    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Доставка
    attempt_count   INT          NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_error      TEXT,
    delivered_at    TIMESTAMPTZ
);

-- Индекс для ретеншена доставленных.
CREATE INDEX ix_outbound_delivered_at
    ON outbound_history_events (delivered_at)
    WHERE delivered_at IS NOT NULL;

CREATE INDEX ix_outbound_channel ON outbound_history_events (channel);

-- outbox-dead-letter:begin
-- locker_id: FK ON DELETE CASCADE уносит недоставленные события физически
-- удалённого локера; soft-delete строку локера не трогает. NULL — без привязки.
ALTER TABLE outbound_history_events
    ADD COLUMN IF NOT EXISTS locker_id UUID REFERENCES lockers (id) ON DELETE CASCADE;
-- Dead-letter: после OUTBOUND_DISPATCHER_MAX_ATTEMPTS событие паркуется и
-- больше не выбирается fetch_pending.
ALTER TABLE outbound_history_events ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ;

-- Индекс с предикатом без dead_lettered_at (до dead-letter) пересоздаётся.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE indexname = 'ix_outbound_pending' AND indexdef NOT LIKE '%dead_lettered_at%'
    ) THEN
        DROP INDEX ix_outbound_pending;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_outbound_pending
    ON outbound_history_events (next_attempt_at, id)
    WHERE delivered_at IS NULL AND dead_lettered_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_outbound_locker_id
    ON outbound_history_events (locker_id)
    WHERE locker_id IS NOT NULL;
-- outbox-dead-letter:end

-- relay-photo-series:begin
-- Серии relay-фото: (шкаф, relay_id, event_id) в пределах эпохи; relay_id/event_id — u32.
CREATE TABLE IF NOT EXISTS relay_photo_series
(
    id                    BIGSERIAL   PRIMARY KEY,
    locker_id             UUID        NOT NULL REFERENCES lockers (id) ON DELETE CASCADE,
    relay_id              BIGINT      NOT NULL,
    event_id              BIGINT      NOT NULL,
    series_uid            UUID        NOT NULL,
    photos_cnt            INTEGER     NOT NULL,
    frame_sha             JSONB       NOT NULL DEFAULT '{}',
    total_bytes           BIGINT      NOT NULL DEFAULT 0,
    first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_frame_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    intrusion_enqueued_at TIMESTAMPTZ,
    superseded_at         TIMESTAMPTZ
);

COMMENT ON TABLE relay_photo_series IS 'Серии фото релейных плат';
COMMENT ON COLUMN relay_photo_series.series_uid IS 'Из него выводятся event_uid события и кадров';
COMMENT ON COLUMN relay_photo_series.frame_sha IS 'photo_id -> sha256 кадра без паддинга';
COMMENT ON COLUMN relay_photo_series.first_seen_at IS 'Начало эпохи серии';
COMMENT ON COLUMN relay_photo_series.superseded_at IS 'Серию сменила новая с тем же event_id';

CREATE UNIQUE INDEX IF NOT EXISTS ux_relay_photo_series_uid ON relay_photo_series (series_uid);
CREATE UNIQUE INDEX IF NOT EXISTS ux_relay_photo_series_current
    ON relay_photo_series (locker_id, relay_id, event_id) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_relay_photo_series_event ON relay_photo_series (locker_id, relay_id, event_id);
CREATE INDEX IF NOT EXISTS ix_relay_photo_series_last_frame ON relay_photo_series (last_frame_at);
CREATE INDEX IF NOT EXISTS ix_relay_photo_series_pending_intrusion
    ON relay_photo_series (first_seen_at) WHERE intrusion_enqueued_at IS NULL;
-- relay-photo-series:end

-- relay-control:begin
-- Идемпотентно; на существующей БД — runbook DEPLOYMENT.md §6. Запускать БЕЗ
-- psql -1: новое значение enum нельзя использовать в транзакции, где оно
-- добавлено, а индекс ниже на него ссылается.
ALTER TYPE command_type ADD VALUE IF NOT EXISTS 'relay_on';
ALTER TYPE command_type ADD VALUE IF NOT EXISTS 'relay_off';
ALTER TYPE command_type ADD VALUE IF NOT EXISTS 'relay_restart';

ALTER TABLE lockers ADD COLUMN IF NOT EXISTS board_version SMALLINT;
ALTER TABLE lockers ADD COLUMN IF NOT EXISTS has_relays BOOLEAN;

CREATE TABLE IF NOT EXISTS locker_relays
(
    locker_id       UUID         NOT NULL REFERENCES lockers (id) ON DELETE CASCADE,
    channel         SMALLINT     NOT NULL CHECK (channel BETWEEN 1 AND 4),
    name            VARCHAR(100),
    purpose         TEXT,
    control_enabled BOOLEAN      NOT NULL DEFAULT TRUE,
    is_protected    BOOLEAN      NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    reported_state  SMALLINT     CHECK (reported_state IN (0, 1, 2)),
    changed_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_by      BIGINT,
    PRIMARY KEY (locker_id, channel)
);

COMMENT ON TABLE locker_relays IS 'Каналы блока реле платы шкафа (r1..r4)';
COMMENT ON COLUMN locker_relays.is_protected IS 'Управлять каналом может только administrator';
COMMENT ON COLUMN locker_relays.reported_state IS 'Последнее состояние из stat: 0 выкл, 1 вкл, 2 ошибка';
COMMENT ON COLUMN locker_relays.changed_at IS 'Время последней смены reported_state';

CREATE UNIQUE INDEX IF NOT EXISTS ux_locker_commands_relay_active
    ON locker_commands (locker_id, (parameters ->> 'relay'))
    WHERE type IN ('relay_on', 'relay_off', 'relay_restart')
      AND status IN ('pending', 'sent', 'delivered');
-- relay-control:end
