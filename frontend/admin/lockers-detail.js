


let lockerUuid = null;
let doorLocker = null;
let doorTimerInterval = null;
let doorListenersReady = false;
let sensorsMap = {};
let sensorCards = {};       
let sensorThresholds = {};  
let currentSensorId = null;
let sensorsRefreshTimer = null;
let sensorsLoading = false;

const SENSORS_REFRESH_MS = 5000;
const SENSORS_FETCH_TIMEOUT_MS = 4000;
let doorCommandPollTimer = null;
let doorCommandPollAttempts = 0;
const DOOR_COMMAND_POLL_MAX_ATTEMPTS = 30;
let pending2fa = null;
let currentLocker = null;
let editingField = null;

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
        getLockerLabel: () => currentLocker?.inventory_number
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
    currentLocker = locker;
    cancelInlineEdit();

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

    document.getElementById('infoCoordinates').textContent =
        locker.location_lat != null && locker.location_lon != null
            ? `${locker.location_lat}, ${locker.location_lon}`
            : '—';

    document.getElementById('loadingState').style.display = 'none';
    document.getElementById('lockerDetailContent').style.display = 'block';

    loadSensors(locker.id, true);
    startSensorsAutoRefresh(locker.id);

    if (locker.who_open_last) {
        loadLastOpener(locker.who_open_last);
    }

    loadEmergencyRequest(lockerUuid);

    document.getElementById('statusResetCard').style.display =
        locker.operational_status && locker.operational_status !== 'normal' ? '' : 'none';

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

async function resetLockerStatus() {
    const btn = document.getElementById('statusResetModalConfirm');
    btn.disabled = true;

    try {
        const response = await fetch(`${API_BASE}/locker/${lockerUuid}/state`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operational_status: 'normal' })
        });

        if (!response.ok) {
            const body = await response.json().catch(() => null);
            showNotification(body?.detail || `Не удалось изменить статус (HTTP ${response.status})`, 'error');
            return;
        }

        document.getElementById('statusResetModal').style.display = 'none';
        showNotification('Статус шкафа изменён на «Норма»', 'success');
        loadLockerDetail(lockerUuid);
    } catch (err) {
        console.error('Ошибка изменения статуса шкафа:', err);
        showNotification('Не удалось подключиться к серверу', 'error');
    } finally {
        btn.disabled = false;
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



async function loadSensors(lockerId, refreshThresholds = false) {
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
        await renderSensors(lockerId, sensors, refreshThresholds);
    } catch (err) {
        if (err.name !== 'AbortError') console.error('Ошибка загрузки датчиков:', err);
    } finally {
        clearTimeout(timeout);
        sensorsLoading = false;
    }
}

function onSensorsVisibilityChange() {
    if (!document.hidden && sensorsRefreshTimer && lockerUuid) loadSensors(lockerUuid);
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



async function refreshSensorsManually() {
    const btn = document.getElementById('sensorsRefreshBtn');
    btn.disabled = true;
    btn.classList.add('loading');
    try {
        await loadSensors(lockerUuid, true);
        startSensorsAutoRefresh(lockerUuid);
    } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
    }
}



async function renderSensors(lockerId, sensors, refreshThresholds) {
    const container = document.getElementById('sensorsList');
    if (!container) return;

    await loadSensorThresholds(lockerId, sensors, refreshThresholds);

    
    if (!container.isConnected) return;

    sensorsMap = {};
    sensors.forEach((sensor, index) => {
        sensorsMap[sensor.id] = sensor;

        const type = isThresholdSensor(sensor) ? 'threshold' : 'state';
        let card = sensorCards[sensor.id];

        
        if (card && card.dataset.type !== type) {
            card.remove();
            card = null;
        }
        if (!card) {
            card = buildSensorCard(sensor, type);
            sensorCards[sensor.id] = card;
        }

        if (type === 'threshold') updateThresholdSensor(card, sensor);
        else updateStateSensor(card, sensor);

        
        if (container.children[index] !== card) {
            container.insertBefore(card, container.children[index] || null);
        }
    });

    
    Object.keys(sensorCards).forEach(id => {
        if (!(id in sensorsMap)) {
            sensorCards[id].remove();
            delete sensorCards[id];
        }
    });

    document.getElementById('sensorsSection').style.display = '';
}



async function loadSensorThresholds(lockerId, sensors, force) {
    const numeric = sensors.filter(isThresholdSensor);

    await Promise.all(
        numeric
            .filter(s => force || !(s.id in sensorThresholds))
            .map(async s => {
                try {
                    const res = await fetch(`${API_BASE}/locker/${lockerId}/sensors/${s.id}/thresholds?limit=1&offset=0`);
                    if (!res.ok) return;
                    const t = await res.json();
                    const items = t.items ?? (Array.isArray(t) ? t : [t]);
                    const threshold = items[0];
                    sensorThresholds[s.id] = {
                        min_value: threshold?.min_value ?? null,
                        max_value: threshold?.max_value ?? null
                    };
                } catch (_) {}
            })
    );

    numeric.forEach(s => {
        s.min_value = sensorThresholds[s.id]?.min_value ?? null;
        s.max_value = sensorThresholds[s.id]?.max_value ?? null;
    });
}

function isThresholdSensor(sensor) {
    return sensor.value_type === 'numeric' && !sensor.display;
}

function buildSensorCard(sensor, type) {
    const body = type === 'threshold'
        ? '<div class="sensor-value"></div>'
        : `<div class="sensor-state-body">
               <div class="sensor-state-dot"></div>
               <span class="sensor-state-label"></span>
           </div>`;

    const card = document.createElement('div');
    card.className = `sensor-card sensor-card--${type}`;
    card.dataset.type = type;
    card.innerHTML = `
        <div class="sensor-header">
            <div class="sensor-name-row">
                <span class="sensor-name"></span>
                <span class="sensor-active-dot"></span>
            </div>
            <button class="sensor-gear-btn" data-sensor-id="${sensor.id}" title="Настройки"><i class="fas fa-cog"></i></button>
            <span class="sensor-serial"></span>
            <span class="sensor-updated"></span>
        </div>
        ${body}
    `;
    card.querySelector('.sensor-gear-btn').addEventListener('click', () => openSensorSettings(sensor.id));
    return card;
}

function updateSensorHeader(card, sensor) {
    card.querySelector('.sensor-name').textContent = sensor.name;
    card.querySelector('.sensor-serial').textContent = sensor.serial_number;
    card.querySelector('.sensor-updated').textContent = `Обновлено: ${formatTimestamp(sensor.updated_at)}`;

    const dot = card.querySelector('.sensor-active-dot');
    dot.classList.toggle('sensor-active-dot--on', !!sensor.is_active);
    dot.classList.toggle('sensor-active-dot--off', !sensor.is_active);
}

function updateThresholdSensor(card, sensor) {
    const min = sensor.min_value;
    const max = sensor.max_value;
    const val = sensor.current_value;
    const unit = sensor.unit ? ` ${sensor.unit}` : '';
    const hasThresholds = min != null && max != null;
    const outOfRange = hasThresholds && val != null && (val < min || val > max);
    const pct = hasThresholds && max !== min && val != null
        ? Math.min(100, Math.max(0, ((val - min) / (max - min)) * 100))
        : 0;

    updateSensorHeader(card, sensor);
    card.classList.toggle('sensor-card--alert', outOfRange);
    card.querySelector('.sensor-value').textContent = val != null ? val + unit : '—';

    let scale = card.querySelector('.sensor-scale-wrap');
    if (!hasThresholds) {
        scale?.remove();
        return;
    }
    if (!scale) {
        card.insertAdjacentHTML('beforeend', `
            <div class="sensor-scale-wrap">
                <div class="sensor-scale">
                    <div class="sensor-scale-fill"></div>
                    <div class="sensor-scale-marker"></div>
                </div>
                <div class="sensor-scale-labels"><span></span><span></span></div>
            </div>`);
        scale = card.querySelector('.sensor-scale-wrap');
    }

    scale.querySelector('.sensor-scale-fill').style.width = `${pct}%`;
    scale.querySelector('.sensor-scale-marker').style.left = `${pct}%`;
    const labels = scale.querySelectorAll('.sensor-scale-labels span');
    labels[0].textContent = `${min}${unit}`;
    labels[1].textContent = `${max}${unit}`;
}

function updateStateSensor(card, sensor) {
    const display = sensor.display;
    const active = display ? display.severity === 'alert' : sensor.current_value == 1;
    const warning = !!display && display.severity === 'warning';
    const isDoor = sensor.name === 'Состояние двери';
    const fallbackLabel = active
        ? (isDoor ? 'Открыта' : 'Сработал')
        : (isDoor ? 'Закрыта' : 'Норма');

    updateSensorHeader(card, sensor);
    card.classList.toggle('sensor-card--alert', active);
    card.classList.toggle('sensor-card--warning', warning);
    const dot = card.querySelector('.sensor-state-dot');
    dot.classList.toggle('sensor-state-dot--active', active);
    dot.classList.toggle('sensor-state-dot--warning', warning);
    card.querySelector('.sensor-state-label').textContent = display ? display.label : fallbackLabel;
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

// ── Редактирование полей по месту ──────────────────────────────

const EDITABLE_FIELDS = {
    inventory_number: { valueId: 'infoInventoryNumber' },
    location_address: { valueId: 'infoAddress' },
    coordinates: { valueId: 'infoCoordinates', hintId: 'infoCoordinatesHint' }
};

function editRow(field) {
    return document.querySelector(`.info-value-row[data-field="${field}"]`);
}

function setupInlineEdit() {
    Object.keys(EDITABLE_FIELDS).forEach(field => {
        const row = editRow(field);
        if (!row) return;

        row.querySelector('[data-action="edit"]').addEventListener('click', () => startInlineEdit(field));
        row.querySelector('[data-action="save"]').addEventListener('click', () => saveInlineEdit(field));
        row.querySelector('[data-action="cancel"]').addEventListener('click', () => cancelInlineEdit());

        const valueEl = document.getElementById(EDITABLE_FIELDS[field].valueId);
        valueEl.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
                e.preventDefault();
                saveInlineEdit(field);
            } else if (e.key === 'Escape') {
                e.preventDefault();
                cancelInlineEdit();
            }
        });
    });
}

function setInlineEditMode(field, editing) {
    const row = editRow(field);
    const config = EDITABLE_FIELDS[field];
    const valueEl = document.getElementById(config.valueId);

    row.querySelector('[data-action="edit"]').style.display = editing ? 'none' : '';
    row.querySelector('[data-action="save"]').style.display = editing ? '' : 'none';
    row.querySelector('[data-action="cancel"]').style.display = editing ? '' : 'none';
    valueEl.classList.toggle('is-editing', editing);
    valueEl.contentEditable = editing ? 'true' : 'false';

    if (config.hintId) {
        document.getElementById(config.hintId).style.display = editing ? '' : 'none';
    }
}

function setInlineEditPending(field, pending) {
    const row = editRow(field);
    row.querySelectorAll('.info-edit-btn').forEach(btn => { btn.disabled = pending; });
    row.querySelector('[data-action="save"]').style.display = pending ? 'none' : '';
    row.querySelector('.info-edit-spinner').style.display = pending ? '' : 'none';
    document.getElementById(EDITABLE_FIELDS[field].valueId).contentEditable = pending ? 'false' : 'true';
}

function startInlineEdit(field) {
    if (editingField === field) return;
    if (editingField) cancelInlineEdit();

    const valueEl = document.getElementById(EDITABLE_FIELDS[field].valueId);
    const original = valueEl.textContent.trim();
    valueEl.dataset.original = original;
    if (original === '—') valueEl.textContent = '';

    editingField = field;
    setInlineEditMode(field, true);
    valueEl.focus();

    const range = document.createRange();
    range.selectNodeContents(valueEl);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
}

function cancelInlineEdit() {
    if (!editingField) return;

    const field = editingField;
    editingField = null;

    const valueEl = document.getElementById(EDITABLE_FIELDS[field].valueId);
    valueEl.textContent = valueEl.dataset.original ?? valueEl.textContent;
    setInlineEditPending(field, false);
    setInlineEditMode(field, false);
}



function buildInlineEditPayload(field, rawValue) {
    const value = rawValue.trim().replace(/\s+/g, ' ');

    if (field === 'coordinates') {
        const match = value.match(/^(-?\d+(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d+(?:[.,]\d+)?)$/);
        if (!match) {
            showNotification('Введите координаты в формате «широта, долгота»', 'error');
            return null;
        }

        const lat = parseFloat(match[1].replace(',', '.'));
        const lon = parseFloat(match[2].replace(',', '.'));

        if (Math.abs(lat) > 90 || Math.abs(lon) > 180) {
            showNotification('Широта должна быть в пределах ±90, долгота — ±180', 'error');
            return null;
        }

        if (lat === currentLocker?.location_lat && lon === currentLocker?.location_lon) {
            return { changed: false };
        }

        return {
            payload: { location_lat: lat, location_lon: lon },
            display: `${lat}, ${lon}`
        };
    }

    if (!value) {
        showNotification(
            field === 'inventory_number' ? 'Инвентарный номер не может быть пустым' : 'Адрес не может быть пустым',
            'error'
        );
        return null;
    }

    if (value === (currentLocker?.[field] ?? '')) return { changed: false };

    return { payload: { [field]: value }, display: value };
}

async function saveInlineEdit(field) {
    if (editingField !== field) return;

    const valueEl = document.getElementById(EDITABLE_FIELDS[field].valueId);
    const result = buildInlineEditPayload(field, valueEl.textContent);

    
    if (!result) {
        valueEl.focus();
        return;
    }

    
    if (result.changed === false) {
        cancelInlineEdit();
        return;
    }

    setInlineEditPending(field, true);

    try {
        const response = await fetch(`${API_BASE}/locker/${lockerUuid}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(result.payload)
        });

        if (!response.ok) {
            const body = await response.json().catch(() => null);
            showNotification(body?.detail || `Не удалось сохранить изменения (HTTP ${response.status})`, 'error');
            setInlineEditPending(field, false);
            valueEl.focus();
            return;
        }

        Object.assign(currentLocker, result.payload);
        editingField = null;
        setInlineEditPending(field, false);
        setInlineEditMode(field, false);
        valueEl.textContent = result.display;
        valueEl.dataset.original = result.display;
        applyLockerFieldToUi(field);

        showNotification('Изменения сохранены', 'success');
    } catch (err) {
        console.error('Ошибка сохранения данных шкафа:', err);
        showNotification('Не удалось подключиться к серверу', 'error');
        setInlineEditPending(field, false);
        valueEl.focus();
    }
}

// Дублирующие места вывода того же значения (шапка, модалка двери)
function applyLockerFieldToUi(field) {
    if (field === 'inventory_number') {
        document.getElementById('headerInventoryNumber').textContent = currentLocker.inventory_number;
        document.getElementById('doorModalInventory').textContent = currentLocker.inventory_number;
    } else if (field === 'location_address') {
        document.getElementById('headerAddress').textContent = currentLocker.location_address;
        document.getElementById('doorModalAddress').textContent = currentLocker.location_address || '—';
    }
}

// ── Настройки датчика ──────────────────────────────────────────

function openSensorSettings(sensorId) {
    const sensor = sensorsMap[sensorId];
    if (!sensor) return;

    currentSensorId = sensorId;
    const isThreshold = isThresholdSensor(sensor);

    document.getElementById('sensorSettingsName').textContent = sensor.name;
    document.getElementById('sensorSettingsActive').checked = !!sensor.is_active;
    document.getElementById('sensorSettingsThresholds').style.display = isThreshold ? 'block' : 'none';

    if (isThreshold) {
        document.getElementById('sensorSettingsMin').value = sensor.min_value;
        document.getElementById('sensorSettingsMax').value = sensor.max_value;
    }

    document.getElementById('sensorSettingsModal').style.display = 'flex';
}

function closeSensorSettingsModal() {
    document.getElementById('sensorSettingsModal').style.display = 'none';
    currentSensorId = null;
}

async function saveSensorSettings() {
    const sensor = sensorsMap[currentSensorId];
    if (!sensor) return;

    const isActive = document.getElementById('sensorSettingsActive').checked;
    const isThreshold = isThresholdSensor(sensor);

    const saveBtn = document.getElementById('sensorSettingsModalSave');
    saveBtn.disabled = true;

    try {
        const requests = [];

        if (isActive !== !!sensor.is_active) {
            requests.push(
                fetch(`${API_BASE}/locker/${lockerUuid}/sensors/${currentSensorId}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ is_active: isActive })
                })
            );
        }

        if (isThreshold) {
            const min = parseFloat(document.getElementById('sensorSettingsMin').value);
            const max = parseFloat(document.getElementById('sensorSettingsMax').value);
            if (!isNaN(min) && !isNaN(max) && (min !== sensor.min_value || max !== sensor.max_value)) {
                requests.push(
                    fetch(`${API_BASE}/locker/${lockerUuid}/sensors/${currentSensorId}/thresholds`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ min_value: min, max_value: max })
                    })
                );
            }
        }

        const results = await Promise.all(requests);
        const failed = results.find(r => !r.ok);
        if (failed) throw new Error(`HTTP ${failed.status}`);

        closeSensorSettingsModal();
        await loadSensors(lockerUuid, true);
    } catch (err) {
        console.error('Ошибка сохранения настроек датчика:', err);
    } finally {
        saveBtn.disabled = false;
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
    
    stopDoorTimer();
    stopDoorCommandPoll();
    stopSensorsAutoRefresh();
    relayBlock?.unmount();
    document.removeEventListener('visibilitychange', onSensorsVisibilityChange);
    document.addEventListener('visibilitychange', onSensorsVisibilityChange);

    
    lockerUuid = null;
    doorLocker = null;
    doorListenersReady = false;
    sensorsMap = {};
    sensorCards = {};
    sensorThresholds = {};
    sensorsLoading = false;
    currentSensorId = null;
    pending2fa = null;
    currentLocker = null;
    editingField = null;

    
    lockerUuid = sessionStorage.getItem('locker_detail_uuid');

    if (!lockerUuid) {
        showError('Не указан UUID', 'Шкаф не выбран. Перейдите из списка открытых шкафов.');
        return;
    }

    
    const sensorModal = document.getElementById('sensorSettingsModal');
    document.getElementById('sensorSettingsModalClose').addEventListener('click', closeSensorSettingsModal);
    document.getElementById('sensorSettingsModalCancel').addEventListener('click', closeSensorSettingsModal);
    document.getElementById('sensorSettingsModalSave').addEventListener('click', saveSensorSettings);
    sensorModal.addEventListener('click', e => { if (e.target === sensorModal) closeSensorSettingsModal(); });
    document.getElementById('sensorsRefreshBtn').addEventListener('click', refreshSensorsManually);

    document.getElementById('emergencyRequestResolveBtn').addEventListener('click', resolveEmergencyRequest);

    setupInlineEdit();

    const statusResetModal = document.getElementById('statusResetModal');
    const closeStatusResetModal = () => { statusResetModal.style.display = 'none'; };
    document.getElementById('statusResetBtn').addEventListener('click', () => { statusResetModal.style.display = 'flex'; });
    document.getElementById('statusResetModalClose').addEventListener('click', closeStatusResetModal);
    document.getElementById('statusResetModalCancel').addEventListener('click', closeStatusResetModal);
    document.getElementById('statusResetModalConfirm').addEventListener('click', resetLockerStatus);
    statusResetModal.addEventListener('click', e => { if (e.target === statusResetModal) closeStatusResetModal(); });

    loadLockerDetail(lockerUuid);
    mountRelayBlock(lockerUuid);
}
