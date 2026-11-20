// logs_new.js


const COMMANDS_API = `${API_BASE}/commands/history`;
const SECURITY_API = `${API_BASE}/events/security`;

const API_HEADERS = { 'Content-Type': 'application/json' };

// ─── Состояние пагинации ─────────────────────────────────────────────────────

let cmdCurrentPage = 1, cmdTotalPages = 1, cmdPageSize = 100;
let cmdStatsData = { total: 0, executed: 0, failed: 0, pending: 0 };

let secCurrentPage = 1, secTotalPages = 1, secPageSize = 100;
let secStatsData = { total: 0, high: 0, medium: 0, low: 0 };

let isPaused          = false;
let currentMode       = 'incidents';
let currentFilters    = { timeRange: null };
let autoUpdateInterval = null;

const ROLE_LABELS = {
    worker:        'Рабочий',
    administrator: 'Администратор',
    operator:      'Оператор'
};

// ─── Инициализация ───────────────────────────────────────────────────────────

export function initLogsModule() {
    setupEventListeners();
    initFilters();

    const moduleFilter = document.getElementById('moduleFilter');
    switchMode(moduleFilter ? moduleFilter.value : 'incidents');

    startAutoUpdate();
}

function switchMode(value) {
    if (value === 'history') {
        currentMode = 'history';
        cmdCurrentPage = 1;
        loadCommandHistory();
    } else if (value === 'incidents') {
        currentMode = 'incidents';
        secCurrentPage = 1;
        loadSecurityEvents();
    }
}

function reloadCurrentMode() {
    if (currentMode === 'history')   loadCommandHistory(1, true);
    else if (currentMode === 'incidents') loadSecurityEvents(1, true);
}

// ─── Обработчики событий ─────────────────────────────────────────────────────

function setupEventListeners() {
    document.getElementById('moduleFilter')?.addEventListener('change', function () {
        switchMode(this.value);
    });

    document.querySelectorAll('.btn-time-preset').forEach(btn => {
        btn.addEventListener('click', function () {
            const hours = parseInt(this.dataset.hours);
            const now   = new Date();
            const from  = new Date(now.getTime() - hours * 3600 * 1000);
            currentFilters.timeRange = { from, to: now };
            window.timeRangePicker?.setDate([from, now]);
            renderActiveFilters();
            reloadCurrentMode();
        });
    });

    document.getElementById('pauseBtn')?.addEventListener('click', togglePause);
    document.getElementById('refreshBtn')?.addEventListener('click', () => reloadCurrentMode());

    document.getElementById('closeLogModal')?.addEventListener('click',  closeLogModal);
    document.getElementById('closeDetailBtn')?.addEventListener('click', closeLogModal);
    document.getElementById('copyLogBtn')?.addEventListener('click',     copyLogDetails);
}

function initFilters() {
    window.timeRangePicker = flatpickr('#timeRange', {
        locale:     'ru',
        mode:       'range',
        dateFormat: 'd.m.Y H:i',
        enableTime: true,
        time_24hr:  true,
        static:     true,
        onChange(selectedDates) {
            if (selectedDates.length === 2) {
                currentFilters.timeRange = { from: selectedDates[0], to: selectedDates[1] };
                renderActiveFilters();
                reloadCurrentMode();
            } else if (selectedDates.length === 0) {
                currentFilters.timeRange = null;
                renderActiveFilters();
                reloadCurrentMode();
            }
        }
    });
}

function renderActiveFilters() {
    const container = document.getElementById('activeFilters');
    if (!container) return;

    if (!currentFilters.timeRange) {
        container.innerHTML = '';
        container.style.display = 'none';
        return;
    }
    container.style.display = 'flex';

    const fmt = date => date.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });

    container.innerHTML = `
        <div class="active-filter">
            <i class="fas fa-clock" style="font-size:11px;color:var(--gray-color)"></i>
            <span>${fmt(currentFilters.timeRange.from)} — ${fmt(currentFilters.timeRange.to)}</span>
            <button class="filter-remove" title="Сбросить фильтр">×</button>
        </div>`;

    container.querySelector('.filter-remove').addEventListener('click', () => {
        currentFilters.timeRange = null;
        window.timeRangePicker?.clear();
        renderActiveFilters();
        reloadCurrentMode();
    });
}

// ─── Параметры запроса ───────────────────────────────────────────────────────

function buildTimeParams() {
    if (!currentFilters.timeRange) return '';
    const from = encodeURIComponent(currentFilters.timeRange.from.toISOString());
    const to   = encodeURIComponent(currentFilters.timeRange.to.toISOString());
    return `&from=${from}&to=${to}`;
}

// ─── История команд ───────────────────────────────────────────────────────────

async function loadCommandHistory(page, silent = false) {
    if (page !== undefined) cmdCurrentPage = page;
    const offset = (cmdCurrentPage - 1) * cmdPageSize;

    const terminal = document.getElementById('logsTerminal');
    const prevContent = terminal.innerHTML;

    try {
        if (!silent) showLoading();
        const url      = `${COMMANDS_API}?limit=${cmdPageSize}&offset=${offset}${buildTimeParams()}`;
        const response = await fetch(url, { headers: API_HEADERS });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data  = await response.json();
        const items = data.items || [];
        const total = data.total || 0;
        cmdTotalPages = Math.max(1, Math.ceil(total / cmdPageSize));

        cmdStatsData = { total, executed: 0, failed: 0, pending: 0 };
        items.forEach(item => {
            if      (item.status === 'executed') cmdStatsData.executed++;
            else if (item.status === 'failed')   cmdStatsData.failed++;
            else                                 cmdStatsData.pending++;
        });

        displayCommandHistory(items);
        updateStats();
        renderCmdPagination(total);
    } catch (error) {
        console.error('Ошибка загрузки истории команд:', error);
        terminal.innerHTML = prevContent;
        showNotification('Ошибка загрузки истории команд', 'error');
    }
}

function displayCommandHistory(items) {
    const terminal = document.getElementById('logsTerminal');

    if (items.length === 0) {
        terminal.innerHTML = `
            <div class="terminal-empty">
                <i class="fas fa-history"></i>
                <p>История команд пуста</p>
                <p class="text-muted">Нет записей за выбранный период</p>
            </div>`;
        setFooterCounts(0, 0);
        renderCmdPagination(0);
        return;
    }

    terminal.innerHTML = items.map(createCommandEntry).join('');
    setFooterCounts(items.length, cmdStatsData.total);
}

const CMD_STATUS_MAP = {
    pending:   { cls: 'pending',   label: 'ОЖИДАНИЕ' },
    sent:      { cls: 'sent',      label: 'ОТПРАВЛЕНО' },
    delivered: { cls: 'delivered', label: 'ДОСТАВЛЕНО' },
    executed:  { cls: 'executed',  label: 'ВЫПОЛНЕНО' },
    failed:    { cls: 'error',     label: 'ОШИБКА' }
};

function createCommandEntry(item) {
    const timestamp  = formatDateTime(new Date(item.created_at));
    const { cls, label } = CMD_STATUS_MAP[item.status] || { cls: 'info', label: (item.status || '').toUpperCase() };
    const errorHtml  = item.error_message
        ? `<span class="log-cmd-error">${escapeHtml(item.error_message)}</span>`
        : '';

    return `
        <div class="log-entry" data-cmd-id="${item.id}">
            <span class="log-timestamp">${timestamp}</span>
            <span class="log-level level-${cls}">${label}</span>
            <span class="log-module">${escapeHtml(item.command_type || '—')}</span>
            <span class="log-locker">${escapeHtml(item.locker_inventory_number || '—')}</span>
            <span class="log-user">${escapeHtml(item.user_full_name || '—')}</span>
            <span class="log-message">${errorHtml}</span>
        </div>`;
}

// ─── Инциденты безопасности ───────────────────────────────────────────────────

const SEC_EVENT_TYPE_LABELS = {
    threshold_violation: 'Выход за пороговое значение',
    intrusion:           'Вторжение',
    offline:             'Офлайн',
    door_tamper:         'Вскрытие двери'
};

async function loadSecurityEvents(page, silent = false) {
    if (page !== undefined) secCurrentPage = page;
    const offset = (secCurrentPage - 1) * secPageSize;

    const terminal = document.getElementById('logsTerminal');
    const prevContent = terminal.innerHTML;

    try {
        if (!silent) showLoading();
        const url      = `${SECURITY_API}?limit=${secPageSize}&offset=${offset}${buildTimeParams()}`;
        const response = await fetch(url, { headers: API_HEADERS });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data  = await response.json();
        const items = data.items || [];
        const total = data.total || 0;
        secTotalPages = Math.max(1, Math.ceil(total / secPageSize));

        secStatsData = { total, high: 0, medium: 0, low: 0 };
        items.forEach(item => {
            const sev = (item.severity || '').toLowerCase();
            if      (sev === 'high')   secStatsData.high++;
            else if (sev === 'medium') secStatsData.medium++;
            else                       secStatsData.low++;
        });

        displaySecurityEvents(items);
        updateStats();
        renderSecPagination(total);
    } catch (error) {
        console.error('Ошибка загрузки инцидентов:', error);
        terminal.innerHTML = prevContent;
        showNotification('Ошибка загрузки инцидентов', 'error');
    }
}

function displaySecurityEvents(items) {
    const terminal = document.getElementById('logsTerminal');

    if (items.length === 0) {
        terminal.innerHTML = `
            <div class="terminal-empty">
                <i class="fas fa-shield-alt"></i>
                <p>Инцидентов нет</p>
                <p class="text-muted">Нет записей за выбранный период</p>
            </div>`;
        setFooterCounts(0, 0);
        renderSecPagination(0);
        return;
    }

    terminal.innerHTML = items.map(createSecurityEntry).join('');
    setFooterCounts(items.length, secStatsData.total);
}

const SEC_SEVERITY_CLASS = { high: 'error', critical: 'error', medium: 'warning', low: 'info' };
const SEC_SEVERITY_LABEL = { high: 'ВЫСОКИЙ', critical: 'КРИТИЧЕСКИЙ', medium: 'СРЕДНИЙ', low: 'НИЗКИЙ' };

function createSecurityEntry(item) {
    const rawDate        = item.occurred_at ? String(item.occurred_at).replace(' ', 'T') : null;
    const timestamp      = rawDate ? formatDateTime(new Date(rawDate)) : '—';
    const severity       = (item.severity || 'low').toLowerCase();
    const severityClass  = SEC_SEVERITY_CLASS[severity] || 'info';
    const severityLabel  = SEC_SEVERITY_LABEL[severity] || severity.toUpperCase();
    const eventTypeLabel = SEC_EVENT_TYPE_LABELS[item.event_type] || item.event_type || '—';

    let extraHtml = '';
    if (item.event_type === 'threshold_violation') {
        extraHtml = (item.violated_thresholds || [])
            .map(t => escapeHtml(t.description || ''))
            .filter(Boolean)
            .join(' | ');
    }
    // intrusion, offline, door_tamper — будет реализовано позже

    return `
        <div class="log-entry" data-sec-id="${item.id}">
            <span class="log-timestamp">${timestamp}</span>
            <span class="log-level level-${severityClass}">${severityLabel}</span>
            <span class="log-module">${escapeHtml(eventTypeLabel)}</span>
            <span class="log-locker">${escapeHtml(String(item.locker_id || '—'))}</span>
            <span class="log-message">${extraHtml}</span>
        </div>`;
}

// ─── Пагинация ────────────────────────────────────────────────────────────────

function getPaginationPages(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
    if (current <= 4)          return [1, 2, 3, 4, 5, '...', total];
    if (current >= total - 3)  return [1, '...', total - 4, total - 3, total - 2, total - 1, total];
    return [1, '...', current - 1, current, current + 1, '...', total];
}

function buildPaginationHtml(currentPage, totalPages, pageSizeOptions, currentPageSize, selectId) {
    let html = '<div class="pagination-footer">';

    html += '<div class="page-size-selector"><div class="page-size-select-wrapper">';
    html += `<select class="page-size-select" id="${selectId}">`;
    pageSizeOptions.forEach(size => {
        html += `<option value="${size}"${size === currentPageSize ? ' selected' : ''}>${size}</option>`;
    });
    html += '</select><i class="fas fa-chevron-down page-size-arrow"></i></div></div>';

    const pages = getPaginationPages(currentPage, totalPages);
    html += '<div class="pagination">';
    html += `<button class="pagination-btn pagination-prev" ${currentPage === 1 ? 'disabled' : ''} data-page="${currentPage - 1}">
        <i class="fas fa-chevron-left"></i></button>`;
    pages.forEach(p => {
        if (p === '...') {
            html += '<span class="pagination-ellipsis">…</span>';
        } else {
            html += `<button class="pagination-btn pagination-page${p === currentPage ? ' active' : ''}" data-page="${p}">${p}</button>`;
        }
    });
    html += `<button class="pagination-btn pagination-next" ${currentPage === totalPages ? 'disabled' : ''} data-page="${currentPage + 1}">
        <i class="fas fa-chevron-right"></i></button>`;
    html += '</div></div>';
    return html;
}

function renderCmdPagination(total) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;
    if (cmdTotalPages <= 1) { container.innerHTML = ''; return; }

    container.innerHTML = buildPaginationHtml(cmdCurrentPage, cmdTotalPages, [20, 50, 100, 200], cmdPageSize, 'cmdPageSizeSelect');

    container.querySelectorAll('.pagination-btn:not(:disabled)').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page);
            if (page && page !== cmdCurrentPage) loadCommandHistory(page);
        });
    });
    container.querySelector('#cmdPageSizeSelect')?.addEventListener('change', function () {
        const size = parseInt(this.value);
        if (size !== cmdPageSize) { cmdPageSize = size; cmdCurrentPage = 1; loadCommandHistory(); }
    });
}

function renderSecPagination(total) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;
    if (secTotalPages <= 1) { container.innerHTML = ''; return; }

    container.innerHTML = buildPaginationHtml(secCurrentPage, secTotalPages, [20, 50, 100, 200], secPageSize, 'secPageSizeSelect');

    container.querySelectorAll('.pagination-btn:not(:disabled)').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page);
            if (page && page !== secCurrentPage) loadSecurityEvents(page);
        });
    });
    container.querySelector('#secPageSizeSelect')?.addEventListener('change', function () {
        const size = parseInt(this.value);
        if (size !== secPageSize) { secPageSize = size; secCurrentPage = 1; loadSecurityEvents(); }
    });
}

// ─── Статистика ───────────────────────────────────────────────────────────────

function updateStats() {
    const now = new Date().toLocaleTimeString('ru-RU');

    if (currentMode === 'history') {
        setStats(cmdStatsData.total, cmdStatsData.failed, cmdStatsData.pending, cmdStatsData.executed, now);
    } else if (currentMode === 'incidents') {
        setStats(secStatsData.total, secStatsData.high, secStatsData.medium, secStatsData.low, now);
    }
}

function setStats(total, errors, warnings, info, time) {
    document.getElementById('totalLogs').textContent  = total;
    document.getElementById('lastUpdate').textContent = time;
}

function setFooterCounts(shown, total) {
    document.getElementById('shownLogs').textContent    = shown;
    document.getElementById('filteredLogs').textContent = total;
}

// ─── Пауза / обновление ───────────────────────────────────────────────────────

function togglePause() {
    isPaused = !isPaused;
    const pauseBtn      = document.getElementById('pauseBtn');
    const liveIndicator = document.getElementById('liveIndicator');

    if (isPaused) {
        pauseBtn.innerHTML      = '<i class="fas fa-play"></i>';
        pauseBtn.title          = 'Возобновить';
        liveIndicator.style.opacity = '0.5';
    } else {
        pauseBtn.innerHTML      = '<i class="fas fa-pause"></i>';
        pauseBtn.title          = 'Приостановить';
        liveIndicator.style.opacity = '1';
        reloadCurrentMode();
    }
}

function startAutoUpdate() {
    if (autoUpdateInterval) clearInterval(autoUpdateInterval);
    autoUpdateInterval = setInterval(() => {
        if (!isPaused) reloadCurrentMode();
    }, 10000);
}

// ─── Модальное окно ───────────────────────────────────────────────────────────

function closeLogModal() {
    document.getElementById('logDetailModal').style.display = 'none';
}

function copyLogDetails() {
    const fields = ['detailLevel', 'detailTimestamp', 'detailModule', 'detailMessage', 'detailIp', 'detailUser', 'detailCabinet'];
    const labels = ['Уровень', 'Время', 'Модуль', 'Сообщение', 'IP', 'Пользователь', 'Шкаф'];
    const text = fields
        .map((id, i) => `${labels[i]}: ${document.getElementById(id)?.textContent || '—'}`)
        .join('\n');

    navigator.clipboard.writeText(text).then(() => showNotification('Детали скопированы', 'success'));
}

// ─── Вспомогательные функции ──────────────────────────────────────────────────

function formatDateTime(date) {
    if (isNaN(date)) return '—';
    const now         = new Date();
    const diffMinutes = Math.floor((now - date) / 60000);
    if (diffMinutes < 1)    return 'только что';
    if (diffMinutes < 60)   return `${diffMinutes} мин. назад`;
    if (diffMinutes < 1440) return `${Math.floor(diffMinutes / 60)} ч. назад`;
    return date.toLocaleDateString('ru-RU') + ' ' + date.toLocaleTimeString('ru-RU').slice(0, 5);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function showLoading() {
    document.getElementById('logsTerminal').innerHTML = `
        <div class="terminal-empty">
            <div class="spinner"></div>
            <p>Загрузка...</p>
        </div>`;
}

function showErrorState() {
    document.getElementById('logsTerminal').innerHTML = `
        <div class="terminal-empty">
            <i class="fas fa-exclamation-triangle" style="color:#ff6b6b;"></i>
            <p>Не удалось загрузить данные</p>
            <p class="text-muted">Проверьте подключение к серверу</p>
        </div>`;
}

function showNotification(message, type) {
    const el = document.createElement('div');
    el.className = `notification ${type}`;
    el.innerHTML = `
        <div class="notification-content">
            <i class="fas fa-${type === 'error' ? 'exclamation-circle' : type === 'warning' ? 'exclamation-triangle' : 'check-circle'}"></i>
            <span>${message}</span>
        </div>`;
    document.body.appendChild(el);
    el.style.display = 'flex';
    setTimeout(() => el.remove(), 3000);
}
