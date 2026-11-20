// relay-block.js — карточка «Блок реле» на странице шкафа (общая для ролей)

const RELAYS_REFRESH_MS = 5000;
const RELAYS_FETCH_TIMEOUT_MS = 4000;
const COMMAND_POLL_MS = 2000;
const COMMAND_TTL_MS = 60000;
const COMMAND_DEADLINE_GRACE_MS = 10000;
const STALE_REPORT_MS = 350000;
const OTP_LENGTH = 6;

const CONNECTIVITY_ONLINE = 'online';

const ACTION = {
    ON: 'relay_on',
    OFF: 'relay_off',
    RESTART: 'relay_restart'
};

const ACTION_UI = {
    [ACTION.ON]: {
        button: 'Вкл', icon: 'fa-power-off', btnClass: 'relay-btn--on',
        verb: 'Включить', progress: 'Включение…', confirm: false
    },
    [ACTION.OFF]: {
        button: 'Выкл', icon: 'fa-power-off', btnClass: 'relay-btn--off',
        verb: 'Выключить', progress: 'Выключение…', confirm: true,
        warning: 'Подключённое оборудование будет обесточено.'
    },
    [ACTION.RESTART]: {
        button: 'Перезапуск', icon: 'fa-redo', btnClass: 'relay-btn--restart',
        verb: 'Перезапустить', progress: 'Перезапуск…', confirm: true,
        warning: 'Подключённое оборудование будет кратковременно обесточено.'
    }
};
const ACTION_ORDER = [ACTION.ON, ACTION.OFF, ACTION.RESTART];

const REPORTED_STATE = {
    0: { label: 'Выкл', mod: 'off' },
    1: { label: 'Вкл', mod: 'on' },
    2: { label: 'Ошибка', mod: 'error' }
};
const REPORTED_UNKNOWN = { label: 'нет данных', mod: 'unknown' };
const NO_RELAY_DATA_TEXT = 'Нет данных о реле: плата ещё не сообщила о блоке реле';
const NO_RELAY_MODULE_TEXT = 'Нет данных о реле: модуль реле на плате выключен';

const COMMAND_STATUS = {
    PENDING: 'pending',
    SENT: 'sent',
    DELIVERED: 'delivered',
    EXECUTED: 'executed',
    FAILED: 'failed'
};
const ACTIVE_STATUSES = new Set([COMMAND_STATUS.PENDING, COMMAND_STATUS.SENT, COMMAND_STATUS.DELIVERED]);

const HTTP_ACCEPTED = 202;
const HTTP_UNAUTHORIZED = 401;

const ERROR_TEXT = {
    'relay-block-absent': 'У шкафа нет блока реле',
    'board-offline': 'Плата не в сети — команда не отправлена',
    'relay-command-in-progress': 'По этому реле уже выполняется команда',
    'relay-channel-unavailable': 'Канал реле недоступен для управления',
    'relay-channel-protected': 'Защищённый канал: управлять может только администратор',
    'relay-command-forbidden': 'Ваша роль не может управлять реле',
    'relay-invalid-parameters': 'Некорректный номер реле',
    'max-binding-required': 'Для подтверждения команды привяжите аккаунт Max'
};
const DEVICE_REASON_TEXT = {
    failed: 'плата сообщила об ошибке',
    not_supported: 'плата не поддерживает команду'
};
const RELAY_CONFLICT_CODES = new Set([
    'relay-block-absent', 'board-offline', 'relay-command-in-progress',
    'relay-channel-unavailable', 'relay-channel-protected',
    'relay-command-forbidden', 'relay-invalid-parameters'
]);
const STATUS_TEXT = {
    401: 'Сессия истекла — войдите заново',
    403: 'Недостаточно прав для управления реле',
    404: 'Шкаф или команда не найдены',
    409: 'Команда отклонена: конфликт состояния',
    422: 'Некорректные параметры команды'
};

const STYLE_ID = 'relay-block-style';
const VERSION_QUERY = new URL(import.meta.url).search;
const STYLE_HREF = new URL(`relay-block.css${VERSION_QUERY}`, import.meta.url).href;

let state = null;
let generation = 0;

function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement('link');
    link.id = STYLE_ID;
    link.rel = 'stylesheet';
    link.href = STYLE_HREF;
    document.head.appendChild(link);
}

function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, ch => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
    ));
}

function formatTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
}

function problemCode(body) {
    if (!body || typeof body !== 'object') return null;
    if (typeof body.code === 'string') return body.code;
    if (typeof body.error_code === 'string') return body.error_code;
    if (typeof body.type === 'string') {
        const tail = body.type.split('/').pop();
        return tail || null;
    }
    return null;
}

function errorText(status, body, fallback) {
    const code = problemCode(body);
    if (code && ERROR_TEXT[code]) return ERROR_TEXT[code];
    if (body?.detail) return body.detail;
    return STATUS_TEXT[status] || `${fallback} (HTTP ${status})`;
}

function actionTitle(type, channel) {
    return `${ACTION_UI[type]?.verb || 'Команда'} реле ${channel}`;
}

function isOnline() {
    return state?.data?.connectivity === CONNECTIVITY_ONLINE;
}

function alive(gen) {
    if (gen !== generation || !state) return false;
    if (!state.container.isConnected) {
        unmount();
        return false;
    }
    return true;
}

function notify(message, kind) {
    try {
        state?.notify?.(escapeHtml(message), kind);
    } catch (err) {
        console.error('Ошибка вывода уведомления:', err);
    }
}

function lockerLabel() {
    try {
        return state?.getLockerLabel?.() || '';
    } catch (_) {
        return '';
    }
}

async function request(path, options = {}) {
    const response = await fetch(`${state.apiBase}/locker/${state.lockerId}${path}`, options);
    const body = await response.json().catch(() => null);
    return { response, body };
}

// ── Разметка ──────────────────────────────────────────────────

function renderSkeleton() {
    state.container.innerHTML = `
        <div class="info-card relay-block-card" data-relay-card style="display:none;">
            <div class="card-header">
                <h3><i class="fas fa-plug"></i> Блок реле</h3>
                <span class="relay-block-reported" data-relay-reported></span>
            </div>
            <div class="card-body">
                <div class="door-offline-notice" data-relay-offline style="display:none;">
                    <i class="fas fa-plug"></i>
                    <span>Шкаф не в сети — управление реле недоступно</span>
                </div>
                <p class="relay-block-empty" data-relay-absent style="display:none;"></p>
                <div class="relay-block-list" data-relay-list></div>
                <p class="relay-block-empty" data-relay-empty style="display:none;">Плата ещё не сообщила состояние реле</p>
            </div>
        </div>

        <div class="new-modal-overlay" data-relay-confirm>
            <div class="new-modal">
                <div class="new-modal-header">
                    <h3><i class="fas fa-plug"></i> <span data-relay-confirm-title></span></h3>
                    <button type="button" class="new-modal-close" data-relay-confirm-close><i class="fas fa-times"></i></button>
                </div>
                <div class="new-modal-content">
                    <p data-relay-confirm-question></p>
                    <div class="door-modal-summary">
                        <p><strong>Шкаф:</strong> <span data-relay-confirm-locker>—</span></p>
                        <p data-relay-confirm-warning></p>
                    </div>
                </div>
                <div class="new-modal-actions">
                    <button type="button" class="btn-secondary" data-relay-confirm-cancel>Отмена</button>
                    <button type="button" class="btn-primary" data-relay-confirm-ok></button>
                </div>
            </div>
        </div>

        <div class="new-modal-overlay" data-relay-2fa>
            <div class="new-modal">
                <div class="new-modal-header">
                    <h3><i class="fas fa-shield-alt"></i> Подтверждение кода</h3>
                    <button type="button" class="new-modal-close" data-relay-2fa-close><i class="fas fa-times"></i></button>
                </div>
                <div class="new-modal-content">
                    <p class="relay-2fa-action" data-relay-2fa-action></p>
                    <p class="door-stage-text">На ваш аккаунт отправлен код подтверждения. Введите 6 цифр, чтобы продолжить.</p>
                    <div class="door-2fa-inputs" data-relay-2fa-inputs>
                        ${Array.from({ length: OTP_LENGTH }, () =>
                            '<input type="text" inputmode="numeric" maxlength="1" class="door-2fa-input">').join('')}
                    </div>
                    <div class="door-2fa-error" data-relay-2fa-error style="display:none;"></div>
                </div>
                <div class="new-modal-actions">
                    <button type="button" class="btn-secondary" data-relay-2fa-cancel>Отмена</button>
                    <button type="button" class="btn-primary" data-relay-2fa-ok><i class="fas fa-check"></i> Подтвердить</button>
                    <div class="door-verify-spinner relay-2fa-spinner" data-relay-2fa-spinner style="display:none;">
                        <div class="spinner"></div>
                        <span>Проверка кода...</span>
                    </div>
                </div>
            </div>
        </div>
    `;
    bindModals();
}

function q(selector) {
    return state.container.querySelector(selector);
}

function buildRow(channel) {
    const row = document.createElement('div');
    row.className = 'relay-row';
    row.dataset.channel = String(channel);
    row.innerHTML = `
        <div class="relay-row-head">
            <span class="relay-row-name">Реле ${channel}</span>
            <span class="relay-state" data-relay-state>
                <span class="relay-state-dot"></span>
                <span class="relay-state-label"></span>
            </span>
        </div>
        <div class="relay-row-changed" data-relay-changed></div>
        <div class="relay-row-actions" data-relay-actions></div>
        <div class="door-verify-spinner relay-row-progress" data-relay-progress style="display:none;">
            <div class="spinner"></div>
            <span data-relay-progress-text>Выполняется…</span>
        </div>
    `;
    row.querySelector('[data-relay-actions]').addEventListener('click', e => {
        const btn = e.target.closest('button[data-action]');
        if (btn && !btn.disabled) requestAction(channel, btn.dataset.action);
    });
    return row;
}

function channelBusy(item) {
    const local = state.inFlight.get(item.channel);
    if (local) return local.type;
    if (state.pending2fa?.channel === item.channel) return state.pending2fa.type;
    if (item.pending_command && ACTIVE_STATUSES.has(item.pending_command.status)) {
        return item.pending_command.type || true;
    }
    return null;
}

function updateRow(row, item) {
    const reported = REPORTED_STATE[item.reported_state] || REPORTED_UNKNOWN;
    const stateEl = row.querySelector('[data-relay-state]');
    stateEl.className = `relay-state relay-state--${reported.mod}`;
    stateEl.querySelector('.relay-state-label').textContent = reported.label;
    row.classList.toggle('relay-row--error', reported.mod === 'error');

    row.querySelector('[data-relay-changed]').textContent =
        item.changed_at ? `Изменено: ${formatTime(item.changed_at)}` : '';

    const busy = channelBusy(item);
    const online = isOnline();
    const allowed = ACTION_ORDER.filter(a => (item.allowed_actions || []).includes(a));
    const actionsEl = row.querySelector('[data-relay-actions]');
    const signature = allowed.join(',');
    if (actionsEl.dataset.signature !== signature) {
        actionsEl.dataset.signature = signature;
        actionsEl.innerHTML = allowed.map(a => {
            const ui = ACTION_UI[a];
            return `<button type="button" class="relay-btn ${ui.btnClass}" data-action="${a}">` +
                `<i class="fas ${ui.icon}"></i> ${ui.button}</button>`;
        }).join('');
    }
    actionsEl.querySelectorAll('button').forEach(btn => { btn.disabled = !!busy || !online; });
    actionsEl.style.display = busy ? 'none' : '';

    const progress = row.querySelector('[data-relay-progress]');
    progress.style.display = busy ? 'flex' : 'none';
    progress.querySelector('[data-relay-progress-text]').textContent =
        (typeof busy === 'string' && ACTION_UI[busy]?.progress) || 'Выполняется…';
}

function render() {
    const data = state.data;
    const card = q('[data-relay-card]');
    if (!data) {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';

    const absentEl = q('[data-relay-absent]');
    const listEl = q('[data-relay-list]');
    if (data.has_relays !== true) {
        absentEl.textContent = data.has_relays === false ? NO_RELAY_MODULE_TEXT : NO_RELAY_DATA_TEXT;
        absentEl.style.display = '';
        listEl.style.display = 'none';
        q('[data-relay-empty]').style.display = 'none';
        q('[data-relay-offline]').style.display = 'none';
        q('[data-relay-reported]').textContent = '';
        return;
    }
    absentEl.style.display = 'none';
    listEl.style.display = '';

    const online = isOnline();
    q('[data-relay-offline]').style.display = online ? 'none' : 'flex';
    card.classList.toggle('relay-block-card--offline', !online);

    const reportedEl = q('[data-relay-reported]');
    const reportedMs = data.reported_at ? new Date(data.reported_at).getTime() : NaN;
    const stale = online && !isNaN(reportedMs) && Date.now() - reportedMs > STALE_REPORT_MS;
    reportedEl.textContent = data.reported_at
        ? `Данные платы: ${formatTime(data.reported_at)}${stale ? ' (устарели)' : ''}`
        : 'Данные платы: —';
    reportedEl.classList.toggle('relay-block-reported--stale', stale || !online);

    const items = (data.items || [])
        .filter(item => item.is_active !== false)
        .sort((a, b) => a.channel - b.channel);
    q('[data-relay-empty]').style.display = items.length ? 'none' : '';

    const list = q('[data-relay-list]');
    const seen = new Set();
    items.forEach((item, index) => {
        seen.add(item.channel);
        let row = state.rows.get(item.channel);
        if (!row) {
            row = buildRow(item.channel);
            state.rows.set(item.channel, row);
        }
        updateRow(row, item);
        if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    });
    for (const [channel, row] of state.rows) {
        if (!seen.has(channel)) {
            row.remove();
            state.rows.delete(channel);
        }
    }
}

// ── Состояние реле ────────────────────────────────────────────

async function refresh() {
    if (!state || state.loading) return;
    const gen = generation;
    state.loading = true;
    const controller = new AbortController();
    state.abort = controller;
    const timeout = setTimeout(() => controller.abort(), RELAYS_FETCH_TIMEOUT_MS);
    try {
        const { response, body } = await request('/relays', { cache: 'no-store', signal: controller.signal });
        if (!alive(gen)) return;
        if (!response.ok) {
            if (response.status === HTTP_UNAUTHORIZED || response.status === 404) {
                state.data = null;
                render();
            }
            return;
        }
        state.data = body;
        render();
    } catch (err) {
        if (err.name !== 'AbortError') console.error('Ошибка загрузки состояния реле:', err);
    } finally {
        clearTimeout(timeout);
        if (gen === generation && state) {
            state.loading = false;
            state.abort = null;
        }
    }
}

function onVisibilityChange() {
    if (!document.hidden && state) refresh();
}

function startRefreshTimer() {
    const gen = generation;
    state.refreshTimer = setInterval(() => {
        if (!alive(gen)) return;
        if (!document.hidden) refresh();
    }, RELAYS_REFRESH_MS);
    document.addEventListener('visibilitychange', onVisibilityChange);
}

// ── Команды ───────────────────────────────────────────────────

function findItem(channel) {
    return (state?.data?.items || []).find(i => i.channel === channel) || null;
}

function requestAction(channel, type) {
    if (!state || !ACTION_UI[type]) return;
    if (!isOnline()) {
        notify('Шкаф не в сети — управление реле недоступно', 'warning');
        return;
    }
    if (ACTION_UI[type].confirm) {
        openConfirm(channel, type);
    } else {
        sendCommand(channel, type);
    }
}

function rerenderChannel(channel) {
    const row = state?.rows.get(channel);
    const item = findItem(channel);
    if (row && item) updateRow(row, item);
}

async function sendCommand(channel, type) {
    const gen = generation;
    state.inFlight.set(channel, { type, commandId: null, deadline: null });
    rerenderChannel(channel);
    try {
        const { response, body } = await request('/commands', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type, parameters: { relay: channel } })
        });
        if (!alive(gen)) return;

        if (!response.ok) {
            finishChannel(channel);
            notify(errorText(response.status, body, 'Не удалось выполнить команду'), 'error');
            return;
        }

        if (response.status === HTTP_ACCEPTED) {
            state.inFlight.delete(channel);
            if (!body?.challenge_id) {
                rerenderChannel(channel);
                notify('Не удалось получить код подтверждения', 'error');
                return;
            }
            open2fa(channel, type, body.challenge_id);
            return;
        }

        trackCommand(channel, type, body);
    } catch (err) {
        console.error('Ошибка отправки команды реле:', err);
        if (!alive(gen)) return;
        finishChannel(channel);
        notify('Не удалось подключиться к серверу', 'error');
    }
}

function commandDeadline(command) {
    const expires = command?.expires_at ? new Date(command.expires_at).getTime() : NaN;
    const created = command?.created_at ? new Date(command.created_at).getTime() : NaN;
    const ttl = !isNaN(expires) && !isNaN(created) ? Math.max(0, expires - created) : COMMAND_TTL_MS;
    return Date.now() + ttl + COMMAND_DEADLINE_GRACE_MS;
}

function trackCommand(channel, type, command) {
    if (!command?.id) {
        finishChannel(channel);
        notify('Сервер не вернул команду', 'error');
        return;
    }
    const entry = { type, commandId: command.id, deadline: commandDeadline(command), timer: null };
    state.inFlight.set(channel, entry);
    rerenderChannel(channel);
    if (!handleCommandStatus(channel, command)) {
        const gen = generation;
        entry.timer = setInterval(() => pollCommand(gen, channel), COMMAND_POLL_MS);
    }
}

function finishChannel(channel) {
    const entry = state?.inFlight.get(channel);
    if (entry?.timer) clearInterval(entry.timer);
    state?.inFlight.delete(channel);
    rerenderChannel(channel);
}

function handleCommandStatus(channel, command) {
    const entry = state.inFlight.get(channel);
    const title = actionTitle(entry?.type, channel);
    switch (command?.status) {
        case COMMAND_STATUS.EXECUTED:
            finishChannel(channel);
            notify(`${title}: команда выполнена`, 'success');
            refresh();
            state.onCommandDone?.(command);
            return true;
        case COMMAND_STATUS.FAILED:
            finishChannel(channel);
            notify(`${title}: ${DEVICE_REASON_TEXT[command.error_message] || command.error_message || 'команда не выполнена'}`, 'error');
            refresh();
            state.onCommandDone?.(command);
            return true;
        default:
            if (ACTIVE_STATUSES.has(command?.status)) return false;
            finishChannel(channel);
            notify(`${title}: неизвестный статус команды`, 'warning');
            return true;
    }
}

async function pollCommand(gen, channel) {
    if (!alive(gen)) return;
    const entry = state.inFlight.get(channel);
    if (!entry || entry.polling) return;
    if (Date.now() > entry.deadline) {
        finishChannel(channel);
        notify(`${actionTitle(entry.type, channel)}: плата не ответила — статус команды не подтверждён`, 'warning');
        refresh();
        return;
    }
    entry.polling = true;
    try {
        const { response, body } = await request(`/commands/${entry.commandId}`, { cache: 'no-store' });
        if (!alive(gen) || state.inFlight.get(channel) !== entry) return;
        if (!response.ok) {
            finishChannel(channel);
            notify(errorText(response.status, body, 'Не удалось получить статус команды'), 'error');
            return;
        }
        handleCommandStatus(channel, body);
    } catch (err) {
        console.error('Ошибка опроса команды реле:', err);
    } finally {
        entry.polling = false;
    }
}

// ── Модалки ───────────────────────────────────────────────────

function showOverlay(el, visible) {
    el.style.display = visible ? 'flex' : 'none';
}

function openConfirm(channel, type) {
    const ui = ACTION_UI[type];
    state.confirm = { channel, type };
    q('[data-relay-confirm-title]').textContent = actionTitle(type, channel);
    q('[data-relay-confirm-question]').textContent = `${actionTitle(type, channel)}?`;
    q('[data-relay-confirm-locker]').textContent = lockerLabel() || '—';
    q('[data-relay-confirm-warning]').textContent = ui.warning || '';
    q('[data-relay-confirm-ok]').innerHTML = `<i class="fas ${ui.icon}"></i> ${escapeHtml(ui.verb)}`;
    showOverlay(q('[data-relay-confirm]'), true);
}

function closeConfirm() {
    state.confirm = null;
    showOverlay(q('[data-relay-confirm]'), false);
}

function otpInputs() {
    return Array.from(state.container.querySelectorAll('[data-relay-2fa-inputs] input'));
}

function set2faError(message) {
    const el = q('[data-relay-2fa-error]');
    el.textContent = message || '';
    el.style.display = message ? 'block' : 'none';
}

function set2faPending(pending) {
    q('[data-relay-2fa-ok]').style.display = pending ? 'none' : '';
    q('[data-relay-2fa-cancel]').disabled = pending;
    q('[data-relay-2fa-spinner]').style.display = pending ? 'flex' : 'none';
}

function open2fa(channel, type, challengeId) {
    state.pending2fa = { channel, type, challengeId };
    rerenderChannel(channel);
    const label = lockerLabel();
    q('[data-relay-2fa-action]').textContent =
        `${actionTitle(type, channel)}${label ? ` на шкафу ${label}` : ''}`;
    otpInputs().forEach(input => { input.value = ''; });
    set2faError('');
    set2faPending(false);
    showOverlay(q('[data-relay-2fa]'), true);
    otpInputs()[0]?.focus();
}

function close2fa() {
    const channel = state.pending2fa?.channel;
    state.pending2fa = null;
    showOverlay(q('[data-relay-2fa]'), false);
    if (channel != null) rerenderChannel(channel);
}

async function confirm2fa() {
    const pending = state.pending2fa;
    if (!pending || pending.sending) return;
    const inputs = otpInputs();
    const code = inputs.map(i => i.value).join('');
    if (!new RegExp(`^\\d{${OTP_LENGTH}}$`).test(code)) {
        set2faError(`Введите ${OTP_LENGTH} цифр кода`);
        inputs[0]?.focus();
        return;
    }

    const gen = generation;
    pending.sending = true;
    set2faError('');
    set2faPending(true);
    try {
        const { response, body } = await request('/commands/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ challenge_id: pending.challengeId, code })
        });
        if (!alive(gen) || state.pending2fa !== pending) return;

        if (!response.ok) {
            const message = errorText(response.status, body, 'Не удалось подтвердить код');
            if (RELAY_CONFLICT_CODES.has(problemCode(body))) {
                close2fa();
                notify(message, 'error');
                return;
            }
            pending.sending = false;
            set2faPending(false);
            set2faError(message);
            inputs.forEach(i => { i.value = ''; });
            inputs[0]?.focus();
            return;
        }

        close2fa();
        trackCommand(pending.channel, pending.type, body);
    } catch (err) {
        console.error('Ошибка подтверждения кода реле:', err);
        if (!alive(gen) || state.pending2fa !== pending) return;
        pending.sending = false;
        set2faPending(false);
        set2faError('Не удалось подключиться к серверу');
    }
}

function bindModals() {
    const confirmEl = q('[data-relay-confirm]');
    q('[data-relay-confirm-close]').addEventListener('click', closeConfirm);
    q('[data-relay-confirm-cancel]').addEventListener('click', closeConfirm);
    q('[data-relay-confirm-ok]').addEventListener('click', () => {
        const pending = state.confirm;
        closeConfirm();
        if (pending) sendCommand(pending.channel, pending.type);
    });
    confirmEl.addEventListener('click', e => { if (e.target === confirmEl) closeConfirm(); });

    const tfaEl = q('[data-relay-2fa]');
    q('[data-relay-2fa-close]').addEventListener('click', () => { if (!state.pending2fa?.sending) close2fa(); });
    q('[data-relay-2fa-cancel]').addEventListener('click', close2fa);
    q('[data-relay-2fa-ok]').addEventListener('click', confirm2fa);

    const inputs = otpInputs();
    inputs.forEach((input, i) => {
        input.addEventListener('input', () => {
            input.value = input.value.replace(/\D/g, '').slice(0, 1);
            if (input.value && i < inputs.length - 1) inputs[i + 1].focus();
        });
        input.addEventListener('keydown', e => {
            if (e.key === 'Backspace' && !input.value && i > 0) inputs[i - 1].focus();
            if (e.key === 'Enter') confirm2fa();
        });
        input.addEventListener('paste', e => {
            e.preventDefault();
            const digits = (e.clipboardData || window.clipboardData)
                .getData('text').replace(/\D/g, '').slice(0, inputs.length);
            for (let k = 0; k < digits.length; k++) inputs[k].value = digits[k];
            inputs[Math.min(digits.length, inputs.length - 1)].focus();
        });
    });
    tfaEl.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !state.pending2fa?.sending) close2fa();
    });
}

// ── Публичный API ─────────────────────────────────────────────

function mount(containerEl, options = {}) {
    unmount();
    if (!containerEl || !options.lockerId) return;
    ensureStyle();
    generation += 1;
    state = {
        container: containerEl,
        lockerId: options.lockerId,
        apiBase: options.apiBase || window.API_BASE || '/api/v1',
        notify: options.notify,
        getLockerLabel: options.getLockerLabel,
        onCommandDone: options.onCommandDone,
        data: null,
        rows: new Map(),
        inFlight: new Map(),
        pending2fa: null,
        confirm: null,
        loading: false,
        abort: null,
        refreshTimer: null
    };
    renderSkeleton();
    refresh();
    startRefreshTimer();
}

function unmount() {
    if (!state) return;
    const current = state;
    generation += 1;
    state = null;
    clearInterval(current.refreshTimer);
    current.abort?.abort();
    for (const entry of current.inFlight.values()) {
        if (entry.timer) clearInterval(entry.timer);
    }
    document.removeEventListener('visibilitychange', onVisibilityChange);
    current.container.innerHTML = '';
}

export const RelayBlock = Object.freeze({ mount, unmount, refresh: () => refresh() });
