
const headers = {
    'Content-Type': 'application/json'
};

// DOM-переменные переинициализируются при каждом вызове initContractorsModule
let contractorsListEl, emptyStateEl, searchInput, addBtn;
let modalOverlay, modalTitle, contractorNameInput, contractorIdInput, nameError;
let closeModalBtn, cancelModalBtn, saveBtn;
let deleteModal, deleteContractorNameSpan, closeDeleteModalBtn, cancelDeleteBtn, confirmDeleteBtn;
let notification, notificationMessage;

let contractors = [];
let currentPage = 1;
let totalPages = 1;
let pageSize = 20;
let deleteId = null;

const ROLE_LABELS = {
    worker: 'Рабочий',
    administrator: 'Администратор',
    operator: 'Оператор'
};

async function loadContractors(search = '', page = null) {
    if (page !== null) currentPage = page;

    try {
        const params = new URLSearchParams();
        if (search) params.set('q', search);
        params.set('limit', pageSize);
        params.set('offset', (currentPage - 1) * pageSize);

        const response = await fetch(`${API_BASE}/contractors?${params}`, { headers });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || 'Ошибка загрузки');
        }
        const data = await response.json();
        contractors = data.items || [];
        const total = data.total || 0;
        totalPages = Math.max(1, Math.ceil(total / pageSize));
        renderTable(contractors);
        renderPagination(total);
    } catch (error) {
        showNotification(error.message, 'error');
        console.error(error);
    }
}

function renderTable(data) {
    if (!data || data.length === 0) {
        contractorsListEl.innerHTML = '';
        emptyStateEl.style.display = 'block';
        return;
    }

    emptyStateEl.style.display = 'none';
    contractorsListEl.innerHTML = data.map(contractor => `
        <tr>
            <td class="contractor-name">${escapeHtml(contractor.name)}</td>
            <td>
                <div class="actions">
                    <button class="btn-icon edit" data-action="edit" data-id="${contractor.id}" data-name="${escapeHtml(contractor.name)}" title="Редактировать">
                        <i class="fas fa-edit"></i>
                    </button>
                    <button class="btn-icon delete" data-action="delete" data-id="${contractor.id}" data-name="${escapeHtml(contractor.name)}" title="Удалить">
                        <i class="fas fa-trash-alt"></i>
                    </button>
                    <button class="btn-icon team" data-action="team" data-id="${contractor.id}" title="Список сотрудников">
                        <i class="fas fa-users"></i>
                    </button>
                </div>
            </td>
        </tr>
    `).join('');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function filterContractors() {
    currentPage = 1;
    loadContractors(searchInput.value.trim());
}

function openAddModal() {
    modalTitle.textContent = 'Добавить подрядчика';
    contractorNameInput.value = '';
    contractorIdInput.value = '';
    nameError.textContent = '';
    modalOverlay.style.display = 'flex';
}

function editContractor(id, name) {
    modalTitle.textContent = 'Редактировать подрядчика';
    contractorNameInput.value = name;
    contractorIdInput.value = id;
    nameError.textContent = '';
    modalOverlay.style.display = 'flex';
}

function closeModal() {
    modalOverlay.style.display = 'none';
}

async function saveContractor() {
    const name = contractorNameInput.value.trim();
    if (!name) {
        nameError.textContent = 'Название обязательно';
        return;
    }
    if (name.length > 255) {
        nameError.textContent = 'Максимальная длина 255 символов';
        return;
    }

    const id = contractorIdInput.value;
    const url = id ? `${API_BASE}/contractor/${id}` : `${API_BASE}/contractors`;
    const method = id ? 'PUT' : 'POST';
    const body = JSON.stringify({ name });

    try {
        const response = await fetch(url, { method, headers, body });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || 'Ошибка сохранения');
        }
        closeModal();
        await loadContractors(searchInput.value.trim());
        showNotification(id ? 'Подрядчик обновлён' : 'Подрядчик добавлен');
    } catch (error) {
        showNotification(error.message, 'error');
        console.error(error);
    }
}

function showDeleteModal(id, name) {
    deleteId = id;
    deleteContractorNameSpan.textContent = name;
    deleteModal.style.display = 'flex';
}

function closeDeleteModal() {
    deleteModal.style.display = 'none';
    deleteId = null;
}

async function confirmDelete() {
    if (!deleteId) return;

    try {
        const response = await fetch(`${API_BASE}/contractor/${deleteId}`, {
            method: 'DELETE',
            headers
        });
        if (!response.ok && response.status !== 204) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || 'Ошибка удаления');
        }
        closeDeleteModal();
        await loadContractors(searchInput.value.trim());
        showNotification('Подрядчик удалён');
    } catch (error) {
        showNotification(error.message, 'error');
        console.error(error);
    }
}

function viewTeam(contractorId) {
    sessionStorage.setItem('filter_contractor_id', contractorId);
    location.hash = 'users';
}

function renderPagination(total) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;

    const pageSizeOptions = [10, 20, 50, 100];
    let html = '<div class="pagination-footer">';

    html += '<div class="page-size-selector">';
    html += '<div class="page-size-select-wrapper">';
    html += '<select class="page-size-select" id="pageSizeSelect">';
    pageSizeOptions.forEach(size => {
        html += `<option value="${size}"${size === pageSize ? ' selected' : ''}>${size}</option>`;
    });
    html += '</select>';
    html += '<i class="fas fa-chevron-down page-size-arrow"></i>';
    html += '</div>';
    html += '</div>';

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

    container.querySelectorAll('.pagination-btn:not(:disabled)').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page);
            if (page && page !== currentPage) {
                loadContractors(searchInput.value.trim(), page);
            }
        });
    });

    const pageSizeSelect = container.querySelector('#pageSizeSelect');
    if (pageSizeSelect) {
        pageSizeSelect.addEventListener('change', () => {
            const size = parseInt(pageSizeSelect.value);
            if (size !== pageSize) {
                pageSize = size;
                currentPage = 1;
                loadContractors(searchInput.value.trim());
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

function showNotification(message, type = 'success') {
    notification.className = 'notification';
    if (type === 'error') {
        notification.classList.add('error');
        notification.querySelector('i').className = 'fas fa-exclamation-circle notification-icon';
    } else if (type === 'info') {
        notification.querySelector('i').className = 'fas fa-info-circle notification-icon';
    } else {
        notification.querySelector('i').className = 'fas fa-check-circle notification-icon';
    }
    notificationMessage.textContent = message;
    notification.style.display = 'flex';

    setTimeout(() => {
        notification.style.display = 'none';
    }, 3000);
}

export function initContractorsModule() {
    // Сброс состояния при повторной навигации
    currentPage = 1;
    totalPages = 1;
    pageSize = 20;
    deleteId = null;

    // Получаем DOM-элементы из свежего фрагмента
    contractorsListEl = document.getElementById('contractorsList');
    emptyStateEl = document.getElementById('emptyState');
    searchInput = document.getElementById('searchInput');
    addBtn = document.getElementById('addContractorBtn');
    modalOverlay = document.getElementById('contractorModal');
    modalTitle = document.getElementById('modalTitle');
    contractorNameInput = document.getElementById('contractorName');
    contractorIdInput = document.getElementById('contractorId');
    nameError = document.getElementById('nameError');
    closeModalBtn = document.getElementById('closeModalBtn');
    cancelModalBtn = document.getElementById('cancelModalBtn');
    saveBtn = document.getElementById('saveContractorBtn');
    deleteModal = document.getElementById('deleteModal');
    deleteContractorNameSpan = document.getElementById('deleteContractorName');
    closeDeleteModalBtn = document.getElementById('closeDeleteModalBtn');
    cancelDeleteBtn = document.getElementById('cancelDeleteBtn');
    confirmDeleteBtn = document.getElementById('confirmDeleteBtn');
    notification = document.getElementById('notification');
    notificationMessage = document.getElementById('notificationMessage');

    loadContractors();

    contractorsListEl.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        const { action, id, name } = btn.dataset;
        if (action === 'edit') editContractor(id, name);
        else if (action === 'delete') showDeleteModal(id, name);
        else if (action === 'team') viewTeam(id);
    });

    let searchTimeout;
    searchInput.addEventListener('input', () => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(filterContractors, 300);
    });

    addBtn.addEventListener('click', openAddModal);

    closeModalBtn.addEventListener('click', closeModal);
    cancelModalBtn.addEventListener('click', closeModal);
    modalOverlay.addEventListener('click', (e) => {
        if (e.target === modalOverlay) closeModal();
    });

    saveBtn.addEventListener('click', saveContractor);

    closeDeleteModalBtn.addEventListener('click', closeDeleteModal);
    cancelDeleteBtn.addEventListener('click', closeDeleteModal);
    deleteModal.addEventListener('click', (e) => {
        if (e.target === deleteModal) closeDeleteModal();
    });
    confirmDeleteBtn.addEventListener('click', confirmDelete);
}
