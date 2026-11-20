-- ==============================================================================
-- SCUD User API Database Schema
-- Полная инициализация базы данных для подсистемы управления пользователями
-- ==============================================================================

-- ==============================================================================
-- ENUM TYPES
-- ==============================================================================

-- Состояние учетной записи пользователя
-- Допустимые переходы:
--   active <---> deactivated
--     |               |
--     +--> deleted <--+
CREATE TYPE user_state AS ENUM (
    'active',
    'deactivated',
    'deleted'
);

-- Роли пользователей согласно Architecture.md
CREATE TYPE user_role AS ENUM (
    'worker',         -- Рабочий: открытие двери, создание заявок
    'operator',       -- Оператор: + управление пользователями, просмотр логов
    'administrator'   -- Администратор: + контроль работоспособности, управление контроллерами
);

-- Статус заявки на экстренное открытие
CREATE TYPE emergency_request_status AS ENUM (
    'pending',   -- Ожидает обработки
    'closed'     -- Закрыта оператором/администратором
);

-- Тип действия для логирования заявок
CREATE TYPE emergency_request_action AS ENUM (
    'created',  -- Заявка создана
    'closed',   -- Заявка закрыта
    'viewed'    -- Заявка просмотрена
);

-- ==============================================================================
-- CORE TABLES
-- ==============================================================================

-- Таблица подрядчиков
CREATE TABLE contractors
(
    id         BIGSERIAL PRIMARY KEY,
    name       VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by BIGINT,

    -- Soft-delete: 'active' — рабочий подрядчик; 'archived' — удалён.
    -- Сохраняем запись для аудита (созданные пользователи ссылаются на contractor_id).
    state      VARCHAR(16) NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','archived'))
);

CREATE INDEX idx_contractors_state ON contractors (state);

-- Таблица пользователей
CREATE TABLE users
(
    id            BIGSERIAL PRIMARY KEY,

    -- Валидируется приложением
    phone         VARCHAR(20)  NOT NULL,

    full_name     VARCHAR(255) NOT NULL,
    contractor_id BIGINT       REFERENCES contractors (id) ON DELETE SET NULL,

    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by    BIGINT REFERENCES users (id),

    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_by    BIGINT REFERENCES users (id),

    state         user_state   NOT NULL DEFAULT 'active',
    role          user_role    NOT NULL DEFAULT 'worker',

    expire_date   TIMESTAMPTZ,

    -- Идентификатор пользователя в боте мессенджера Max (NULL до привязки).
    max_user_id   BIGINT
);

-- Уникальный индекс для телефонов (исключая удаленных пользователей)
CREATE UNIQUE INDEX ux_users_phone_not_deleted ON users (phone) WHERE state <> 'deleted';

-- Один Max-аккаунт может быть привязан только к одному активному пользователю.
CREATE UNIQUE INDEX ux_users_max_user_id_not_deleted ON users (max_user_id)
    WHERE max_user_id IS NOT NULL AND state <> 'deleted';

-- Добавляем внешний ключ для contractors.created_by после создания users
ALTER TABLE contractors
    ADD CONSTRAINT fk_contractors_created_by
        FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL;

-- Одноразовые инвайт-токены для привязки Max к пользователю.
-- POST /api/v1/users выдаёт токен админу, ссылка max.ru/<bot>?start=<token>;
-- notification_api ловит bot_started и через /internal/user/by-invite
-- ставит users.max_user_id и помечает токен used_at=now().
CREATE TABLE max_invite_tokens
(
    token      VARCHAR(64) PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_max_invite_tokens_user_id ON max_invite_tokens (user_id);
CREATE INDEX idx_max_invite_tokens_active ON max_invite_tokens (expires_at)
    WHERE used_at IS NULL;

-- Таблица аутентификации пользователей
CREATE TABLE user_auth
(
    user_id       BIGINT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    password_hash TEXT    NOT NULL,
    -- 2FA включён по умолчанию для всех новых пользователей. Исключение —
    -- bootstrap-администратор (заводится с require_2fa=FALSE явно), т.к. у
    -- него ещё нет привязки Max в момент старта системы.
    require_2fa   BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ
);

-- Таблица заявок на доступ
CREATE TABLE access_requests
(
    id            BIGSERIAL PRIMARY KEY,
    name          VARCHAR(50) NOT NULL,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by      BIGINT REFERENCES users (id),
    contractor_id BIGINT REFERENCES contractors (id)
);

-- Таблица допусков пользователей
CREATE TABLE user_accesses
(
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT REFERENCES users (id) ON DELETE CASCADE NOT NULL,

    start_at    TIMESTAMPTZ                                    NOT NULL,
    end_at      TIMESTAMPTZ,

    granted_by  BIGINT REFERENCES users (id),
    request_id  BIGINT REFERENCES access_requests (id),

    full_access BOOLEAN                                        NOT NULL DEFAULT FALSE,

    is_active   BOOLEAN                                        NOT NULL DEFAULT TRUE,

    created_at  TIMESTAMPTZ                                    NOT NULL DEFAULT now()
);

-- Таблица связи допусков и шкафов
CREATE TABLE user_access_lockers
(
    user_access_id BIGINT REFERENCES user_accesses (id) ON DELETE CASCADE,
    locker_id      UUID NOT NULL,
    PRIMARY KEY (user_access_id, locker_id)
);

-- ==============================================================================
-- EMERGENCY REQUESTS MODULE
-- ==============================================================================

-- Основная таблица заявок на экстренное открытие
CREATE TABLE emergency_requests
(
    id         BIGSERIAL PRIMARY KEY,

    -- Пользователь, подавший заявку
    user_id    BIGINT                   NOT NULL REFERENCES users (id) ON DELETE CASCADE,

    -- Идентификатор шкафа (внешний ключ к сервису шкафов)
    locker_id  UUID                     NOT NULL,

    -- Статус заявки
    status     emergency_request_status NOT NULL DEFAULT 'pending',

    -- Дата и время создания заявки
    created_at TIMESTAMPTZ              NOT NULL DEFAULT now(),

    -- Дата и время закрытия заявки
    closed_at  TIMESTAMPTZ,

    -- Кто закрыл заявку (оператор или администратор)
    closed_by  BIGINT REFERENCES users (id),

    -- Проверка: если закрыта, должны быть заполнены closed_at и closed_by
    CONSTRAINT check_closed_fields
        CHECK (
            (status = 'pending' AND closed_at IS NULL AND closed_by IS NULL)
            OR (status = 'closed' AND closed_at IS NOT NULL AND closed_by IS NOT NULL)
        )
);

-- Таблица логов действий с заявками (audit trail)
CREATE TABLE emergency_request_logs
(
    id           BIGSERIAL PRIMARY KEY,

    -- Ссылка на заявку
    request_id   BIGINT                   NOT NULL REFERENCES emergency_requests (id) ON DELETE CASCADE,

    -- Тип действия
    action       emergency_request_action NOT NULL,

    -- Кто выполнил действие
    performed_by BIGINT                   NOT NULL REFERENCES users (id),

    -- Когда выполнено
    performed_at TIMESTAMPTZ              NOT NULL DEFAULT now(),

    -- Дополнительные детали (JSON)
    details      JSONB
);

-- ==============================================================================
-- INDEXES
-- ==============================================================================

-- Индексы для user_accesses
CREATE INDEX idx_user_system_access_user
    ON user_accesses (user_id);

CREATE INDEX idx_user_accesses_is_active
    ON user_accesses (is_active);

CREATE INDEX idx_user_accesses_end_at
    ON user_accesses (end_at) WHERE is_active = TRUE;

-- Индексы для user_access_lockers
CREATE INDEX idx_user_access_lockers_locker
    ON user_access_lockers (locker_id);

-- Индексы для users
CREATE INDEX idx_users_role
    ON users (role);

CREATE INDEX idx_users_contractor_id
    ON users (contractor_id);

CREATE INDEX idx_users_state
    ON users (state);

-- Индексы для emergency_requests
CREATE INDEX idx_emergency_requests_status
    ON emergency_requests (status);

CREATE INDEX idx_emergency_requests_user
    ON emergency_requests (user_id);

CREATE INDEX idx_emergency_requests_locker
    ON emergency_requests (locker_id);

CREATE INDEX idx_emergency_requests_created_at
    ON emergency_requests (created_at DESC);

-- Индексы для emergency_request_logs
CREATE INDEX idx_emergency_request_logs_request
    ON emergency_request_logs (request_id);

CREATE INDEX idx_emergency_request_logs_performed_at
    ON emergency_request_logs (performed_at DESC);

CREATE INDEX idx_emergency_request_logs_action
    ON emergency_request_logs (action);

-- ==============================================================================
-- COMMENTS (Documentation)
-- ==============================================================================

COMMENT ON TYPE user_state IS 'Состояние учетной записи пользователя';
COMMENT ON TYPE user_role IS 'Роль пользователя в системе СКУД';
COMMENT ON COLUMN users.role IS 'Роль пользователя: worker/operator/administrator';

COMMENT ON TABLE emergency_requests IS 'Заявки на экстренное открытие шкафов';
COMMENT ON COLUMN emergency_requests.locker_id IS 'ID шкафа из внешнего сервиса';

COMMENT ON TABLE emergency_request_logs IS 'Аудит-лог всех действий с заявками';
COMMENT ON COLUMN emergency_request_logs.details IS 'Дополнительная информация в формате JSON';
