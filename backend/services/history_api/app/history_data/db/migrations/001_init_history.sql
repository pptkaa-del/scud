-- =================================================================
-- History API Database Schema
-- Подсистема 5: Хранение исторических данных
-- =================================================================

-- =================================================================
-- 1. ПАРТИЦИОНИРОВАННАЯ ТАБЛИЦА ТЕЛЕМЕТРИИ ДАТЧИКОВ
-- =================================================================

-- Главная партиционированная таблица для телеметрии
-- Партиционирование по месяцам для оптимизации работы с временными данными
CREATE TABLE sensor_telemetry (
    id BIGSERIAL,
    locker_id UUID NOT NULL,
    sensor_id UUID NOT NULL,
    sensor_type_code VARCHAR(50) NOT NULL,  -- Денормализация для быстрых фильтров
    value DOUBLE PRECISION,                 -- Для числовых значений
    value_text TEXT,                        -- Для строковых/булевых значений
    recorded_at TIMESTAMPTZ NOT NULL,
    metadata JSONB DEFAULT '{}',
    -- ARC-02: продьюсер-генерируемый UUID для идемпотентности повторной
    -- доставки (at-least-once). NULL допустим для обратной совместимости.
    event_uid UUID
) PARTITION BY RANGE (recorded_at);

COMMENT ON TABLE sensor_telemetry IS 'Телеметрия датчиков с партиционированием по месяцам (хранение 2+ лет)';
COMMENT ON COLUMN sensor_telemetry.sensor_type_code IS 'Код типа датчика (температура, влажность и т.д.)';
COMMENT ON COLUMN sensor_telemetry.value IS 'Числовое значение датчика';
COMMENT ON COLUMN sensor_telemetry.value_text IS 'Текстовое/булевое значение датчика';

-- Месячные партиции создаются автоматически RetentionService._create_future_partitions()
-- через функцию create_monthly_partition() (см. ниже в этом файле): на каждой
-- итерации сервис гарантирует партиции для текущего месяца + LOOKAHEAD_MONTHS
-- вперёд. Старые партиции удаляются _drop_old_partitions по retention_months.
--
-- DEFAULT-партиция — safety net: при штатной работе сервиса должна оставаться
-- пустой; если строки попадают сюда — это инцидент (RetentionService не
-- работает или отстал), который нужно мониторить.
CREATE TABLE IF NOT EXISTS sensor_telemetry_default PARTITION OF sensor_telemetry DEFAULT;

-- Индексы для sensor_telemetry
-- BRIN индексы очень компактны и эффективны для времен ных данных
CREATE INDEX idx_telemetry_recorded_brin ON sensor_telemetry
    USING BRIN (recorded_at) WITH (pages_per_range = 128);

-- B-tree индексы для точных поисков
CREATE INDEX idx_telemetry_locker_recorded ON sensor_telemetry (locker_id, recorded_at DESC);
CREATE INDEX idx_telemetry_sensor_recorded ON sensor_telemetry (sensor_id, recorded_at DESC);
CREATE INDEX idx_telemetry_sensor_type ON sensor_telemetry (sensor_type_code, recorded_at DESC);

-- ARC-02: идемпотентность повторной доставки батча телеметрии.
-- UNIQUE-индекс на партиционированной таблице ОБЯЗАН включать ключ
-- партиционирования (recorded_at) — ограничение Postgres. NULL event_uid
-- не конфликтует (NULL'ы различны), поэтому строки без uid вставляются как
-- раньше. ON CONFLICT (event_uid, recorded_at) DO NOTHING в insert_batch.
CREATE UNIQUE INDEX ux_telemetry_event_uid
    ON sensor_telemetry (event_uid, recorded_at);

-- =================================================================
-- 2. ТАБЛИЦА СОБЫТИЙ БЕЗОПАСНОСТИ
-- =================================================================

-- Расширение для аудита step-up 2FA: 'info'/'warning'/'alert' добавлены к
-- датчиковым 'low'/'medium'/'high'/'critical'. См. shared.enums.EventSeverity.
CREATE TYPE event_severity AS ENUM (
    'low', 'medium', 'high', 'critical', 'info', 'warning', 'alert'
);

CREATE TABLE security_events (
    id BIGSERIAL PRIMARY KEY,
    -- NULL разрешён для аудит-событий, не привязанных к конкретному шкафу
    -- (например, command_2fa_failed для несуществующего challenge_id).
    locker_id UUID,
    sensor_id UUID,
    -- 'threshold_violation', 'intrusion', 'offline', 'door_tamper',
    -- 'command_2fa_issued', 'command_2fa_verified', 'command_2fa_failed',
    -- 'command_2fa_mismatch', 'command_late_execution'.
    event_type VARCHAR(50) NOT NULL,
    severity event_severity NOT NULL DEFAULT 'medium',
    violated_thresholds JSONB DEFAULT '[]',  -- Массив нарушенных правил
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolved_by BIGINT,  -- User ID кто закрыл событие
    metadata JSONB DEFAULT '{}',
    -- ARC-02: продьюсер-генерируемый UUID для идемпотентности повторной
    -- доставки AUDIT-события (at-least-once). NULL — обратная совместимость.
    event_uid UUID
);

COMMENT ON TABLE security_events IS 'События безопасности: нарушения пороговых значений, взломы, отключения';
COMMENT ON COLUMN security_events.event_type IS 'Тип события безопасности';
COMMENT ON COLUMN security_events.severity IS 'Уровень критичности события';
COMMENT ON COLUMN security_events.violated_thresholds IS 'JSON массив нарушенных пороговых значений';

CREATE INDEX idx_security_events_locker_occurred ON security_events (locker_id, occurred_at DESC);
CREATE INDEX idx_security_events_occurred ON security_events (occurred_at DESC);
CREATE INDEX idx_security_events_severity ON security_events (severity, occurred_at DESC);
CREATE INDEX idx_security_events_unresolved ON security_events (occurred_at DESC)
    WHERE resolved_at IS NULL;

-- ARC-02: идемпотентность повторной доставки security-event.
-- Множественные NULL event_uid допустимы (NULL'ы различны в UNIQUE-индексе),
-- так что legacy-события без uid не конфликтуют. ON CONFLICT (event_uid)
-- DO NOTHING в SecurityEventRepository.create.
CREATE UNIQUE INDEX ux_security_events_event_uid
    ON security_events (event_uid);

-- =================================================================
-- 3. ТАБЛИЦА ИСТОРИИ КОМАНД
-- =================================================================

CREATE TABLE command_history (
    id BIGSERIAL PRIMARY KEY,
    command_id UUID NOT NULL UNIQUE,  -- Связь с locker_commands.id
    locker_id UUID NOT NULL,
    locker_inventory_number VARCHAR(100),  -- Денормализация для отчетов
    command_type VARCHAR(50) NOT NULL,
    parameters JSONB DEFAULT '{}',
    created_by BIGINT,
    user_phone VARCHAR(20),           -- Денормализация для отчетов
    user_full_name VARCHAR(255),      -- Денормализация для отчетов
    created_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ,
    executed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    status VARCHAR(20) NOT NULL,
    result JSONB DEFAULT '{}',
    error_message TEXT
);

COMMENT ON TABLE command_history
IS 'История всех отправленных команд на шкафы с денормализованными данными пользователей';
COMMENT ON COLUMN command_history.command_id IS 'UUID команды из таблицы locker_commands';
COMMENT ON COLUMN command_history.locker_inventory_number IS 'Инвентарный номер шкафа (денормализация)';
COMMENT ON COLUMN command_history.user_phone IS 'Телефон пользователя (денормализация для отчетов)';

CREATE INDEX idx_command_history_locker_created ON command_history (locker_id, created_at DESC);
CREATE INDEX idx_command_history_user_created ON command_history (created_by, created_at DESC);
CREATE INDEX idx_command_history_created ON command_history (created_at DESC);
CREATE INDEX idx_command_history_status ON command_history (status, created_at DESC);
CREATE INDEX idx_command_history_command_id ON command_history (command_id);

-- =================================================================
-- 4. ТАБЛИЦА МЕДИА-ФАЙЛОВ ШКАФОВ (фото)
-- =================================================================

CREATE TYPE media_kind AS ENUM ('photo');

CREATE TABLE locker_media (
    id BIGSERIAL PRIMARY KEY,
    locker_id UUID NOT NULL,
    sensor_id UUID,
    security_event_id BIGINT REFERENCES security_events(id) ON DELETE SET NULL,
    kind media_kind NOT NULL,
    mime_type VARCHAR(64) NOT NULL,
    bytes BYTEA NOT NULL,
    size_bytes INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    duration_ms INTEGER,
    captured_at TIMESTAMPTZ NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    uploaded_by BIGINT,
    description TEXT,
    metadata JSONB NOT NULL DEFAULT '{}',
    deleted_at TIMESTAMPTZ,
    -- ARC-02: продьюсер-генерируемый UUID для идемпотентности повторной
    -- доставки медиа (10 MB не дублируются при at-least-once). NULL —
    -- обратная совместимость.
    event_uid UUID
);

COMMENT ON TABLE locker_media IS 'Метаданные и содержимое медиа-файлов со шкафов (фото)';
COMMENT ON COLUMN locker_media.security_event_id IS
    'Связь с событием безопасности (intrusion, door_tamper). NULL если медиа не привязано к событию';
COMMENT ON COLUMN locker_media.bytes IS 'Бинарное содержимое медиа-файла';
COMMENT ON COLUMN locker_media.duration_ms IS 'Длительность; для photo всегда NULL';
COMMENT ON COLUMN locker_media.deleted_at IS 'Soft-delete: время удаления вручную, NULL для активного медиа';

CREATE INDEX idx_locker_media_event
    ON locker_media (security_event_id)
    WHERE security_event_id IS NOT NULL;
CREATE INDEX idx_locker_media_locker_time
    ON locker_media (locker_id, captured_at DESC);
CREATE INDEX idx_locker_media_kind ON locker_media (kind);
CREATE INDEX idx_locker_media_locker_active
    ON locker_media (locker_id, captured_at DESC)
    WHERE deleted_at IS NULL;

-- ARC-02: идемпотентность повторной доставки медиа. Множественные NULL
-- event_uid допустимы. ON CONFLICT (event_uid) DO NOTHING в MediaRepository.insert.
CREATE UNIQUE INDEX ux_locker_media_event_uid
    ON locker_media (event_uid);

-- media-security-event-uid:begin
ALTER TABLE locker_media ADD COLUMN IF NOT EXISTS security_event_uid UUID;

COMMENT ON COLUMN locker_media.security_event_uid IS
    'event_uid события безопасности для отложенной привязки медиа';

CREATE INDEX IF NOT EXISTS idx_locker_media_pending_event_uid
    ON locker_media (security_event_uid)
    WHERE security_event_id IS NULL AND security_event_uid IS NOT NULL;
-- media-security-event-uid:end

-- =================================================================
-- 5. ТАБЛИЦА ПРЕДРАСЧИТАННЫХ АГРЕГАТОВ
-- =================================================================

CREATE TYPE aggregation_type AS ENUM ('hourly', 'daily', 'monthly');

CREATE TABLE telemetry_aggregations (
    id BIGSERIAL PRIMARY KEY,
    locker_id UUID NOT NULL,
    sensor_id UUID NOT NULL,
    sensor_type_code VARCHAR(50) NOT NULL,
    aggregation_type aggregation_type NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    value_avg DOUBLE PRECISION,
    value_min DOUBLE PRECISION,
    value_max DOUBLE PRECISION,
    value_sum DOUBLE PRECISION,
    value_stddev DOUBLE PRECISION,  -- Стандартное отклонение
    sample_count BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE telemetry_aggregations IS 'Предрасчитанные агрегаты телеметрии (часовые/дневные/месячные)';
COMMENT ON COLUMN telemetry_aggregations.aggregation_type IS 'Тип агрегации: hourly, daily, monthly';
COMMENT ON COLUMN telemetry_aggregations.sample_count IS 'Количество сэмплов использованных для агрегации';

CREATE UNIQUE INDEX ux_aggregations_sensor_type_period
    ON telemetry_aggregations (sensor_id, aggregation_type, period_start);

CREATE INDEX idx_aggregations_locker_type_period
    ON telemetry_aggregations (locker_id, aggregation_type, period_start DESC);

CREATE INDEX idx_aggregations_sensor_type_code_period
    ON telemetry_aggregations (sensor_type_code, aggregation_type, period_start DESC);

-- =================================================================
-- 6. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
-- =================================================================

-- Функция для автоматического создания новых партиций
CREATE OR REPLACE FUNCTION create_monthly_partition(table_name TEXT, start_date DATE)
RETURNS TEXT AS $$
DECLARE
    partition_name TEXT;
    start_ts TEXT;
    end_ts TEXT;
BEGIN
    partition_name := table_name || '_' || to_char(start_date, 'YYYY_MM');
    start_ts := to_char(start_date, 'YYYY-MM-DD');
    end_ts := to_char(start_date + INTERVAL '1 month', 'YYYY-MM-DD');

    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
        partition_name, table_name, start_ts, end_ts
    );

    RETURN partition_name;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION create_monthly_partition IS 'Создает новую месячную партицию для партиционированной таблицы';

-- =================================================================
-- ЗАВЕРШЕНИЕ МИГРАЦИИ
-- =================================================================

-- Вставка метаданных о миграции
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT
);

INSERT INTO schema_migrations (version, description)
VALUES (1, 'Initial history database schema with partitioned telemetry');
