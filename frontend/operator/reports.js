// reports.js — Модуль отчётов
const SECTION_LABELS = {
    locker_open: 'Открытые шкафы',
    incidents: 'Инциденты',
    user_activity: 'Активность',
    cabinet_state: 'Состояние шкафов',
    system_logs: 'Журнал событий',
    contractors: 'Подрядчики'
};

const FILTER_LABELS = {
    report_type: {
        locker_open: 'Открытые шкафы',
        incidents: 'Инциденты',
        user_activity: 'Активность',
        cabinet_state: 'Состояние шкафов',
        system_logs: 'Журнал событий',
        contractors: 'Подрядчики'
    },
    status: {
        pending: 'В очереди',
        running: 'Формируется',
        ready: 'Готов',
        failed: 'Ошибка',
        expired: 'Истёк'
    }
};

const FILTER_SELECT_IDS = {
    report_type: 'filterReportType',
    status: 'filterStatus'
};

let reportsData = [];
let currentSearch = '';
let currentFilters = { report_type: null, status: null };
let currentPage = 1;
let totalPages = 1;
let pageSize = 20;
let deleteTargetId = null;
let searchDebounceTimer = null;
let pollInterval = null;

// ===== Поллинг =====

function startPolling() {
    if (pollInterval) return;
    pollInterval = setInterval(() => {
        // Самоотключение, если модуль выгружен из DOM
        if (!document.getElementById('reportsList')) {
            stopPolling();
            return;
        }
        loadReports(currentSearch, null, true);
    }, 5000);
}

function stopPolling() {
    if (!pollInterval) return;
    clearInterval(pollInterval);
    pollInterval = null;
}

function updatePolling() {
    const hasActive = reportsData.some(r => r.status === 'pending' || r.status === 'running');
    hasActive ? startPolling() : stopPolling();
}

// ===== Загрузка данных =====

async function loadReports(search = '', page = null, silent = false) {
    if (page !== null) currentPage = page;
    currentSearch = search;

    const tbody = document.getElementById('reportsList');
    const emptyState = document.getElementById('emptyState');
    const paginationContainer = document.getElementById('paginationContainer');

    if (!tbody) return;

    if (!silent) {
        emptyState.style.display = 'none';
        if (paginationContainer) paginationContainer.style.display = 'none';
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="loading-row">
                    <span class="spinner-sm"></span> Загрузка...
                </td>
            </tr>`;
    }

    try {
        const params = new URLSearchParams();
        if (search) params.set('q', search);
        if (currentFilters.report_type) params.set('report_type', currentFilters.report_type);
        if (currentFilters.status) params.set('status', currentFilters.status);
        params.set('limit', pageSize);
        params.set('offset', (currentPage - 1) * pageSize);
        const response = await fetch(`${API_BASE}/reports?${params}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        reportsData = data.items || [];
        const total = data.total ?? 0;
        totalPages = Math.max(1, Math.ceil(total / pageSize));
        renderPagination(total);
    } catch (err) {
        console.error('Ошибка загрузки отчётов:', err);
        if (silent) return;
        tbody.innerHTML = '';
        emptyState.style.display = 'block';
        showToast('Не удалось загрузить отчёты', 'error');
        return;
    }
    renderTable(reportsData);
    updatePolling();
}

// ===== Рендер таблицы =====

function renderTable(data) {
    const tbody = document.getElementById('reportsList');
    const emptyState = document.getElementById('emptyState');

    if (!data || data.length === 0) {
        tbody.innerHTML = '';
        emptyState.style.display = 'block';
        return;
    }

    emptyState.style.display = 'none';
    tbody.innerHTML = data.map(report => createReportRow(report)).join('');

    tbody.querySelectorAll('.btn-download').forEach(btn => {
        btn.addEventListener('click', () => downloadReport(btn.dataset.id));
    });
    tbody.querySelectorAll('.btn-delete').forEach(btn => {
        btn.addEventListener('click', () => openDeleteModal(btn.dataset.id));
    });
}

function createReportRow(report) {
    const fileName = report.name || '—';
    const typeLabel = SECTION_LABELS[report.report_type] || report.report_type || '—';
    const { badgeClass, badgeIcon, badgeText } = getStatusBadge(report.status);
    const canDownload = report.status === 'ready';

    return `
        <tr>
            <td>
                <div class="report-name-cell">
                    <div class="report-icon">
                        <i class="fas fa-chart-bar"></i>
                    </div>
                    <span class="report-name-text">${escapeHtml(fileName)}</span>
                </div>
            </td>
            <td><span class="section-tag">${escapeHtml(typeLabel)}</span></td>
            <td><span class="report-size">${formatFileSize(report.file_size)}</span></td>
            <td><span class="report-date">${formatDatetime(report.created_at)}</span></td>
            <td>
                <span class="status-badge ${badgeClass}">
                    ${badgeIcon}${badgeText}
                </span>
            </td>
            <td>
                <div class="actions-cell">
                    <button class="btn-action btn-download" data-id="${report.id}"
                            title="Скачать" ${canDownload ? '' : 'disabled'}>
                        <i class="fas fa-download"></i>
                    </button>
                    <button class="btn-action btn-delete" data-id="${report.id}" title="Удалить"
                            ${report.status === 'expired' ? 'disabled' : ''}>
                        <i class="fas fa-trash-alt"></i>
                    </button>
                </div>
            </td>
        </tr>
    `;
}

function getStatusBadge(status) {
    switch (status) {
        case 'ready':
            return { badgeClass: 'status-ready', badgeIcon: '<i class="fas fa-check-circle"></i> ', badgeText: 'Готов' };
        case 'pending':
            return { badgeClass: 'status-generating', badgeIcon: '<span class="spinner-sm"></span> ', badgeText: 'В очереди' };
        case 'running':
            return { badgeClass: 'status-generating', badgeIcon: '<span class="spinner-sm"></span> ', badgeText: 'Формируется' };
        case 'failed':
            return { badgeClass: 'status-error', badgeIcon: '<i class="fas fa-exclamation-circle"></i> ', badgeText: 'Ошибка' };
        case 'expired':
            return { badgeClass: 'status-expired', badgeIcon: '<i class="fas fa-clock"></i> ', badgeText: 'Истёк' };
        default:
            return { badgeClass: '', badgeIcon: '', badgeText: status || '—' };
    }
}

// ===== Пагинация =====

function renderPagination(total) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;

    const pageSizeOptions = [10, 20, 50, 100, 200];
    let html = '<div class="pagination-footer">';

    html += '<div class="page-size-selector">';
    html += '<div class="page-size-select-wrapper">';
    html += '<select class="page-size-select" id="pageSizeSelect">';
    pageSizeOptions.forEach(size => {
        html += `<option value="${size}"${size === pageSize ? ' selected' : ''}>${size}</option>`;
    });
    html += '</select>';
    html += '<i class="fas fa-chevron-down page-size-arrow"></i>';
    html += '</div></div>';

    if (totalPages > 1) {
        const pages = getPaginationPages(currentPage, totalPages);
        html += '<div class="pagination">';
        html += `<button class="pagination-btn pagination-prev" ${currentPage === 1 ? 'disabled' : ''} data-page="${currentPage - 1}">
            <i class="fas fa-chevron-left"></i>
        </button>`;
        pages.forEach(p => {
            if (p === '...') {
                html += '<span class="pagination-ellipsis">…</span>';
            } else {
                html += `<button class="pagination-btn pagination-page${p === currentPage ? ' active' : ''}" data-page="${p}">${p}</button>`;
            }
        });
        html += `<button class="pagination-btn pagination-next" ${currentPage === totalPages ? 'disabled' : ''} data-page="${currentPage + 1}">
            <i class="fas fa-chevron-right"></i>
        </button>`;
        html += '</div>';
    }

    html += '</div>';
    container.innerHTML = html;
    container.style.display = '';

    container.querySelectorAll('.pagination-btn:not(:disabled)').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page);
            if (page && page !== currentPage) loadReports(currentSearch, page);
        });
    });

    const pageSizeSelect = container.querySelector('#pageSizeSelect');
    if (pageSizeSelect) {
        pageSizeSelect.addEventListener('change', () => {
            const size = parseInt(pageSizeSelect.value);
            if (size !== pageSize) {
                pageSize = size;
                currentPage = 1;
                loadReports(currentSearch);
            }
        });
    }
}

function getPaginationPages(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
    if (current <= 4) return [1, 2, 3, 4, 5, '...', total];
    if (current >= total - 3) return [1, '...', total - 4, total - 3, total - 2, total - 1, total];
    return [1, '...', current - 1, current, current + 1, '...', total];
}

// ===== Фильтры =====

function applyFilter(key, value) {
    currentFilters[key] = value || null;
    const select = document.getElementById(FILTER_SELECT_IDS[key]);
    if (select) select.classList.toggle('active', !!value);
    renderFilterChips();
    currentPage = 1;
    loadReports(currentSearch);
}

function removeFilter(key) {
    currentFilters[key] = null;
    const select = document.getElementById(FILTER_SELECT_IDS[key]);
    if (select) { select.value = ''; select.classList.remove('active'); }
    renderFilterChips();
    currentPage = 1;
    loadReports(currentSearch);
}

function renderFilterChips() {
    const container = document.getElementById('filterChips');
    container.innerHTML = Object.entries(currentFilters)
        .filter(([, v]) => v)
        .map(([key, value]) => `
            <span class="filter-chip">
                ${FILTER_LABELS[key]?.[value] || value}
                <button type="button" class="filter-chip-remove" data-key="${key}">
                    <i class="fas fa-times"></i>
                </button>
            </span>`)
        .join('');
    container.querySelectorAll('.filter-chip-remove').forEach(btn => {
        btn.addEventListener('click', () => removeFilter(btn.dataset.key));
    });
}

// ===== Создание отчёта =====

function openCreateModal() {
    document.getElementById('reportForm').reset();
    document.getElementById('reportNameError').textContent = '';
    document.getElementById('sectionsError').textContent = '';
    document.getElementById('dateError').textContent = '';
    document.getElementById('reportName').classList.remove('error');
    document.getElementById('createModal').style.display = 'flex';
}

function closeCreateModal() {
    document.getElementById('createModal').style.display = 'none';
}

async function submitReport() {
    let valid = true;

    const name = document.getElementById('reportName').value.trim();
    if (!name) {
        document.getElementById('reportNameError').textContent = 'Введите название отчёта';
        document.getElementById('reportName').classList.add('error');
        valid = false;
    } else {
        document.getElementById('reportNameError').textContent = '';
        document.getElementById('reportName').classList.remove('error');
    }

    const checkedSections = Array.from(
        document.querySelectorAll('input[name="sections"]:checked')
    ).map(cb => cb.value);

    if (checkedSections.length === 0) {
        document.getElementById('sectionsError').textContent = 'Выберите хотя бы один раздел';
        valid = false;
    } else {
        document.getElementById('sectionsError').textContent = '';
    }

    const dateFrom = document.getElementById('dateFrom').value;
    const dateTo = document.getElementById('dateTo').value;

    if (!dateFrom || !dateTo) {
        document.getElementById('dateError').textContent = 'Укажите начало и конец периода';
        valid = false;
    } else if (dateFrom > dateTo) {
        document.getElementById('dateError').textContent = 'Дата начала не может быть позже даты окончания';
        valid = false;
    } else {
        document.getElementById('dateError').textContent = '';
    }

    if (!valid) return;

    const payload = {
        report_type: checkedSections[0],
        name: name,
        params: {
            date_from: `${dateFrom}T00:00:00Z`,
            date_to: `${dateTo}T23:59:59Z`
        }
    };

    try {
        const response = await fetch(`${API_BASE}/reports`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (response.status !== 202) {
            const err = await response.json().catch(() => ({}));
            showToast(err.detail || `Ошибка ${response.status}`, 'error');
            return;
        }
    } catch (err) {
        console.error('Ошибка создания отчёта:', err);
        showToast('Не удалось создать отчёт', 'error');
        return;
    }

    closeCreateModal();
    showToast('Отчёт поставлен в очередь на формирование', 'success');
    await loadReports(currentSearch);
}

// ===== Удаление =====

function openDeleteModal(reportId) {
    const report = reportsData.find(r => r.id === reportId);
    if (!report) return;
    deleteTargetId = reportId;
    document.getElementById('deleteReportName').textContent = report.name || reportId;
    document.getElementById('deleteModal').style.display = 'flex';
}

function closeDeleteModal() {
    document.getElementById('deleteModal').style.display = 'none';
    deleteTargetId = null;
}

async function confirmDelete() {
    if (!deleteTargetId) return;

    try {
        const response = await fetch(`${API_BASE}/report/${deleteTargetId}`, { method: 'DELETE' });
        if (!response.ok && response.status !== 204) {
            const err = await response.json().catch(() => ({}));
            showToast(err.detail || `Ошибка ${response.status}`, 'error');
            closeDeleteModal();
            return;
        }
    } catch (err) {
        console.error('Ошибка удаления:', err);
        showToast('Не удалось удалить отчёт', 'error');
        return;
    }

    closeDeleteModal();
    showToast('Отчёт удалён', 'success');
    await loadReports(currentSearch);
}

// ===== Скачивание =====

function downloadReport(reportId) {
    const report = reportsData.find(r => r.id === reportId);
    if (!report) return;
    const link = document.createElement('a');
    link.href = `${API_BASE}/report/${reportId}/download`;
    link.download = report.file_name || reportId;
    link.click();
}

// ===== Вспомогательные функции =====

function formatDatetime(isoStr) {
    if (!isoStr) return '—';
    const date = new Date(isoStr);
    return date.toLocaleString('ru-RU', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatFileSize(bytes) {
    if (!bytes) return '—';
    if (bytes < 1024) return `${bytes} Б`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

function showToast(message, type = 'success') {
    const toast = document.getElementById('toastNotification');
    const icon = document.getElementById('toastIcon');
    const msg = document.getElementById('toastMessage');

    const icons = {
        success: 'fa-check-circle',
        error: 'fa-exclamation-circle',
        warning: 'fa-exclamation-triangle'
    };

    toast.className = `toast-notification${type === 'error' ? ' toast-error' : type === 'warning' ? ' toast-warning' : ''}`;
    icon.className = `fas ${icons[type] || icons.success}`;
    msg.textContent = message;
    toast.style.display = 'flex';
    toast.style.animation = 'none';
    requestAnimationFrame(() => { toast.style.animation = 'slideInToast 0.3s ease'; });

    clearTimeout(toast._hideTimeout);
    toast._hideTimeout = setTimeout(() => { toast.style.display = 'none'; }, 3000);
}

function setupEventListeners() {
    const searchInput = document.getElementById('searchReports');
    const clearSearch = document.getElementById('clearSearch');

    searchInput.addEventListener('input', function () {
        clearSearch.style.display = this.value ? 'flex' : 'none';
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => { currentPage = 1; loadReports(this.value.trim()); }, 300);
    });

    clearSearch.addEventListener('click', function () {
        searchInput.value = '';
        this.style.display = 'none';
        currentPage = 1;
        loadReports('');
    });

    [
        { id: 'filterReportType', key: 'report_type' },
        { id: 'filterStatus', key: 'status' }
    ].forEach(({ id, key }) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', function () { applyFilter(key, this.value); });
    });

    document.querySelectorAll('input[name="sections"]').forEach(cb => {
        cb.addEventListener('change', function () {
            if (this.checked) {
                document.querySelectorAll('input[name="sections"]').forEach(other => {
                    if (other !== this) other.checked = false;
                });
            }
        });
    });

    document.getElementById('createReportBtn').addEventListener('click', openCreateModal);
    document.getElementById('closeCreateModal').addEventListener('click', closeCreateModal);
    document.getElementById('cancelCreateBtn').addEventListener('click', closeCreateModal);
    document.getElementById('createModal').addEventListener('click', function (e) {
        if (e.target === this) closeCreateModal();
    });
    document.getElementById('submitReportBtn').addEventListener('click', submitReport);

    document.getElementById('closeDeleteModal').addEventListener('click', closeDeleteModal);
    document.getElementById('cancelDeleteBtn').addEventListener('click', closeDeleteModal);
    document.getElementById('deleteModal').addEventListener('click', function (e) {
        if (e.target === this) closeDeleteModal();
    });
    document.getElementById('confirmDeleteBtn').addEventListener('click', confirmDelete);
}

export function initReportsModule() {
    stopPolling();

    reportsData = [];
    currentSearch = '';
    currentFilters = { report_type: null, status: null };
    currentPage = 1;
    totalPages = 1;
    pageSize = 20;
    deleteTargetId = null;
    searchDebounceTimer = null;

    setupEventListeners();
    loadReports();
}
