// users.js - Модуль управления пользователями

// Глобальные переменные
let usersData = [];
let contractorsData = [];
let currentEditUserId = null;
let currentAccessUserId = null;
let originalUserData = null;
let originalTwoFaEnabled = false;
let pendingConfirmCallback = null;
let searchDebounceTimer = null;
let orgSearchDebounceTimer = null;
let currentContractorId = null;
let _accessesCache = {};
let _lockerNames = {};
let currentFilters = { role: null, state: null };
let currentPage = 1;
let totalPages = 1;
let pageSize = 100;

const FILTER_LABELS = {
    role: { worker: 'Рабочий', operator: 'Оператор', administrator: 'Администратор' },
    state: { active: 'Активный', deactivated: 'Отключён' }
};

const USERS_API = {
    GET_USERS: '/api/v1/users',
    //GET_USERS_SEARCH: (q) => `/api/v1/users?q=${encodeURIComponent(q)}`,
    GET_USERS_FILTERED: (params) => `/api/v1/users?${new URLSearchParams(params).toString()}`,
    CREATE_USER: '/api/v1/users',
    //UPDATE_USER: (id) => `/api/v1/users/${id}`,
    PATCH_USER: (id) => `/api/v1/user/${id}`,
    DEACTIVATE_USER: (id) => `/api/v1/user/${id}`,
    REACTIVATE_USER: (id) => `/api/v1/user/${id}/reactivate`,
    ENABLE_2FA: (id) => `/api/v1/user/${id}/enable-2fa`,
    DISABLE_2FA: (id) => `/api/v1/user/${id}/disable-2fa`,
    GET_CONTRACTORS: '/api/v1/contractors',
    GET_USER_ACCESSES: (userId, includeInactive = false) => `/api/v1/user/${userId}/accesses${includeInactive ? '?include_inactive=true' : ''}`,
    CREATE_ACCESS: (userId) => `/api/v1/user/${userId}/accesses`,
    DEACTIVATE_ACCESS: (accessId) => `/api/v1/access/${accessId}`,
    REACTIVATE_ACCESS: (accessId) => `/api/v1/access/${accessId}/reactivate`,
    PATCH_ACCESS: (accessId) => `/api/v1/access/${accessId}`,
    LOCKERS_BATCH: '/api/v1/lockers/batch',
    SEARCH_LOCKERS: (q) => `/api/v1/lockers?q=${encodeURIComponent(q)}&limit=10`
};

const API_HEADERS = {
    'Content-Type': 'application/json'
};

// API отдаёт lockers как массив строк-UUID, а не объектов {uuid}
function normalizeAccess(access) {
    if (Array.isArray(access.lockers)) {
        access.lockers = access.lockers.map(l => typeof l === 'string' ? { uuid: l } : l);
    }
    return access;
}

const ROLE_LABELS = {
    worker: 'Рабочий',
    administrator: 'Администратор',
    operator: 'Оператор'
};

export function initUsersModule() {
    setupEventListeners();
    const stored = sessionStorage.getItem('filter_contractor_id');
    if (stored) {
        sessionStorage.removeItem('filter_contractor_id');
        currentContractorId = stored;
    } else {
        currentContractorId = null;
    }
    loadUsers();
}


function setupEventListeners() {
    setupOrgSearch();
    document.getElementById('addUserBtn').addEventListener('click', showAddUserModal);
    document.getElementById('closeModal').addEventListener('click', closeUserModal);
    document.getElementById('cancelBtn').addEventListener('click', closeUserModal);
    document.getElementById('saveUserBtn').addEventListener('click', saveUser);

    // Поиск с дебаунсом — запрос уходит на сервер
    document.getElementById('searchUsers').addEventListener('input', function() {
        const clearBtn = document.getElementById('clearSearch');
        clearBtn.style.display = this.value ? 'flex' : 'none';
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => { currentPage = 1; loadUsers(this.value.trim()); }, 400);
    });

    document.getElementById('clearSearch').addEventListener('click', function() {
        const input = document.getElementById('searchUsers');
        input.value = '';
        this.style.display = 'none';
        loadUsers();
    });

    document.getElementById('filterRole').addEventListener('change', function() {
        applyFilter('role', this.value);
    });
    document.getElementById('filterState').addEventListener('change', function() {
        applyFilter('state', this.value);
    });

    document.getElementById('closeAccessModal').addEventListener('click', closeAccessModal);
    document.getElementById('cancelAccessBtn').addEventListener('click', closeAccessModal);

    document.getElementById('includeInactiveAccesses').addEventListener('change', () => {
        if (currentAccessUserId) showAccessModal(currentAccessUserId);
    });

    document.getElementById('addAccessBtn').addEventListener('click', showNewAccessForm);
    document.getElementById('cancelNewAccessBtn').addEventListener('click', hideNewAccessForm);
    document.getElementById('saveNewAccessBtn').addEventListener('click', () => saveNewAccess(currentAccessUserId));

    document.getElementById('newAccessFullAccess').addEventListener('change', function() {
        document.getElementById('newAccessLockersSection').classList.toggle('hidden', this.checked);
    });

    document.getElementById('newAccessForm').addEventListener('click', function(e) {
        const removeBtn = e.target.closest('.remove-locker-tag');
        if (removeBtn) removeBtn.closest('.access-locker-tag').remove();
    });

    // Автокомплит-поиск шкафа в модалке допусков (форма выдачи + строки редактирования)
    const accessModal = document.getElementById('accessModal');
    accessModal.addEventListener('input', function(e) {
        const input = e.target.closest('.access-locker-input');
        if (!input) return;
        clearTimeout(input._lockerSuggestTimer);
        input._lockerSuggestTimer = setTimeout(() => fetchAccessLockerSuggestions(input), 300);
    });
    accessModal.addEventListener('click', function(e) {
        const suggestion = e.target.closest('.access-locker-suggestion');
        if (suggestion) { addLockerTagFromSuggestion(suggestion); return; }
        if (!e.target.closest('.access-locker-input-wrap')) {
            hideAllAccessLockerSuggestions();
        }
    });

    // Делегирование событий для списка допусков
    const accessesList = document.getElementById('accessesList');
    accessesList.addEventListener('change', function(e) {
        const toggle = e.target.closest('.access-toggle-input');
        if (toggle) {
            toggleAccessActive(currentAccessUserId, parseInt(toggle.dataset.accessId), toggle.checked);
        }
    });
    accessesList.addEventListener('click', function(e) {
        const editBtn = e.target.closest('.btn-access-edit');
        if (editBtn) { toggleAccessEditForm(parseInt(editBtn.dataset.accessId)); return; }

        const cancelBtn = e.target.closest('.btn-access-cancel');
        if (cancelBtn) { toggleAccessEditForm(parseInt(cancelBtn.dataset.accessId), false); return; }

        const saveBtn = e.target.closest('.btn-access-save');
        if (saveBtn) { saveAccessEdit(currentAccessUserId, parseInt(saveBtn.dataset.accessId)); return; }

        const removeBtn = e.target.closest('.remove-locker-tag');
        if (removeBtn) { removeBtn.closest('.access-locker-tag').remove(); return; }
    });

    ['userName', 'userPhone', 'userPassword'].forEach(id => {
        document.getElementById(id).addEventListener('input', function() {
            this.classList.remove('error');
            const err = document.getElementById(id + 'Error');
            if (err) err.textContent = '';
        });
    });

    document.getElementById('togglePassword').addEventListener('click', function() {
        const input = document.getElementById('userPassword');
        const icon = document.getElementById('togglePasswordIcon');
        const isHidden = input.type === 'password';
        input.type = isHidden ? 'text' : 'password';
        icon.className = isHidden ? 'fas fa-eye-slash' : 'fas fa-eye';
        this.title = isHidden ? 'Скрыть пароль' : 'Показать пароль';
    });

    document.getElementById('generatePassword').addEventListener('click', function() {
        const password = generatePassword();
        const input = document.getElementById('userPassword');
        input.value = password;
        input.type = 'text';
        input.classList.remove('error');
        document.getElementById('userPasswordError').textContent = '';
        document.getElementById('togglePasswordIcon').className = 'fas fa-eye-slash';
        document.getElementById('togglePassword').title = 'Скрыть пароль';
        input.focus();
    });

    document.getElementById('getMaxInviteLinkBtn').addEventListener('click', getMaxInviteLink);

    document.getElementById('copyMaxInviteLinkBtn').addEventListener('click', function() {
        copyToClipboard(document.getElementById('maxInviteLink').value);
    });

    document.getElementById('closeConfirmModal').addEventListener('click', closeConfirmModal);
    document.getElementById('confirmModalCancel').addEventListener('click', closeConfirmModal);
    document.getElementById('confirmModalOk').addEventListener('click', function() {
        if (pendingConfirmCallback) {
            pendingConfirmCallback();
            pendingConfirmCallback = null;
        }
        closeConfirmModal();
    });
}

function setupOrgField(selectedId = null, selectedName = '') {
    document.getElementById('userOrganizationSearch').value = selectedName;
    document.getElementById('userOrganization').value = selectedId || '';
    document.getElementById('orgDropdown').classList.add('hidden');
}

function setupOrgSearch() {
    const searchInput = document.getElementById('userOrganizationSearch');
    const dropdown = document.getElementById('orgDropdown');

    searchInput.addEventListener('focus', function() {
        searchContractors(this.value.trim());
    });

    searchInput.addEventListener('input', function() {
        document.getElementById('userOrganization').value = '';
        clearTimeout(orgSearchDebounceTimer);
        orgSearchDebounceTimer = setTimeout(() => searchContractors(this.value.trim()), 300);
    });

    document.addEventListener('click', function(e) {
        const combobox = document.getElementById('orgCombobox');
        if (!combobox) return;
        if (!combobox.contains(e.target)) {
            dropdown.classList.add('hidden');
        }
    });
}

async function searchContractors(query) {
    const dropdown = document.getElementById('orgDropdown');
    dropdown.innerHTML = '<div class="org-dropdown-loading"><i class="fas fa-spinner fa-spin"></i> Загрузка...</div>';
    dropdown.classList.remove('hidden');

    try {
        const url = query
            ? `${USERS_API.GET_CONTRACTORS}?q=${encodeURIComponent(query)}`
            : USERS_API.GET_CONTRACTORS;
        const response = await fetch(url, { headers: API_HEADERS });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        const items = data.items || data;

        dropdown.innerHTML = '';
        if (items.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'org-dropdown-empty';
            empty.textContent = 'Ничего не найдено';
            dropdown.appendChild(empty);
        } else {
            items.forEach(c => {
                const item = document.createElement('div');
                item.className = 'org-dropdown-item';
                item.textContent = c.name;
                item.addEventListener('click', () => {
                    document.getElementById('userOrganizationSearch').value = c.name;
                    document.getElementById('userOrganization').value = c.id;
                    dropdown.classList.add('hidden');
                });
                dropdown.appendChild(item);
            });
        }
    } catch (e) {
        dropdown.innerHTML = '<div class="org-dropdown-empty">Ошибка загрузки</div>';
    }
}

// Загрузка пользователей с сервера
async function loadUsers(search = '', page = null) {
    if (page !== null) currentPage = page;

    try {
        showLoader();

        const params = {};
        if (search) params.q = search;
        if (currentContractorId) params.contractor_id = currentContractorId;
        if (currentFilters.role) params.role = currentFilters.role;
        if (currentFilters.state) params.state = currentFilters.state;
        params.limit = pageSize;
        params.offset = (currentPage - 1) * pageSize;

        const url = USERS_API.GET_USERS_FILTERED(params);
        const response = await fetch(url, { headers: API_HEADERS });

        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        usersData = data.items || [];
        const total = data.total || 0;
        totalPages = Math.max(1, Math.ceil(total / pageSize));

        displayUsers(usersData);
        renderPagination(total);

    } catch (error) {
        console.error('Ошибка загрузки пользователей:', error);
        showNotification('Ошибка загрузки данных', 'error');
    } finally {
        hideLoader();
    }
}

// Отображение пользователей в таблице
function displayUsers(users) {
    const usersList = document.getElementById('usersList');

    if (users.length === 0) {
        usersList.innerHTML = `
            <tr>
                <td colspan="6">
                    <div class="empty-state">
                        <i class="fas fa-users"></i>
                        <h3>Нет пользователей</h3>
                        <p>Добавьте первого пользователя, нажав на кнопку "Добавить"</p>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    usersList.innerHTML = users.map(user => createUserRow(user)).join('');

    document.querySelectorAll('.btn-edit').forEach(btn => {
        btn.addEventListener('click', function() {
            editUser(parseInt(this.dataset.userId));
        });
    });

    document.querySelectorAll('.btn-disable').forEach(btn => {
        btn.addEventListener('click', function() {
            toggleUserStatus(parseInt(this.dataset.userId));
        });
    });

    document.querySelectorAll('.btn-access').forEach(btn => {
        btn.addEventListener('click', function() {
            showAccessModal(parseInt(this.dataset.userId));
        });
    });

    document.querySelectorAll('.tag.more').forEach(tag => {
        tag.addEventListener('mouseenter', showTagsTooltip);
        tag.addEventListener('mouseleave', hideTagsTooltip);
    });
}

// Создание строки пользователя
function createUserRow(user) {
    const isExpired = user.expire_date && parseDateOnly(user.expire_date) < new Date();
    const isActive = user.state === 'active';
    const status = !isActive ? 'inactive' : isExpired ? 'expired' : 'active';
    const statusText = !isActive ? 'Отключен' : isExpired ? 'Просрочен' : 'Активен';

    const expiryDate = user.expire_date ? formatISODate(user.expire_date) : 'Бессрочно';
    const roleLabel = ROLE_LABELS[user.role] || user.role || '—';
    const tagsHtml = renderTags(user.tags || [], 3);

    return `
        <tr class="${(!isActive || isExpired) ? 'expired' : ''}">
            <td class="id-cell">${user.id}</td>
            <td>
                <div class="user-cell">
                    <div class="user-avatar">
                        <i class="fas fa-user"></i>
                    </div>
                    <div class="user-info">
                        <span class="user-name">${escapeHtml(user.full_name)}</span>
                        <span class="user-position">${escapeHtml(roleLabel)}</span>
                    </div>
                </div>
            </td>
            <td>${escapeHtml(user.contractor_name || '—')}</td>
            <td>${expiryDate}</td>
            <td>
                <span class="status-badge status-${status}">${statusText}</span>
            </td>
            <td>
                <div class="actions-cell">
                    <button class="btn-action btn-edit" data-user-id="${user.id}" title="Редактировать">
                        <i class="fas fa-edit"></i>
                    </button>
                    <button class="btn-action btn-access" data-user-id="${user.id}" title="Доступы">
                        <i class="fas fa-key"></i>
                    </button>
                    <button class="btn-action btn-disable" data-user-id="${user.id}"
                            title="${!isActive || isExpired ? 'Включить' : 'Отключить'}">
                        <i class="fas ${!isActive || isExpired ? 'fa-user-check' : 'fa-user-slash'}"></i>
                    </button>
                </div>
            </td>
        </tr>
    `;
}

// Рендер тегов с ограничением
function renderTags(tags, maxVisible = 3) {
    if (!tags || tags.length === 0) {
        return '<span class="text-muted">Нет тегов</span>';
    }

    const visibleTags = tags.slice(0, maxVisible);
    const hiddenTags = tags.slice(maxVisible);

    let html = visibleTags.map(tag =>
        `<span class="tag">${escapeHtml(tag)}</span>`
    ).join('');

    if (hiddenTags.length > 0) {
        html += `<span class="tag more" title="Ещё ${hiddenTags.length} тегов">+${hiddenTags.length}</span>`;
    }

    return html;
}

// Фильтры
function applyFilter(key, value) {
    currentFilters[key] = value || null;
    currentPage = 1;
    const select = document.getElementById(key === 'role' ? 'filterRole' : 'filterState');
    if (select) select.classList.toggle('active', !!value);
    renderFilterChips();
    const searchTerm = document.getElementById('searchUsers').value.trim();
    loadUsers(searchTerm);
}

function removeFilter(key) {
    currentFilters[key] = null;
    currentPage = 1;
    const select = document.getElementById(key === 'role' ? 'filterRole' : 'filterState');
    if (select) { select.value = ''; select.classList.remove('active'); }
    renderFilterChips();
    const searchTerm = document.getElementById('searchUsers').value.trim();
    loadUsers(searchTerm);
}

function renderFilterChips() {
    const container = document.getElementById('filterChips');
    container.innerHTML = Object.entries(currentFilters)
        .filter(([, v]) => v)
        .map(([key, value]) => `
            <span class="filter-chip">
                ${escapeHtml(FILTER_LABELS[key][value])}
                <button type="button" class="filter-chip-remove" data-key="${key}">
                    <i class="fas fa-times"></i>
                </button>
            </span>
        `).join('');

    container.querySelectorAll('.filter-chip-remove').forEach(btn => {
        btn.addEventListener('click', () => removeFilter(btn.dataset.key));
    });
}

// Тултип для скрытых тегов
function showTagsTooltip(event) {
    const tagElement = event.target;
    const userId = tagElement.closest('.tags-container').dataset.userId;
    const user = usersData.find(u => u.id == userId);

    if (user && user.tags && user.tags.length > 3) {
        const hiddenTags = user.tags.slice(3);
        const tooltip = document.getElementById('tagsTooltip');

        tooltip.innerHTML = `
            <div class="tooltip-tags">
                ${hiddenTags.map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join('')}
            </div>
        `;

        const rect = tagElement.getBoundingClientRect();
        tooltip.style.left = `${rect.left}px`;
        tooltip.style.top = `${rect.bottom + 5}px`;
        tooltip.style.display = 'block';
    }
}

function hideTagsTooltip() {
    document.getElementById('tagsTooltip').style.display = 'none';
}


function resetMaxInviteLink() {
    document.getElementById('maxInviteLink').value = '';
    document.getElementById('maxInviteLinkRow').classList.add('hidden');
    document.getElementById('getMaxInviteLinkBtn').classList.remove('hidden');
}

function setCreateMode() {
    document.getElementById('passwordGroup').style.display = '';
    document.getElementById('userPassword').placeholder = 'Минимум 6 символов';
    document.getElementById('twoFaSection').style.display = 'none';
}

function setEditMode() {
    document.getElementById('passwordGroup').style.display = '';
    document.getElementById('userPassword').placeholder = 'Оставьте пустым, чтобы не менять';
    document.getElementById('twoFaSection').style.display = '';
    resetMaxInviteLink();
}

// Ссылка для привязки MAX (включает 2FA)
async function getMaxInviteLink() {
    if (!currentEditUserId) return;

    const btn = document.getElementById('getMaxInviteLinkBtn');
    btn.disabled = true;
    try {
        const response = await fetch(USERS_API.ENABLE_2FA(currentEditUserId), {
            method: 'POST',
            headers: API_HEADERS
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        document.getElementById('userTwoFa').checked = true;
        originalTwoFaEnabled = true;

        document.getElementById('maxInviteLink').value = data.max_invite_link || '';
        document.getElementById('maxInviteLinkRow').classList.remove('hidden');
        btn.classList.add('hidden');
    } catch (error) {
        console.error('Ошибка получения ссылки MAX:', error);
        showNotification('Не удалось получить ссылку для привязки MAX', 'error');
    } finally {
        btn.disabled = false;
    }
}

// Модальное окно добавления
function showAddUserModal() {
    currentEditUserId = null;
    document.getElementById('modalTitle').textContent = 'Добавить пользователя';
    document.getElementById('userForm').reset();
    setupOrgField(null, '');
    setCreateMode();
    document.getElementById('userModal').style.display = 'flex';
}

// Редактирование пользователя
function editUser(userId) {
    const user = usersData.find(u => u.id === userId);
    if (!user) return;

    currentEditUserId = userId;
    const rawOriginalPhone = user.phone || '';
    originalUserData = {
        full_name: user.full_name || '',
        role: user.role || 'worker',
        phone: normalizePhone(rawOriginalPhone) ?? rawOriginalPhone,
        contractor_id: user.contractor_id ?? null
    };

    document.getElementById('modalTitle').textContent = 'Редактировать пользователя';

    document.getElementById('userId').value = user.id;
    document.getElementById('userName').value = user.full_name;
    document.getElementById('userRole').value = user.role || 'worker';
    document.getElementById('userPhone').value = user.phone || '';
    setupOrgField(user.contractor_id ?? null, user.contractor_name || '');

    originalTwoFaEnabled = !!user.is_2fa_enabled;
    document.getElementById('userTwoFa').checked = originalTwoFaEnabled;

    setEditMode();
    document.getElementById('userModal').style.display = 'flex';
}

// Сохранение пользователя
async function saveUser() {
    const isEdit = !!currentEditUserId;
    let hasErrors = false;

    const name = document.getElementById('userName').value.trim();
    if (!name) {
        showValidationError('userName', 'ФИО обязательно для заполнения');
        hasErrors = true;
    }

    const rawPhone = document.getElementById('userPhone').value.trim();
    if (!rawPhone) {
        showValidationError('userPhone', 'Телефон обязателен для заполнения');
        hasErrors = true;
    } else if (normalizePhone(rawPhone) === null) {
        showValidationError('userPhone', 'Формат: +79991234567 или 89991234567');
        hasErrors = true;
    }

    if (!isEdit) {
        const password = document.getElementById('userPassword').value;
        if (!password) {
            showValidationError('userPassword', 'Пароль обязателен');
            hasErrors = true;
        }
    }

    if (hasErrors) return;

    const userData = isEdit ? collectEditFormData() : collectCreateFormData();
    const twoFaEnabled = document.getElementById('userTwoFa').checked;
    const twoFaChanged = isEdit && twoFaEnabled !== originalTwoFaEnabled;

    if (isEdit && Object.keys(userData).length === 0 && !twoFaChanged) {
        closeUserModal();
        return;
    }

    try {
        let response;
        if (isEdit) {
            if (Object.keys(userData).length > 0) {
                response = await fetch(USERS_API.PATCH_USER(currentEditUserId), {
                    method: 'PATCH',
                    headers: { ...API_HEADERS, 'Content-Type': 'application/json' },
                    body: JSON.stringify(userData)
                });
            }
        } else {
            response = await fetch(USERS_API.CREATE_USER, {
                method: 'POST',
                headers: { ...API_HEADERS, 'Content-Type': 'application/json' },
                body: JSON.stringify(userData)
            });
        }

        if (response && response.status === 422) {
            const err = await response.json();
            if (err.fields) {
                Object.entries(err.fields).forEach(([field, msg]) => {
                    const fieldMap = { name: 'userName', full_name: 'userName', password: 'userPassword' };
                    showValidationError(fieldMap[field] || field, msg);
                });
            }
            return;
        }

        if (response && !response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        if (twoFaChanged) {
            const twoFaUrl = twoFaEnabled
                ? USERS_API.ENABLE_2FA(currentEditUserId)
                : USERS_API.DISABLE_2FA(currentEditUserId);
            const twoFaResponse = await fetch(twoFaUrl, {
                method: 'POST',
                headers: API_HEADERS
            });

            if (!twoFaResponse.ok) {
                throw new Error(`HTTP ${twoFaResponse.status}`);
            }
        }

        const searchTerm = document.getElementById('searchUsers').value.trim();
        await loadUsers(searchTerm);

        if (isEdit) {
            closeUserModal();
            showNotification('Пользователь обновлён', 'success');
        } else {
            closeUserModal();
            showNotification('Пользователь создан', 'success');
        }

    } catch (error) {
        console.error('Ошибка сохранения:', error);
        showNotification('Ошибка сохранения данных', 'error');
    }
}

// Данные для создания (POST /api/v1/users)
function collectCreateFormData() {
    const contractorId = document.getElementById('userOrganization').value;
    const rawPhone = document.getElementById('userPhone').value.trim();
    return {
        full_name: document.getElementById('userName').value.trim(),
        phone: normalizePhone(rawPhone) || rawPhone,
        password: document.getElementById('userPassword').value,
        role: document.getElementById('userRole').value,
        contractor_id: contractorId ? parseInt(contractorId) : null
    };
}

// Данные для редактирования — только изменённые поля
function collectEditFormData() {
    const contractorId = document.getElementById('userOrganization').value;
    const rawPhone = document.getElementById('userPhone').value.trim();
    const current = {
        full_name: document.getElementById('userName').value.trim(),
        role: document.getElementById('userRole').value,
        phone: normalizePhone(rawPhone) || rawPhone,
        contractor_id: contractorId ? parseInt(contractorId) : null
    };

    const diff = {};
    for (const key of Object.keys(current)) {
        if (current[key] !== originalUserData[key]) {
            diff[key] = current[key];
        }
    }

    const password = document.getElementById('userPassword').value;
    if (password) diff.password = password;

    return diff;
}

// Смена статуса пользователя
function toggleUserStatus(userId) {
    const user = usersData.find(u => u.id === userId);
    if (!user) return;

    const isExpired = user.expire_date && parseDateOnly(user.expire_date) < new Date();
    const willEnable = user.state !== 'active' || isExpired;

    showConfirmModal({
        title: willEnable ? 'Включить пользователя' : 'Отключить пользователя',
        text: user.full_name,
        subtext: willEnable
            ? 'Пользователь получит доступ к системе.'
            : 'Доступ пользователя будет заблокирован.',
        iconClass: willEnable ? 'icon-success' : 'icon-danger',
        iconFa: willEnable ? 'fa-user-check' : 'fa-user-slash',
        okClass: willEnable ? 'btn-success' : 'btn-danger',
        okText: willEnable ? 'Включить' : 'Отключить',
        showDateField: false,
        onConfirm: async () => {
            try {
                let response;
                if (willEnable) {
                    response = await fetch(USERS_API.REACTIVATE_USER(userId), {
                        method: 'POST',
                        headers: API_HEADERS
                    });
                } else {
                    response = await fetch(USERS_API.DEACTIVATE_USER(userId), {
                        method: 'DELETE',
                        headers: API_HEADERS
                    });
                }

                if (!response.ok) throw new Error(`HTTP ${response.status}`);

                const idx = usersData.findIndex(u => u.id === userId);
                if (response.status === 204) {
                    if (idx !== -1) usersData[idx].state = 'inactive';
                } else {
                    const updated = await response.json();
                    if (idx !== -1) {
                        usersData[idx].state = updated.state;
                        usersData[idx].expire_date = updated.expire_date;
                    }
                }
                displayUsers(usersData);
                showNotification(
                    willEnable ? 'Пользователь включён' : 'Пользователь отключён',
                    'success'
                );
            } catch (error) {
                console.error('Ошибка смены статуса:', error);
                showNotification('Ошибка смены статуса', 'error');
            }
        }
    });
}

// Модалка допусков
async function showAccessModal(userId) {
    currentAccessUserId = userId;

    const loadingEl = document.getElementById('accessesLoading');
    const listEl = document.getElementById('accessesList');
    const emptyEl = document.getElementById('accessesEmpty');

    listEl.innerHTML = '';
    emptyEl.classList.add('hidden');
    loadingEl.style.display = 'flex';

    document.getElementById('accessModal').style.display = 'flex';

    try {
        const includeInactive = document.getElementById('includeInactiveAccesses')?.checked || false;
        const response = await fetch(USERS_API.GET_USER_ACCESSES(userId, includeInactive), { headers: API_HEADERS });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const data = await response.json();
        const items = data.items || [];

        loadingEl.style.display = 'none';

        if (items.length === 0) {
            emptyEl.classList.remove('hidden');
        } else {
            _accessesCache = {};
            items.forEach(a => { _accessesCache[a.id] = normalizeAccess(a); });

            // Собираем все уникальные UUID из допусков и получаем их имена
            const allUuids = [...new Set(
                items.flatMap(a => a.full_access ? [] : (a.lockers || []).map(l => l.uuid))
            )];
            _lockerNames = {};
            if (allUuids.length > 0) {
                try {
                    const batchResp = await fetch(USERS_API.LOCKERS_BATCH, {
                        method: 'POST',
                        headers: { ...API_HEADERS, 'Content-Type': 'application/json' },
                        body: JSON.stringify({ locker_ids: allUuids, view: 'brief' })
                    });
                    if (batchResp.ok) {
                        const batchData = await batchResp.json();
                        _lockerNames = {};
                        Object.entries(batchData.items || {}).forEach(([uuid, info]) => {
                            _lockerNames[uuid] = info.inventory_number || uuid;
                        });
                    }
                } catch (e) {
                    console.warn('Не удалось загрузить имена шкафов:', e);
                }
            }

            listEl.innerHTML = items.map(renderAccessItem).join('');
        }
    } catch (error) {
        console.error('Ошибка загрузки допусков:', error);
        loadingEl.style.display = 'none';
        showNotification('Ошибка загрузки допусков', 'error');
    }
}

// ─── Автокомплит поиска шкафа в модалке допусков ────────────────────────────

async function fetchAccessLockerSuggestions(input) {
    const box = input.closest('.access-locker-input-wrap')?.querySelector('.access-locker-suggestions');
    if (!box) return;
    const term = input.value.trim();
    if (!term) { hideAccessLockerSuggestions(box); return; }
    try {
        const resp = await fetch(USERS_API.SEARCH_LOCKERS(term), { headers: API_HEADERS });
        if (!resp.ok) { hideAccessLockerSuggestions(box); return; }
        const data = await resp.json();
        renderAccessLockerSuggestions(box, data.items || []);
    } catch (e) {
        console.warn('Ошибка загрузки подсказок шкафов:', e);
        hideAccessLockerSuggestions(box);
    }
}

function renderAccessLockerSuggestions(box, items) {
    if (!items.length) {
        box.innerHTML = '<div class="access-locker-suggestion-empty">Ничего не найдено</div>';
        box.style.display = 'block';
        return;
    }
    box.innerHTML = items.map(item => `
        <button type="button" class="access-locker-suggestion"
                data-uuid="${escapeHtml(item.id)}"
                data-label="${escapeHtml(item.inventory_number || item.id)}">
            <span class="access-locker-suggestion-number">${escapeHtml(item.inventory_number || '—')}</span>
            <span class="access-locker-suggestion-address">${escapeHtml(item.location_address || '')}</span>
        </button>`).join('');
    box.style.display = 'block';
}

function hideAccessLockerSuggestions(box) {
    if (box) { box.style.display = 'none'; box.innerHTML = ''; }
}

function hideAllAccessLockerSuggestions() {
    document.querySelectorAll('.access-locker-suggestions').forEach(b => {
        b.style.display = 'none';
        b.innerHTML = '';
    });
}

function addLockerTagFromSuggestion(suggestionBtn) {
    const section = suggestionBtn.closest('.access-edit-lockers');
    if (!section) return;
    const input = section.querySelector('.access-locker-input');
    const container = section.querySelector('.access-locker-tags');
    if (!input || !container) return;

    const uuid = suggestionBtn.dataset.uuid;
    const label = suggestionBtn.dataset.label || uuid;
    const existing = Array.from(container.querySelectorAll('.access-locker-tag')).map(t => t.dataset.uuid);

    if (existing.includes(uuid)) {
        showNotification('Этот шкаф уже добавлен', 'warning');
    } else {
        _lockerNames[uuid] = label;
        const tag = document.createElement('div');
        tag.className = 'access-locker-tag';
        tag.dataset.uuid = uuid;
        tag.innerHTML = `<span title="${escapeHtml(uuid)}">${escapeHtml(label)}</span><button type="button" class="remove-locker-tag"><i class="fas fa-times"></i></button>`;
        container.appendChild(tag);
    }

    input.value = '';
    hideAccessLockerSuggestions(section.querySelector('.access-locker-suggestions'));
}

function renderAccessItem(access) {
    const startDate = access.start_at ? formatAccessDateTime(access.start_at) : '—';
    const endDate = access.end_at ? formatAccessDateTime(access.end_at) : 'Бессрочно';
    const startVal = access.start_at ? access.start_at.slice(0, 16) : '';
    const endVal = access.end_at ? access.end_at.slice(0, 16) : '';

    let lockersHtml;
    if (access.full_access) {
        lockersHtml = '<span class="access-locker-all">all</span>';
    } else if (access.lockers && access.lockers.length > 0) {
        lockersHtml = access.lockers.map(l =>
            `<span class="access-locker-uuid" title="${escapeHtml(l.uuid)}">${escapeHtml(_lockerNames[l.uuid] || l.uuid)}</span>`
        ).join('');
    } else {
        lockersHtml = '<span class="access-lockers-none">—</span>';
    }

    const lockersEditHtml = access.full_access ? '' : `
        <div class="access-edit-lockers">
            <label class="access-edit-label">Шкафы</label>
            <div class="access-locker-tags" id="accessLockerTags-${access.id}">
                ${(access.lockers || []).map(l => `
                    <div class="access-locker-tag" data-uuid="${escapeHtml(l.uuid)}">
                        <span title="${escapeHtml(l.uuid)}">${escapeHtml(_lockerNames[l.uuid] || l.uuid)}</span>
                        <button type="button" class="remove-locker-tag">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                `).join('')}
            </div>
            <div class="access-locker-input-wrap">
                <input type="text" class="access-locker-input"
                       id="accessLockerInput-${access.id}"
                       placeholder="Поиск по инвентарному номеру или адресу" autocomplete="off">
                <div class="access-locker-suggestions" style="display:none;"></div>
            </div>
        </div>
    `;

    return `
        <div class="access-item${access.is_active ? '' : ' access-item--inactive'}" data-access-id="${access.id}">
            <div class="access-item-header">
                <span class="access-item-id">Допуск #${access.id}</span>
                <div class="access-item-controls">
                    <button class="btn-access-edit" data-access-id="${access.id}" title="Редактировать">
                        <i class="fas fa-pencil-alt"></i>
                    </button>
                    <label class="access-toggle-label" title="${access.is_active ? 'Активен' : 'Неактивен'}">
                        <input type="checkbox" class="access-toggle-input"
                               data-access-id="${access.id}"
                               ${access.is_active ? 'checked' : ''}>
                        <span class="access-toggle-track"></span>
                    </label>
                </div>
            </div>
            <div class="access-item-view">
                <div class="access-item-dates">
                    <div class="access-date-field">
                        <span class="access-date-label">С</span>
                        <span class="access-date-value">${startDate}</span>
                    </div>
                    <div class="access-date-sep">—</div>
                    <div class="access-date-field">
                        <span class="access-date-label">По</span>
                        <span class="access-date-value">${endDate}</span>
                    </div>
                </div>
                <div class="access-item-lockers">
                    <span class="access-lockers-label">Шкафы:</span>
                    <div class="access-lockers-list">${lockersHtml}</div>
                </div>
            </div>
            <div class="access-edit-form hidden" data-access-id="${access.id}">
                <div class="access-edit-dates">
                    <div class="access-edit-field">
                        <label class="access-edit-label">С</label>
                        <input type="datetime-local" class="access-edit-start" value="${startVal}">
                    </div>
                    <div class="access-edit-field">
                        <label class="access-edit-label">По</label>
                        <input type="datetime-local" class="access-edit-end" value="${endVal}">
                    </div>
                </div>
                ${lockersEditHtml}
                <div class="access-edit-actions">
                    <button type="button" class="btn-secondary btn-sm btn-access-cancel"
                            data-access-id="${access.id}">Отмена</button>
                    <button type="button" class="btn-primary btn-sm btn-access-save"
                            data-access-id="${access.id}">Сохранить</button>
                </div>
            </div>
        </div>
    `;
}

async function toggleAccessActive(userId, accessId, isActive) {
    const url = isActive
        ? USERS_API.REACTIVATE_ACCESS(accessId)
        : USERS_API.DEACTIVATE_ACCESS(accessId);
    const method = isActive ? 'POST' : 'DELETE';

    try {
        const response = await fetch(url, { method, headers: API_HEADERS });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const item = document.querySelector(`.access-item[data-access-id="${accessId}"]`);
        if (item) item.classList.toggle('access-item--inactive', !isActive);

        showNotification(isActive ? 'Допуск активирован' : 'Допуск деактивирован', 'success');
        loadUsers(document.getElementById('searchUsers').value.trim());
    } catch (error) {
        console.error('Ошибка изменения статуса допуска:', error);
        showNotification('Ошибка изменения статуса', 'error');
        const input = document.querySelector(`.access-toggle-input[data-access-id="${accessId}"]`);
        if (input) input.checked = !isActive;
    }
}

function toggleAccessEditForm(accessId, show = null) {
    const item = document.querySelector(`.access-item[data-access-id="${accessId}"]`);
    if (!item) return;
    const form = item.querySelector('.access-edit-form');
    const view = item.querySelector('.access-item-view');
    const open = show !== null ? show : form.classList.contains('hidden');
    form.classList.toggle('hidden', !open);
    view.classList.toggle('hidden', open);
}

async function saveAccessEdit(userId, accessId) {
    const original = _accessesCache[accessId];
    if (!original) return;

    const item = document.querySelector(`.access-item[data-access-id="${accessId}"]`);
    if (!item) return;

    const patch = {};
    const startInput = item.querySelector('.access-edit-start');
    const endInput = item.querySelector('.access-edit-end');
    const origStart = original.start_at ? original.start_at.slice(0, 16) : '';
    const origEnd = original.end_at ? original.end_at.slice(0, 16) : '';

    if (startInput.value !== origStart) {
        patch.start_at = startInput.value ? `${startInput.value}:00Z` : null;
    }
    if (endInput.value !== origEnd) {
        patch.end_at = endInput.value ? `${endInput.value}:00Z` : null;
    }

    if (!original.full_access) {
        const tagsContainer = item.querySelector('.access-locker-tags');
        if (tagsContainer) {
            const newIds = Array.from(tagsContainer.querySelectorAll('.access-locker-tag'))
                .map(t => t.dataset.uuid);
            const origIds = (original.lockers || []).map(l => l.uuid);
            if ([...newIds].sort().join(',') !== [...origIds].sort().join(',')) {
                patch.locker_ids = newIds;
            }
        }
    }

    if (Object.keys(patch).length === 0) {
        toggleAccessEditForm(accessId, false);
        return;
    }

    try {
        const response = await fetch(USERS_API.PATCH_ACCESS(accessId), {
            method: 'PATCH',
            headers: { ...API_HEADERS, 'Content-Type': 'application/json' },
            body: JSON.stringify(patch)
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const updated = normalizeAccess(await response.json());
        _accessesCache[accessId] = updated;

        // Обновляем view in-place
        const view = item.querySelector('.access-item-view');
        const [startVal, endVal] = view.querySelectorAll('.access-date-value');
        startVal.textContent = updated.start_at ? formatAccessDateTime(updated.start_at) : '—';
        endVal.textContent = updated.end_at ? formatAccessDateTime(updated.end_at) : 'Бессрочно';

        if (!updated.full_access) {
            const lockersList = view.querySelector('.access-lockers-list');
            if (lockersList) {
                lockersList.innerHTML = (updated.lockers || []).length
                    ? updated.lockers.map(l =>
                        `<span class="access-locker-uuid" title="${escapeHtml(l.uuid)}">${escapeHtml(_lockerNames[l.uuid] || l.uuid)}</span>`
                    ).join('')
                    : '<span class="access-lockers-none">—</span>';
            }
        }

        // Актуализируем значения инпутов для следующего редактирования
        startInput.value = updated.start_at ? updated.start_at.slice(0, 16) : '';
        endInput.value = updated.end_at ? updated.end_at.slice(0, 16) : '';

        toggleAccessEditForm(accessId, false);
        showNotification('Допуск обновлён', 'success');
        loadUsers(document.getElementById('searchUsers').value.trim());
    } catch (error) {
        console.error('Ошибка сохранения допуска:', error);
        showNotification('Ошибка сохранения', 'error');
    }
}

function closeAccessModal() {
    document.getElementById('accessModal').style.display = 'none';
    currentAccessUserId = null;
    const cb = document.getElementById('includeInactiveAccesses');
    if (cb) cb.checked = false;
    hideNewAccessForm();
}

function showNewAccessForm() {
    const emptyEl = document.getElementById('accessesEmpty');
    if (!emptyEl.classList.contains('hidden')) {
        emptyEl.classList.add('hidden');
    }
    document.getElementById('newAccessForm').classList.remove('hidden');
    document.getElementById('addAccessBtn').classList.add('hidden');
    document.getElementById('newAccessFullAccess').checked = false;
    document.getElementById('newAccessLockersSection').classList.remove('hidden');
    document.getElementById('newAccessLockerTags').innerHTML = '';
    document.getElementById('newAccessLockerInput').value = '';
    document.getElementById('newAccessStart').value = '';
    document.getElementById('newAccessEnd').value = '';
}

function hideNewAccessForm() {
    document.getElementById('newAccessForm').classList.add('hidden');
    document.getElementById('addAccessBtn').classList.remove('hidden');
    const listEl = document.getElementById('accessesList');
    if (!listEl.hasChildNodes()) {
        document.getElementById('accessesEmpty').classList.remove('hidden');
    }
}

async function saveNewAccess(userId) {
    if (!userId) return;

    const fullAccess = document.getElementById('newAccessFullAccess').checked;
    const startVal = document.getElementById('newAccessStart').value;
    const endVal = document.getElementById('newAccessEnd').value;

    if (!startVal) { showNotification('Укажите дату и время начала', 'warning'); return; }
    if (!endVal) { showNotification('Укажите дату и время окончания', 'warning'); return; }
    if (!fullAccess) {
        const tags = document.getElementById('newAccessLockerTags');
        if (tags.querySelectorAll('.access-locker-tag').length === 0) {
            showNotification('Добавьте хотя бы один шкаф или включите полный доступ', 'warning');
            return;
        }
    }

    const body = {
        start_at: startVal ? `${startVal}:00Z` : null,
        end_at: endVal ? `${endVal}:00Z` : null,
        full_access: fullAccess
    };

    if (!fullAccess) {
        const tags = document.getElementById('newAccessLockerTags');
        body.locker_ids = Array.from(tags.querySelectorAll('.access-locker-tag')).map(t => t.dataset.uuid);
    }

    try {
        const response = await fetch(USERS_API.CREATE_ACCESS(userId), {
            method: 'POST',
            headers: { ...API_HEADERS, 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        showNotification('Допуск выдан', 'success');
        await showAccessModal(userId);
        loadUsers(document.getElementById('searchUsers').value.trim());
    } catch (error) {
        console.error('Ошибка выдачи допуска:', error);
        showNotification('Ошибка выдачи допуска', 'error');
    }
}

function generatePassword() {
    const upper = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
    const lower = 'abcdefghijklmnopqrstuvwxyz';
    const digits = '0123456789';
    const special = '!@#$%^&*()_+-=[]{}';
    const all = upper + lower + digits + special;

    // Гарантируем минимум по одному символу каждого типа
    const required = [
        upper[Math.floor(Math.random() * upper.length)],
        lower[Math.floor(Math.random() * lower.length)],
        digits[Math.floor(Math.random() * digits.length)],
        special[Math.floor(Math.random() * special.length)]
    ];

    const rest = Array.from({ length: 6 }, () => all[Math.floor(Math.random() * all.length)]);
    const chars = required.concat(rest);

    // Перемешиваем (Fisher-Yates)
    for (let i = chars.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [chars[i], chars[j]] = [chars[j], chars[i]];
    }
    return chars.join('');
}

function showConfirmModal({ title, text, subtext, iconClass, iconFa, okClass, okText, showDateField, onConfirm }) {
    document.getElementById('confirmModalTitle').textContent = title;
    document.getElementById('confirmModalText').textContent = text;
    document.getElementById('confirmModalSubtext').textContent = subtext || '';
    document.getElementById('confirmModalIcon').className = `confirm-icon ${iconClass}`;
    document.getElementById('confirmModalIconFa').className = `fas ${iconFa}`;

    const okBtn = document.getElementById('confirmModalOk');
    okBtn.className = okClass;
    okBtn.textContent = okText || 'Подтвердить';

    const dateSection = document.getElementById('confirmDateSection');
    if (showDateField) {
        dateSection.classList.remove('hidden');
        document.getElementById('confirmExpiryDate').value = '';
    } else {
        dateSection.classList.add('hidden');
    }

    pendingConfirmCallback = onConfirm;
    document.getElementById('confirmModal').style.display = 'flex';
}

function closeConfirmModal() {
    document.getElementById('confirmModal').style.display = 'none';
    pendingConfirmCallback = null;
}

// Вспомогательные функции
function showValidationError(fieldId, message) {
    const errorElement = document.getElementById(fieldId + 'Error');
    if (errorElement) {
        errorElement.textContent = message;
        document.getElementById(fieldId)?.classList.add('error');
    }
}

// Нормализация телефона: возвращает +7XXXXXXXXXX или null если формат не распознан
function normalizePhone(raw) {
    if (!raw) return '';
    const digits = raw.replace(/\D/g, '');
    if (/^\+7\d{10}$/.test(raw)) return raw;
    if (/^7\d{10}$/.test(digits)) return '+' + digits;
    if (/^8\d{10}$/.test(digits)) return '+7' + digits.slice(1);
    return null;
}

function formatDate(date) {
    return date.toLocaleDateString('ru-RU', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric'
    });
}

function formatISODate(isoStr) {
    const [year, month, day] = isoStr.slice(0, 10).split('-');
    return `${day}.${month}.${year}`;
}

// Формат даты со временем для допусков: ДД.ММ.ГГГГ ЧЧ:ММ
function formatAccessDateTime(isoStr) {
    const datePart = formatISODate(isoStr);
    const timePart = isoStr.slice(11, 16);
    return timePart ? `${datePart} ${timePart}` : datePart;
}

// Парсит дату из ISO-строки в локальное время без сдвига по часовому поясу
function parseDateOnly(str) {
    const [year, month, day] = str.slice(0, 10).split('-').map(Number);
    return new Date(year, month - 1, day);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Пагинация
function renderPagination(total) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;

    const pageSizeOptions = [10, 20, 50, 100, 200];
    let html = '<div class="pagination-footer">';

    // Селектор размера страницы
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

    // Пагинация по страницам
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
                const searchTerm = document.getElementById('searchUsers').value.trim();
                loadUsers(searchTerm, page);
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
                const searchTerm = document.getElementById('searchUsers').value.trim();
                loadUsers(searchTerm);
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

function showLoader() {
    document.body.classList.add('loading');
}

function hideLoader() {
    document.body.classList.remove('loading');
}

function closeUserModal() {
    document.getElementById('userModal').style.display = 'none';
    document.querySelectorAll('#userForm .error-message').forEach(el => el.textContent = '');
    document.querySelectorAll('#userForm .error').forEach(el => el.classList.remove('error'));
    const passwordInput = document.getElementById('userPassword');
    passwordInput.value = '';
    passwordInput.type = 'password';
    document.getElementById('togglePasswordIcon').className = 'fas fa-eye';
    document.getElementById('togglePassword').title = 'Показать пароль';
}

function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text)
            .then(() => showNotification('Ссылка скопирована', 'success'))
            .catch(() => showNotification('Не удалось скопировать ссылку', 'error'));
        return;
    }

    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
        document.execCommand('copy');
        showNotification('Ссылка скопирована', 'success');
    } catch (error) {
        showNotification('Не удалось скопировать ссылку', 'error');
    } finally {
        document.body.removeChild(textarea);
    }
}

function showNotification(message, type = 'info') {
    const toast = document.getElementById('toastNotification');
    const toastIcon = document.getElementById('toastIcon');
    const toastMessage = document.getElementById('toastMessage');

    const icons = {
        success: 'fa-check-circle',
        error: 'fa-exclamation-circle',
        warning: 'fa-exclamation-triangle',
        info: 'fa-info-circle'
    };

    toast.className = `toast-notification${type === 'error' ? ' toast-error' : type === 'warning' ? ' toast-warning' : ''}`;
    toastIcon.className = `fas ${icons[type] || icons.info}`;
    toastMessage.textContent = message;
    toast.style.display = 'flex';
    toast.style.animation = 'none';
    requestAnimationFrame(() => { toast.style.animation = 'slideInToast 0.3s ease'; });

    clearTimeout(toast._hideTimeout);
    toast._hideTimeout = setTimeout(() => { toast.style.display = 'none'; }, 3000);
}
