-- ==============================================================================
-- SCUD Auth API Database Schema
-- Таблицы для cookie-авторизации: OTP коды, access tokens
-- ==============================================================================

-- OTP коды для SMS 2FA
CREATE TABLE otp_codes
(
    id         BIGSERIAL   PRIMARY KEY,
    phone      VARCHAR(20) NOT NULL,
    code_hash  VARCHAR(64) NOT NULL,       -- SHA-256(code)
    attempts   SMALLINT    NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индекс для поиска активного OTP по телефону
CREATE INDEX idx_otp_phone ON otp_codes (phone, expires_at) WHERE used_at IS NULL;

-- Access tokens (opaque, в БД хранится хэш)
CREATE TABLE access_tokens
(
    id          BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    token_hash  VARCHAR(64) NOT NULL UNIQUE,  -- SHA-256(opaque_token)
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent  TEXT,
    ip_address  INET
);

CREATE INDEX idx_at_hash ON access_tokens (token_hash) WHERE revoked_at IS NULL;
CREATE INDEX idx_at_user ON access_tokens (user_id);

-- Login rate-limit (per IP). Replaces in-memory _login_attempts dict.
CREATE TABLE login_rate_limit
(
    key            TEXT        PRIMARY KEY,    -- IP address (string form)
    attempts       INTEGER     NOT NULL DEFAULT 1,
    window_start   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_login_rate_limit_window ON login_rate_limit (window_start);

-- ==============================================================================
-- Step-up 2FA challenges (для подтверждения привилегированных команд шкафа)
-- ==============================================================================

CREATE TABLE step_up_challenges
(
    id            UUID         PRIMARY KEY,
    user_id       BIGINT       NOT NULL,
    scope_kind    VARCHAR(64)  NOT NULL,
    scope_payload JSONB        NOT NULL,
    code_hash     VARCHAR(64)  NOT NULL,
    attempts      SMALLINT     NOT NULL DEFAULT 0,
    max_attempts  SMALLINT     NOT NULL DEFAULT 5,
    expires_at    TIMESTAMPTZ  NOT NULL,
    used_at       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_step_up_challenges_user_active
    ON step_up_challenges (user_id)
    WHERE used_at IS NULL;

CREATE INDEX idx_step_up_challenges_expires
    ON step_up_challenges (expires_at)
    WHERE used_at IS NULL;

-- ==============================================================================
-- COMMENTS
-- ==============================================================================

COMMENT ON TABLE otp_codes IS 'Одноразовые коды для SMS 2FA при входе';
COMMENT ON TABLE access_tokens IS 'Сессионные access tokens (cookie-based auth)';
COMMENT ON TABLE login_rate_limit IS 'Per-key counters for login rate-limiting (window-based)';
COMMENT ON TABLE step_up_challenges
IS 'Step-up OTP-челленджи для подтверждения операций (привилегированные команды шкафа и т.п.)';
