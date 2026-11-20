// lockers-detail.js — Детальная страница шкафа


let lockerUuid = null;
let doorLocker = null;
let doorTimerInterval = null;
let doorListenersReady = false;
let doorCommandPollTimer = null;
let doorCommandPollAttempts = 0;
const DOOR_COMMAND_POLL_MAX_ATTEMPTS = 30;
let pending2fa = null; // { type, challengeId }
let sensorsRefreshTimer = null;
let sensorsLoading = false;
const SENSORS_REFRESH_MS = 5000;
const SENSORS_FETCH_TIMEOUT_MS = 4000;

const RELAY_BLOCK_SRC = '/libs/scud/relay-block.js?v=2';
let relayBlock = null;

async function mountRelayBlock(uuid) {
    try {
        relayBlock = relayBlock || (await import(RELAY_BLOCK_SRC)).RelayBlock;
    } catch (err) {
        console.error('Не удалось загрузить блок реле:', err);
        return;
    }
    const mountEl = document.getElementById('relayBlockMount');
    if (lockerUuid !== uuid || !mountEl) return;
    relayBlock.mount(mountEl, {
        lockerId: uuid,
        apiBase: API_BASE,
        notify: showNotification,
        getLockerLabel: () => doorLocker?.inventory_number
    });
}

async function loadLockerDetail(uuid) {
    try {
        const response = await fetch(`${API_BASE}/locker/${uuid}`);

        if (!response.ok) {
            if (response.status === 404) {
                showError('Шкаф не найден', `Шкаф с UUID ${uuid} не существует.`);
            } else {
                showError('Ошибка сервера', `Сервер вернул статус ${response.status}.`);
            }
            return;
        }

        const locker = await response.json();
        renderLocker(locker);
    } catch (err) {
        console.error('Ошибка загрузки данных шкафа:', err);
        showError('Ошибка соединения', 'Не удалось подключиться к серверу. Проверьте соединение.');
    }
}

function renderLocker(locker) {
    document.getElementById('headerInventoryNumber').textContent = locker.inventory_number;
    document.getElementById('headerAddress').textContent = locker.location_address;

    const stateEl = document.getElementById('headerState');
    stateEl.className = `connection-status ${locker.connectivity === 'online' ? 'online' : 'offline'}`;
    stateEl.innerHTML = `<i class="fas fa-circle"></i> ${locker.connectivity === 'online' ? 'В сети' : 'Не в сети'}`;

    document.getElementById('infoInventoryNumber').textContent = locker.inventory_number;
    document.getElementById('infoId').textContent = locker.id;
    document.getElementById('infoAddress').textContent = locker.location_address;
    document.getElementById('infoLastSeen').textContent = formatTimestamp(locker.last_opened_at);
    document.getElementById('infoCreatedAt').textContent = formatTimestamp(locker.created_at);
    document.getElementById('infoUpdatedAt').textContent = formatTimestamp(locker.updated_at);

    const infoStateEl = document.getElementById('infoState');
    infoStateEl.className = `connection-status ${locker.connectivity === 'online' ? 'online' : 'offline'}`;
    infoStateEl.innerHTML = `<i class="fas fa-circle"></i> ${locker.connectivity === 'online' ? 'В сети' : 'Не в сети'}`;

    if (locker.location_lat != null && locker.location_lon != null) {
        document.getElementById('infoCoordinates').textContent = `${locker.location_lat}, ${locker.location_lon}`;
    }

    document.getElementById('loadingState').style.display = 'none';
    document.getElementById('lockerDetailContent').style.display = 'block';

    loadSensors(locker.id);
    startSensorsAutoRefresh(locker.id);

    if (locker.who_open_last) {
        loadLastOpener(locker.who_open_last);
    }

    loadEmergencyRequest(lockerUuid);

    initDoorControl(locker);
}

async function loadEmergencyRequest(uuid) {
    try {
        const params = new URLSearchParams({ status: 'pending', locker_id: uuid });
        const response = await fetch(`${API_BASE}/emergency-requests?${params}`, { cache: 'no-store' });
        if (!response.ok) return;

        const data = await response.json();
        const request = data.items?.[0];
        if (!request) return;

        document.getElementById('emergencyRequestName').textContent = request.user_full_name || '—';
        document.getElementById('emergencyRequestPhone').textContent = request.user_phone || '—';
        document.getElementById('emergencyRequestCreatedAt').textContent = formatTimestamp(request.created_at);
        document.getElementById('emergencyRequestCard').style.display = '';

        const countEl = document.getElementById('emergencyRequestCount');
        if (data.total > 1) {
            countEl.textContent = data.total;
            countEl.style.display = '';
        } else {
            countEl.style.display = 'none';
        }
    } catch (err) {
        console.error('Ошибка загрузки запроса помощи:', err);
    }
}

async function resolveEmergencyRequest() {
    const btn = document.getElementById('emergencyRequestResolveBtn');
    const spinner = document.getElementById('emergencyRequestResolveSpinner');
    btn.style.display = 'none';
    spinner.style.display = 'flex';

    try {
        const response = await fetch(`${API_BASE}/emergency-requests`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ locker_id: lockerUuid, status: 'closed' })
        });

        if (!response.ok) {
            const body = await response.json().catch(() => null);
            showNotification(body?.detail || `Не удалось отметить помощь (HTTP ${response.status})`, 'error');
            btn.style.display = '';
            spinner.style.display = 'none';
            return;
        }

        document.getElementById('emergencyRequestCard').style.display = 'none';
        showNotification('Помощь отмечена как оказанная', 'success');
    } catch (err) {
        console.error('Ошибка отправки статуса помощи:', err);
        showNotification('Не удалось подключиться к серверу', 'error');
        btn.style.display = '';
        spinner.style.display = 'none';
    }
}

// ── Управление дверью ──────────────────────────────────────────

function initDoorControl(locker) {
    doorLocker = locker;

    pending2fa = null;
    hideDoor2faStage();

    const offline = isDoorOffline();
    document.getElementById('doorOfflineNotice').style.display = offline ? 'flex' : 'none';
    document.getElementById('doorRequestCodeBtn').disabled = offline;
    document.getElementById('doorCloseBtn').disabled = offline;

    document.getElementById('doorModalInventory').textContent = locker.inventory_number;
    document.getElementById('doorModalAddress').textContent = locker.location_address || '—';

    if (locker.door === 'open' || locker.door === 'help') {
        setDoorStage(2);
    } else {
        setDoorStage(1);
    }

    document.getElementById('doorControlCard').style.display = '';

    if (!doorListenersReady) {
        setupDoorListeners();
        doorListenersReady = true;
    }
}

function isDoorOffline() {
    return doorLocker?.connectivity !== 'online';
}

function setDoorStage(n) {
    if (n !== 2) stopDoorTimer();

    for (let i = 1; i <= 3; i++) {
        document.getElementById(`doorStage${i}`).classList.remove('active');
    }
    document.getElementById(`doorStage${n}`).classList.add('active');

    if (n === 1) {
        document.getElementById('doorStage1Start').style.display = 'block';
    } else if (n === 2 && doorLocker) {
        if (doorLocker.last_opened_at) {
            startDoorTimer(doorLocker.last_opened_at);
        } else {
            document.getElementById('doorWorkTimer').textContent = '00:00:00';
        }
    }
}

function setupDoorListeners() {
    document.getElementById('doorRequestCodeBtn').addEventListener('click', () => sendDoorCommand('open'));
    document.getElementById('doorResetBtn').addEventListener('click', () => setDoorStage(1));

    const closeModal = document.getElementById('doorCloseModal');
    const closeCloseModal = () => { closeModal.style.display = 'none'; };
    document.getElementById('doorCloseBtn').addEventListener('click', () => { closeModal.style.display = 'flex'; });
    document.getElementById('doorCloseModalClose').addEventListener('click', closeCloseModal);
    document.getElementById('doorCloseModalCancel').addEventListener('click', closeCloseModal);
    document.getElementById('doorCloseModalConfirm').addEventListener('click', () => {
        closeCloseModal();
        sendDoorCommand('close');
    });
    closeModal.addEventListener('click', e => { if (e.target === closeModal) closeCloseModal(); });

    for (let i = 1; i <= 6; i++) {
        const input = document.getElementById(`door2faCode${i}`);
        input.addEventListener('input', function () {
            this.value = this.value.replace(/\D/g, '').slice(0, 1);
            if (this.value.length === 1 && i < 6) {
                document.getElementById(`door2faCode${i + 1}`).focus();
            }
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Backspace' && this.value.length === 0 && i > 1) {
                document.getElementById(`door2faCode${i - 1}`).focus();
            }
            if (e.key === 'Enter') confirmDoor2fa();
        });
        input.addEventListener('paste', door2faHandlePaste);
    }
    document.getElementById('door2faConfirmBtn').addEventListener('click', confirmDoor2fa);
    document.getElementById('door2faCancelBtn').addEventListener('click', cancelDoor2fa);
}

function door2faHandlePaste(e) {
    e.preventDefault();
    const digits = (e.clipboardData || window.clipboardData)
        .getData('text').replace(/\D/g, '').slice(0, 6);
    for (let i = 0; i < digits.length; i++) {
        const input = document.getElementById(`door2faCode${i + 1}`);
        if (input) input.value = digits[i];
    }
    const next = document.getElementById(`door2faCode${Math.min(digits.length, 5) + 1}`);
    if (next) next.focus();
}

function door2faClearCodeInputs() {
    for (let i = 1; i <= 6; i++) {
        const input = document.getElementById(`door2faCode${i}`);
        if (input) input.value = '';
    }
}

function door2faReadCode() {
    let code = '';
    for (let i = 1; i <= 6; i++) {
        code += document.getElementById(`door2faCode${i}`).value;
    }
    return code;
}

const DOOR_COMMAND_UI = {
    open: { btnId: 'doorRequestCodeBtn', spinnerId: 'doorVerifySpinner' },
    close: { btnId: 'doorCloseBtn', spinnerId: 'doorCloseSpinner' }
};

function setDoorCommandPending(type, pending) {
    const { btnId, spinnerId } = DOOR_COMMAND_UI[type];
    const btn = document.getElementById(btnId);
    const spinner = document.getElementById(spinnerId);
    btn.style.display = pending ? 'none' : '';
    btn.disabled = pending || isDoorOffline();
    spinner.style.display = pending ? 'flex' : 'none';
}

function stopDoorCommandPoll() {
    if (doorCommandPollTimer) {
        clearInterval(doorCommandPollTimer);
        doorCommandPollTimer = null;
    }
    doorCommandPollAttempts = 0;
}

async function sendDoorCommand(type) {
    if (isDoorOffline()) {
        showNotification('Шкаф не в сети — управление дверью недоступно', 'warning');
        return;
    }
    setDoorCommandPending(type, true);

    try {
        const response = await fetch(`${API_BASE}/locker/${lockerUuid}/commands`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type })
        });

        const body = await response.json().catch(() => null);

        if (!response.ok) {
            showNotification(body?.detail || `Не удалось выполнить команду (HTTP ${response.status})`, 'error');
            setDoorCommandPending(type, false);
            return;
        }

        if (response.status === 202) {
            const challengeId = body?.challenge_id;
            if (!challengeId) {
                showNotification('Не удалось получить код подтверждения', 'error');
                setDoorCommandPending(type, false);
                return;
            }
            setDoorCommandPending(type, false);
            pending2fa = { type, challengeId };
            showDoor2faStage();
            return;
        }

        // 201 — команда создана, дальше следим за её статусом
        handleDoorCommandStatus(body, type);
    } catch (err) {
        console.error('Ошибка выполнения команды двери:', err);
        showNotification('Не удалось подключиться к серверу', 'error');
        setDoorCommandPending(type, false);
    }
}

function handleDoorCommandStatus(command, type) {
    const actionText = type === 'open' ? 'Дверь открыта' : 'Дверь закрыта';

    switch (command?.status) {
        case 'executed':
            stopDoorCommandPoll();
            setDoorCommandPending(type, false);
            showNotification(actionText, 'success');
            loadLockerDetail(lockerUuid);
            break;
        case 'failed':
            stopDoorCommandPoll();
            setDoorCommandPending(type, false);
            showNotification('Не удалось выполнить команду', 'error');
            loadLockerDetail(lockerUuid);
            break;
        case 'pending':
        case 'sent':
        case 'delivered':
            setDoorCommandPending(type, true);
            if (!doorCommandPollTimer) {
                doorCommandPollAttempts = 0;
                doorCommandPollTimer = setInterval(() => pollDoorCommand(command.id, type), 2000);
            }
            break;
        default:
            stopDoorCommandPoll();
            setDoorCommandPending(type, false);
            showNotification('Неизвестный статус команды', 'warning');
    }
}

async function pollDoorCommand(commandId, type) {
    // Лимит попыток: если шкаф не отвечает, команда может навсегда остаться
    // в pending/sent — прекращаем опрос (сетевые ошибки тоже считаются)
    if (++doorCommandPollAttempts > DOOR_COMMAND_POLL_MAX_ATTEMPTS) {
        stopDoorCommandPoll();
        setDoorCommandPending(type, false);
        showNotification('Шкаф не ответил — статус команды не подтверждён', 'warning');
        return;
    }

    try {
        const response = await fetch(
            `${API_BASE}/locker/${lockerUuid}/commands/${commandId}`,
            { cache: 'no-store' }
        );
        const body = await response.json().catch(() => null);

        if (!response.ok) {
            stopDoorCommandPoll();
            setDoorCommandPending(type, false);
            showNotification(body?.detail || `Не удалось получить статус команды (HTTP ${response.status})`, 'error');
            return;
        }

        handleDoorCommandStatus(body, type);
    } catch (err) {
        console.error('Ошибка опроса статуса команды:', err);
    }
}

function showDoor2faStage() {
    stopDoorTimer();
    for (let i = 1; i <= 3; i++) {
        document.getElementById(`doorStage${i}`).classList.remove('active');
    }
    door2faClearCodeInputs();
    document.getElementById('door2faError').style.display = 'none';
    setDoor2faPending(false);
    const stage = document.getElementById('door2faStage');
    stage.style.display = 'block';
    stage.classList.add('active');
    document.getElementById('door2faCode1').focus();
}

function hideDoor2faStage() {
    const stage = document.getElementById('door2faStage');
    stage.style.display = 'none';
    stage.classList.remove('active');
}

function setDoor2faPending(pending) {
    document.getElementById('door2faConfirmBtn').style.display = pending ? 'none' : '';
    document.getElementById('door2faCancelBtn').disabled = pending;
    document.getElementById('door2faSpinner').style.display = pending ? 'flex' : 'none';
}

function cancelDoor2fa() {
    const type = pending2fa?.type;
    pending2fa = null;
    hideDoor2faStage();
    if (type) setDoorCommandPending(type, false);
    setDoorStage(type === 'close' ? 2 : 1);
}

async function confirmDoor2fa() {
    if (!pending2fa) return;
    const { type, challengeId } = pending2fa;
    const code = door2faReadCode();
    const errorEl = document.getElementById('door2faError');
    errorEl.style.display = 'none';

    if (!/^\d{6}$/.test(code)) {
        errorEl.textContent = 'Введите 6 цифр кода';
        errorEl.style.display = 'block';
        document.getElementById('door2faCode1').focus();
        return;
    }

    setDoor2faPending(true);

    try {
        const response = await fetch(`${API_BASE}/locker/${lockerUuid}/commands/confirm`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ challenge_id: challengeId, code })
        });

        const body = await response.json().catch(() => null);

        if (!response.ok) {
            showNotification(body?.detail || `Не удалось подтвердить код (HTTP ${response.status})`, 'error');
            setDoor2faPending(false);
            door2faClearCodeInputs();
            document.getElementById('door2faCode1').focus();
            return;
        }

        pending2fa = null;
        hideDoor2faStage();
        setDoorStage(type === 'close' ? 2 : 1);
        handleDoorCommandStatus(body, type);
    } catch (err) {
        console.error('Ошибка подтверждения кода:', err);
        showNotification('Не удалось подключиться к серверу', 'error');
        setDoor2faPending(false);
    }
}

function toMilliseconds(ts) {
    if (!ts) return null;
    const d = new Date(ts);
    if (!isNaN(d.getTime())) return d.getTime();
    const num = Number(ts);
    if (!isNaN(num) && num > 0) return num < 1e11 ? num * 1000 : num;
    return null;
}

function startDoorTimer(lastOpen) {
    stopDoorTimer();
    const ms = toMilliseconds(lastOpen);
    if (!ms) return;
    const el = document.getElementById('doorWorkTimer');
    const tick = () => {
        if (!document.getElementById('doorWorkTimer')) {
            stopDoorTimer();
            return;
        }
        const sec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        el.textContent = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    };
    tick();
    doorTimerInterval = setInterval(tick, 1000);
}

function stopDoorTimer() {
    if (doorTimerInterval) {
        clearInterval(doorTimerInterval);
        doorTimerInterval = null;
    }
}

async function loadSensors(lockerId) {
    if (sensorsLoading) return;
    sensorsLoading = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), SENSORS_FETCH_TIMEOUT_MS);
    try {
        const response = await fetch(`${API_BASE}/locker/${lockerId}/sensors`,
            { cache: 'no-store', signal: controller.signal });
        if (!response.ok) return;
        const data = await response.json();
        const sensors = data.items ?? data;
        if (!sensors || sensors.length === 0) return;
        await renderSensors(lockerId, sensors);
    } catch (err) {
        if (err.name !== 'AbortError') console.error('Ошибка загрузки датчиков:', err);
    } finally {
        clearTimeout(timeout);
        sensorsLoading = false;
    }
}

function startSensorsAutoRefresh(lockerId) {
    stopSensorsAutoRefresh();
    sensorsRefreshTimer = setInterval(() => {
        if (!document.getElementById('sensorsSection')) {
            stopSensorsAutoRefresh();
            return;
        }
        if (!document.hidden) loadSensors(lockerId);
    }, SENSORS_REFRESH_MS);
}

function stopSensorsAutoRefresh() {
    if (sensorsRefreshTimer) {
        clearInterval(sensorsRefreshTimer);
        sensorsRefreshTimer = null;
    }
}

function onSensorsVisibilityChange() {
    if (!document.hidden && sensorsRefreshTimer && lockerUuid) loadSensors(lockerUuid);
}

async function renderSensors(lockerId, sensors) {
    const container = document.getElementById('sensorsList');
    if (!container) return;

    await Promise.all(
        sensors
            .filter(isThresholdSensor)
            .map(async s => {
                try {
                    const res = await fetch(`${API_BASE}/locker/${lockerId}/sensors/${s.id}/thresholds?limit=1&offset=0`);
                    if (res.ok) {
                        const t = await res.json();
                        const items = t.items ?? (Array.isArray(t) ? t : [t]);
                        const threshold = items[0];
                        s.min_value = threshold?.min_value ?? null;
                        s.max_value = threshold?.max_value ?? null;
                    }
                } catch (_) {}
            })
    );

    if (!container.isConnected) return;
    container.replaceChildren(...sensors.map(sensor =>
        isThresholdSensor(sensor) ? buildThresholdSensor(sensor) : buildStateSensor(sensor)
    ));

    document.getElementById('sensorsSection').style.display = '';
}

function isThresholdSensor(sensor) {
    return sensor.value_type === 'numeric' && !sensor.display;
}

function buildThresholdSensor(sensor) {
    const min = sensor.min_value;
    const max = sensor.max_value;
    const val = sensor.current_value;
    const unit = sensor.unit ? ` ${sensor.unit}` : '';
    const hasThresholds = min != null && max != null;
    const outOfRange = hasThresholds && val != null && (val < min || val > max);
    const pct = hasThresholds && max !== min && val != null
        ? Math.min(100, Math.max(0, ((val - min) / (max - min)) * 100))
        : 0;

    const scaleHtml = hasThresholds ? `
        <div class="sensor-scale-wrap">
            <div class="sensor-scale">
                <div class="sensor-scale-fill" style="width:${pct}%"></div>
                <div class="sensor-scale-marker" style="left:${pct}%"></div>
            </div>
            <div class="sensor-scale-labels">
                <span>${min}${unit}</span>
                <span>${max}${unit}</span>
            </div>
        </div>` : '';

    const card = document.createElement('div');
    card.className = `sensor-card sensor-card--threshold${outOfRange ? ' sensor-card--alert' : ''}`;
    card.innerHTML = `
        <div class="sensor-header">
            <div class="sensor-name-row">
                <span class="sensor-name">${sensor.name}</span>
                <span class="sensor-active-dot ${sensor.is_active ? 'sensor-active-dot--on' : 'sensor-active-dot--off'}"></span>
            </div>
            <span class="sensor-serial">${sensor.serial_number}</span>
            <span class="sensor-updated">Обновлено: ${formatTimestamp(sensor.updated_at)}</span>
        </div>
        <div class="sensor-value">${val != null ? val + unit : '—'}</div>
        ${scaleHtml}
    `;
    return card;
}

function buildStateSensor(sensor) {
    const display = sensor.display;
    const active = display ? display.severity === 'alert' : sensor.current_value == 1;
    const warning = !!display && display.severity === 'warning';
    const isDoor = sensor.name === 'Состояние двери';
    const activeLabel  = isDoor ? 'Открыта'  : 'Сработал';
    const normalLabel  = isDoor ? 'Закрыта'  : 'Норма';
    const severityClass = active ? ' sensor-card--alert' : warning ? ' sensor-card--warning' : '';
    const dotClass = active ? 'sensor-state-dot--active' : warning ? 'sensor-state-dot--warning' : '';

    const card = document.createElement('div');
    card.className = `sensor-card sensor-card--state${severityClass}`;
    card.innerHTML = `
        <div class="sensor-header">
            <div class="sensor-name-row">
                <span class="sensor-name">${sensor.name}</span>
                <span class="sensor-active-dot ${sensor.is_active ? 'sensor-active-dot--on' : 'sensor-active-dot--off'}"></span>
            </div>
            <span class="sensor-serial">${sensor.serial_number}</span>
            <span class="sensor-updated">Обновлено: ${formatTimestamp(sensor.updated_at)}</span>
        </div>
        <div class="sensor-state-body">
            <div class="sensor-state-dot ${dotClass}"></div>
            <span class="sensor-state-label"></span>
        </div>
    `;
    card.querySelector('.sensor-state-label').textContent = display
        ? display.label
        : (active ? activeLabel : normalLabel);
    return card;
}

async function loadLastOpener(userId) {
    try {
        const response = await fetch(`${API_BASE}/user/${userId}`);
        if (!response.ok) return;
        const user = await response.json();

        document.getElementById('lastOpenerName').textContent = user.full_name || '—';
        document.getElementById('lastOpenerOrg').textContent = user.contractor_name || '—';
        document.getElementById('lastOpenerPhone').textContent = user.phone || '—';
        document.getElementById('lastOpenerSection').style.display = '';
    } catch (err) {
        console.error('Ошибка загрузки пользователя:', err);
    }
}

function showNotification(message, type = 'success') {
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.innerHTML = `
        <div class="notification-content">
            <i class="fas fa-${type === 'error' ? 'exclamation-circle' : type === 'warning' ? 'exclamation-triangle' : 'check-circle'}"></i>
            <span>${message}</span>
        </div>
    `;
    document.body.appendChild(notification);
    notification.style.display = 'flex';
    setTimeout(() => notification.remove(), 3000);
}

function showError(title, message) {
    document.getElementById('loadingState').style.display = 'none';
    document.getElementById('errorState').style.display = 'flex';
    document.getElementById('errorTitle').textContent = title;
    document.getElementById('errorMessage').textContent = message;
}

function formatTimestamp(ts) {
    if (!ts) return '—';
    const date = new Date(ts);
    if (isNaN(date.getTime())) return '—';
    return date.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });
}

export function initLockersDetailModule() {
    // Останавливаем таймеры от предыдущего визита
    stopDoorTimer();
    stopDoorCommandPoll();
    stopSensorsAutoRefresh();
    relayBlock?.unmount();
    document.removeEventListener('visibilitychange', onSensorsVisibilityChange);
    document.addEventListener('visibilitychange', onSensorsVisibilityChange);

    // Сброс состояния — DOM будет новый, флаг слушателей тоже сбрасываем
    lockerUuid = null;
    doorLocker = null;
    doorListenersReady = false;
    pending2fa = null;

    // UUID передаётся через sessionStorage при навигации с open-lockers
    lockerUuid = sessionStorage.getItem('locker_detail_uuid');

    if (!lockerUuid) {
        showError('Не указан UUID', 'Шкаф не выбран. Перейдите из списка открытых шкафов.');
        return;
    }

    document.getElementById('emergencyRequestResolveBtn').addEventListener('click', resolveEmergencyRequest);

    loadLockerDetail(lockerUuid);
    mountRelayBlock(lockerUuid);
}
