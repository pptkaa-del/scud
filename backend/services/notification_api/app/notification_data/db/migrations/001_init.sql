-- ==============================================================================
-- SCUD Notification API Database Schema
-- Подсистема уведомлений (Подсистема 6 по Architecture.md)
-- ==============================================================================

-- ==============================================================================
-- ENUM TYPES
-- ==============================================================================

-- Тип уведомления
CREATE TYPE notification_type AS ENUM (
    'persistent'      -- Постоянные уведомления (внутри Системы)
);

-- ==============================================================================
-- CORE TABLES
-- ==============================================================================

-- Таблица уведомлений
CREATE TABLE notifications
(
    id                UUID              PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Тип уведомления
    type              notification_type NOT NULL,

    -- Текст уведомления
    text              TEXT              NOT NULL,

    -- Теги для фильтрации (формат: {"key1": "value1", "key2": "value2"})
    tags              JSONB             NOT NULL DEFAULT '{}',

    -- Дата начала действия (для persistent)
    start_at          TIMESTAMPTZ,

    -- Статус
    is_cancelled      BOOLEAN           NOT NULL DEFAULT FALSE,

    -- Метаданные
    created_at        TIMESTAMPTZ       NOT NULL DEFAULT now(),
    created_by        BIGINT,

    -- Ограничения по типам уведомлений
    CONSTRAINT check_persistent CHECK (
        type != 'persistent' OR start_at IS NOT NULL
    )
);

-- Таблица получателей уведомлений
-- Для persistent: хранит user_id или role
CREATE TABLE notification_recipients
(
    id                BIGSERIAL PRIMARY KEY,

    notification_id   UUID        NOT NULL REFERENCES notifications (id) ON DELETE CASCADE,

    -- ID пользователя (для persistent)
    user_id           BIGINT,

    -- Роль (для persistent)
    role              VARCHAR(50),

    -- Ровно одно из двух полей должно быть заполнено
    CONSTRAINT check_recipient_type CHECK (
        (user_id IS NOT NULL)::INT
        + (role IS NOT NULL)::INT = 1
    )
);

-- ==============================================================================
-- INDEXES
-- ==============================================================================

-- Основные индексы notifications
CREATE INDEX idx_notifications_type ON notifications (type);
CREATE INDEX idx_notifications_is_cancelled ON notifications (is_cancelled);
CREATE INDEX idx_notifications_created_at ON notifications (created_at DESC);

-- GIN-индекс для фильтрации по тегам (@> оператор)
CREATE INDEX idx_notifications_tags ON notifications USING GIN (tags);

-- Индексы notification_recipients
CREATE INDEX idx_recipients_notification ON notification_recipients (notification_id);
CREATE INDEX idx_recipients_user_id ON notification_recipients (user_id);
CREATE INDEX idx_recipients_role ON notification_recipients (role);

-- ==============================================================================
-- MAX MESSENGER OUTBOX
-- ==============================================================================

-- Очередь исходящих сообщений в Max-бот: OTP-коды второго фактора
-- + потенциально другие нотификации. При вставке max_user_id может быть NULL
-- (пользователь ещё не привязал Max); long-polling воркер при bot_started
-- проставит привязку и доставит pending записи. attempts/last_error — для
-- видимости в БД, что отправка идёт.
-- channel фиксирует транспорт, которым сообщение реально было отправлено.
CREATE TABLE outbound_max_messages
(
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT       NOT NULL,
    max_user_id BIGINT,
    -- body хранит plaintext OTP только до доставки. После терминального
    -- состояния (mark_sent/mark_failed) обнуляется (SEC-18: не держать
    -- второй фактор в БД-бэкапах), поэтому NULLABLE.
    body        VARCHAR(4000),
    purpose     VARCHAR(32)  NOT NULL,
    attempts    INT          NOT NULL DEFAULT 0,
    sent_at     TIMESTAMPTZ,
    failed_at   TIMESTAMPTZ,
    last_error  TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ  NOT NULL,
    channel     VARCHAR(16)  NOT NULL DEFAULT 'max',
    address     VARCHAR(64)
);

CREATE INDEX idx_outbound_max_pending ON outbound_max_messages (expires_at)
    WHERE sent_at IS NULL AND failed_at IS NULL;
CREATE INDEX idx_outbound_max_user_pending ON outbound_max_messages (user_id)
    WHERE sent_at IS NULL AND failed_at IS NULL;

-- outbound-max-body-nullable:begin
ALTER TABLE outbound_max_messages ALTER COLUMN body DROP NOT NULL;
-- outbound-max-body-nullable:end

-- outbound-max-channel:begin
ALTER TABLE outbound_max_messages
    ADD COLUMN IF NOT EXISTS channel VARCHAR(16) NOT NULL DEFAULT 'max',
    ADD COLUMN IF NOT EXISTS address VARCHAR(64);
-- outbound-max-channel:end

-- Single-row таблица состояния long-polling воркера Max-бота.
-- CHECK (id = 1) гарантирует, что строк в ней не больше одной.
CREATE TABLE bot_poller_state
(
    id         INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    marker     TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO bot_poller_state (id, marker) VALUES (1, NULL);

-- ==============================================================================
-- COMMENTS
-- ==============================================================================

COMMENT ON TYPE notification_type IS 'Тип уведомления: persistent';
COMMENT ON TABLE notifications IS 'Уведомления системы СКУД';
COMMENT ON COLUMN notifications.tags IS 'Произвольные теги для фильтрации (JSONB: {"key": "value"})';
COMMENT ON TABLE notification_recipients IS 'Получатели уведомлений (user_id или роли)';
COMMENT ON TABLE outbound_max_messages IS 'Очередь исходящих сообщений в Max-бот (OTP и пр.)';
COMMENT ON TABLE bot_poller_state IS 'Single-row состояние long-polling воркера Max-бота';
