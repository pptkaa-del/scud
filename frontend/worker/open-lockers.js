let lockersUpdateInterval = null;

// Загрузка открытых шкафов + активных запросов помощи
async function loadOpenLockers() {
    try {
        const [lockersRes, emergencyRes] = await Promise.all([
            fetch(`${API_BASE}/lockers?door=open`, { cache: 'no-store' }),
            fetch(`${API_BASE}/emergency-requests?status=pending`, { cache: 'no-store' })
        ]);
        if (!lockersRes.ok) throw new Error(`HTTP error ${lockersRes.status}`);
        if (!emergencyRes.ok) throw new Error(`HTTP error ${emergencyRes.status}`);

        const lockersData = await lockersRes.json();
        const emergencyData = await emergencyRes.json();

        const helpIds = new Set(emergencyData.items.map(r => r.locker_id));

        const lockers = lockersData.items.map(locker => ({
            ...locker,
            state_door: helpIds.has(locker.id) ? 'help' : 'open'
        }));

        renderLockers(lockers);
        startTimerUpdater();
    } catch (error) {
        console.error('Ошибка загрузки шкафов:', error);
        showNotification('Не удалось загрузить список шкафов', 'error');
    }
}

function renderLockers(lockers) {
    const container = document.getElementById('lockersList');
    if (!container) return;

    container.innerHTML = '';

    if (!lockers.length) {
        container.innerHTML = `
            <div class="lockers-empty">
                <i class="fas fa-door-closed"></i>
                <p>Открытых шкафов нет</p>
            </div>
        `;
        return;
    }

    lockers.forEach(locker => {
        const card = createLockerCard(locker);
        container.appendChild(card);
    });
}

function createLockerCard(locker) {
    const card = document.createElement('a');
    card.href = '#lockers-detail';
    card.className = `locker-card ${locker.state_door}`;
    card.dataset.uuid = locker.id;
    card.dataset.lastOpen = locker.last_opened_at || 0;

    card.addEventListener('click', (e) => {
        e.preventDefault();
        sessionStorage.setItem('locker_detail_uuid', locker.id);
        location.hash = 'lockers-detail';
    });

    const isHelp = locker.state_door === 'help';
    const timeText = locker.last_opened_at ? formatTimeElapsed(locker.last_opened_at) : '—';

    card.innerHTML = `
        <div class="locker-card-header">
            <span class="locker-inventory">${locker.inventory_number}</span>
            <span class="locker-state-badge ${isHelp ? 'state-help' : 'state-open'}">
                ${isHelp ? '<i class="fas fa-life-ring"></i> Помощь' : 'Открыт'}
            </span>
        </div>
        <div class="locker-address">
            <i class="fas fa-map-marker-alt"></i>
            <span>${locker.location_address || '—'}</span>
        </div>
        <div class="locker-time-open">
            <i class="fas fa-clock"></i>
            <span class="time-elapsed" data-uuid="${locker.id}">${timeText}</span>
        </div>
    `;

    return card;
}

// Бэкенд может отдавать: секунды (1741000000), миллисекунды (1741000000000) или ISO-строку
function toMilliseconds(ts) {
    if (!ts) return null;
    const num = Number(ts);
    if (!isNaN(num) && num > 0) {
        return num < 1e11 ? num * 1000 : num;
    }
    const d = new Date(ts);
    return isNaN(d.getTime()) ? null : d.getTime();
}

function formatTimeElapsed(timestamp) {
    const ms = toMilliseconds(timestamp);
    if (!ms) return '—';
    const elapsed = Math.floor((Date.now() - ms) / 1000);
    if (elapsed < 0) return '00:00:00';
    const hours = Math.floor(elapsed / 3600);
    const minutes = Math.floor((elapsed % 3600) / 60);
    const seconds = elapsed % 60;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

function updateLockersTime() {
    // Самоотключение, если модуль выгружен из DOM
    if (!document.getElementById('lockersList')) {
        clearInterval(lockersUpdateInterval);
        lockersUpdateInterval = null;
        return;
    }
    document.querySelectorAll('.time-elapsed').forEach(el => {
        const uuid = el.dataset.uuid;
        const card = document.querySelector(`.locker-card[data-uuid="${uuid}"]`);
        if (card && card.dataset.lastOpen) {
            el.textContent = formatTimeElapsed(card.dataset.lastOpen);
        }
    });
}

function startTimerUpdater() {
    if (lockersUpdateInterval) clearInterval(lockersUpdateInterval);
    lockersUpdateInterval = setInterval(updateLockersTime, 1000);
}

function showNotification(text, type = 'success') {
    let notification = document.getElementById('notification');
    if (!notification) {
        notification = document.createElement('div');
        notification.id = 'notification';
        notification.className = 'notification';
        notification.innerHTML = `
            <div class="notification-content">
                <i class="fas fa-check-circle notification-icon"></i>
                <span id="notificationText">${text}</span>
            </div>
        `;
        document.body.appendChild(notification);
    }

    const notificationText = document.getElementById('notificationText');
    const icon = notification.querySelector('.notification-icon');
    if (notificationText) notificationText.textContent = text;

    if (type === 'error') {
        notification.style.backgroundColor = '#e74c3c';
        if (icon) icon.className = 'fas fa-exclamation-circle notification-icon';
    } else if (type === 'warning') {
        notification.style.backgroundColor = '#f39c12';
        if (icon) icon.className = 'fas fa-exclamation-triangle notification-icon';
    } else {
        notification.style.backgroundColor = '#27ae60';
        if (icon) icon.className = 'fas fa-check-circle notification-icon';
    }

    notification.style.display = 'flex';
    setTimeout(() => { notification.style.display = 'none'; }, 3000);
}

export function initOpenLockersModule() {
    if (lockersUpdateInterval) {
        clearInterval(lockersUpdateInterval);
        lockersUpdateInterval = null;
    }
    loadOpenLockers();
}
