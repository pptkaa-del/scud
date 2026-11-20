// lockers-detail.js — Детальная страница шкафа


let lockerUuid = null;
let doorLocker = null;
let doorTimerInterval = null;
let doorListenersReady = false;
let doorCommandPollTimer = null;
let pending2fa = null; // { type, challengeId }
let statusRefreshTimer = null;
let statusLoading = false;
const STATUS_REFRESH_MS = 5000;
const STATUS_FETCH_TIMEOUT_MS = 4000;

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
        startStatusAutoRefresh(uuid);
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

    const infoStateEl = document.getElementById('infoState');
    infoStateEl.className = `connection-status ${locker.connectivity === 'online' ? 'online' : 'offline'}`;
    infoStateEl.innerHTML = `<i class="fas fa-circle"></i> ${locker.connectivity === 'online' ? 'В сети' : 'Не в сети'}`;

    if (locker.location_lat != null && locker.location_lon != null) {
        document.getElementById('infoCoordinates').textContent = `${locker.location_lat}, ${locker.location_lon}`;
    }

    document.getElementById('loadingState').style.display = 'none';
    document.getElementById('lockerDetailContent').style.display = 'block';

    if (locker.who_open_last) {
        loadLastOpener(locker.who_open_last);
    }

    initDoorControl(locker);
}

function renderConnectivity(locker) {
    const online = locker.connectivity === 'online';
    for (const id of ['headerState', 'infoState']) {
        const el = document.getElementById(id);
        el.className = `connection-status ${online ? 'online' : 'offline'}`;
        el.innerHTML = `<i class="fas fa-circle"></i> ${online ? 'В сети' : 'Не в сети'}`;
    }
    document.getElementById('doorOfflineNotice').style.display = online ? 'none' : 'flex';
    document.getElementById('doorRequestCodeBtn').disabled = !online;
}

function activeDoorStage() {
    for (let i = 1; i <= 3; i++) {
        if (document.getElementById(`doorStage${i}`)?.classList.contains('active')) return i;
    }
    return null;
}

function applyLiveStatus(locker) {
    renderConnectivity(locker);
    const wanted = locker.door === 'open' || locker.door === 'help' ? 2 : 1;
    const current = activeDoorStage();
    const reopened = wanted === 2 && doorLocker?.last_opened_at !== locker.last_opened_at;
    doorLocker = locker;
    if ((current === 1 || current === 2) && (current !== wanted || reopened)) setDoorStage(wanted);
}

async function refreshLockerStatus(uuid) {
    if (statusLoading || document.hidden || doorCommandPollTimer || pending2fa) return;
    statusLoading = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), STATUS_FETCH_TIMEOUT_MS);
    try {
        const response = await fetch(`${API_BASE}/locker/${uuid}`,
            { cache: 'no-store', signal: controller.signal });
        if (!response.ok || doorCommandPollTimer || pending2fa) return;
        applyLiveStatus(await response.json());
    } catch (err) {
        if (err.name !== 'AbortError') console.error('Ошибка обновления статуса шкафа:', err);
    } finally {
        clearTimeout(timeout);
        statusLoading = false;
    }
}

function startStatusAutoRefresh(uuid) {
    stopStatusAutoRefresh();
    statusRefreshTimer = setInterval(() => {
        if (!document.getElementById('lockerDetailContent')) {
            stopStatusAutoRefresh();
            return;
        }
        refreshLockerStatus(uuid);
    }, STATUS_REFRESH_MS);
}

function stopStatusAutoRefresh() {
    if (statusRefreshTimer) {
        clearInterval(statusRefreshTimer);
        statusRefreshTimer = null;
    }
}

function onStatusVisibilityChange() {
    if (!document.hidden && statusRefreshTimer && lockerUuid) refreshLockerStatus(lockerUuid);
}

// ── Управление дверью ──────────────────────────────────────────

function initDoorControl(locker) {
    doorLocker = locker;

    pending2fa = null;
    hideDoor2faStage();

    const offline = locker.connectivity !== 'online';
    document.getElementById('doorOfflineNotice').style.display = offline ? 'flex' : 'none';
    document.getElementById('doorRequestCodeBtn').disabled = offline;

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
    btn.disabled = pending;
    spinner.style.display = pending ? 'flex' : 'none';
}

function stopDoorCommandPoll() {
    if (doorCommandPollTimer) {
        clearInterval(doorCommandPollTimer);
        doorCommandPollTimer = null;
    }
}

async function sendDoorCommand(type) {
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

export function initLockersDetailModule() {
    // Останавливаем таймеры от предыдущего визита
    stopDoorTimer();
    stopDoorCommandPoll();
    stopStatusAutoRefresh();
    document.removeEventListener('visibilitychange', onStatusVisibilityChange);
    document.addEventListener('visibilitychange', onStatusVisibilityChange);

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

    loadLockerDetail(lockerUuid);
}
