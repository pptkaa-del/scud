// security.js — Модуль безопасности
let incidentsData = [];
let lockerNames = {};
let selectedLockerId = null;
let currentFilter = 'all';
let currentPage = 1;
let totalPages = 1;
let pageSize = 10;
let totalItems = 0;

const ROLE_LABELS = {
    worker: 'Рабочий',
    administrator: 'Администратор',
    operator: 'Оператор'
};

export function initSecurityModule() {
    setupEventListeners();
    loadIncidents();
}


function setupEventListeners() {
    const searchInput = document.getElementById('incidentsSearch');
    const clearSearchBtn = document.getElementById('clearIncidentsSearch');

    // Загрузка подсказок шкафов при вводе (debounce)
    searchInput.addEventListener('input', debounce(function(e) {
        const term = e.target.value.trim();
        if (!term) {
            hideSuggestions();
            return;
        }
        loadLockerSuggestions(term);
    }, 300));

    // Мгновенная реакция: показать/скрыть крестик и сбросить ранее выбранный шкаф
    searchInput.addEventListener('input', function() {
        clearSearchBtn.style.display = this.value ? 'flex' : 'none';
        if (selectedLockerId !== null) {
            selectedLockerId = null;
            loadIncidents(1);
        }
    });

    clearSearchBtn.addEventListener('click', function() {
        searchInput.value = '';
        this.style.display = 'none';
        hideSuggestions();
        if (selectedLockerId !== null) {
            selectedLockerId = null;
            loadIncidents(1);
        }
    });

    // Закрытие выпадающего списка по клику вне поиска
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.security-search-container')) {
            hideSuggestions();
        }
    });

    document.querySelectorAll('.incidents-filter-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            if (this.dataset.filter === currentFilter) return;
            document.querySelectorAll('.incidents-filter-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            currentFilter = this.dataset.filter;
            loadIncidents(1);
        });
    });
}

// Загрузка подсказок шкафов по инвентарному номеру или адресу
async function loadLockerSuggestions(term) {
    try {
        const url = `${API_BASE}/lockers?q=${encodeURIComponent(term)}&limit=10`;
        const response = await fetch(url);
        if (!response.ok) {
            hideSuggestions();
            return;
        }
        const data = await response.json();
        renderSuggestions(data.items || []);
    } catch (e) {
        console.error('Ошибка загрузки подсказок:', e);
        hideSuggestions();
    }
}

function renderSuggestions(items) {
    const box = document.getElementById('searchSuggestions');
    if (!box) return;

    if (!items.length) {
        box.innerHTML = '<div class="search-suggestion-empty">Ничего не найдено</div>';
        box.style.display = 'block';
        return;
    }

    box.innerHTML = items.map(item => `
        <button type="button" class="search-suggestion" data-id="${item.id}" data-label="${item.inventory_number || ''}">
            <span class="suggestion-number">${item.inventory_number || '—'}</span>
            <span class="suggestion-address">${item.location_address || ''}</span>
        </button>
    `).join('');
    box.style.display = 'block';

    box.querySelectorAll('.search-suggestion').forEach(btn => {
        btn.addEventListener('click', () => selectLocker(btn.dataset.id, btn.dataset.label));
    });
}

function selectLocker(id, label) {
    selectedLockerId = id;
    const searchInput = document.getElementById('incidentsSearch');
    searchInput.value = label;
    document.getElementById('clearIncidentsSearch').style.display = 'flex';
    hideSuggestions();
    loadIncidents(1);
}

function hideSuggestions() {
    const box = document.getElementById('searchSuggestions');
    if (box) {
        box.style.display = 'none';
        box.innerHTML = '';
    }
}

function normalizeIncident(item) {
    return {
        id: item.id,
        locker_id: item.locker_id,
        occurred_at: item.occurred_at,
        severity: item.severity,
        status: item.resolved_at === null ? 'new' : 'viewed',
        resolved_at: item.resolved_at,
        photos: []
    };
}

// Загрузка страницы инцидентов с API
async function loadIncidents(page = null) {
    if (page !== null) currentPage = page;
    try {
        showLoading();
        const offset = (currentPage - 1) * pageSize;
        const resolvedParam = currentFilter === 'new' ? '&resolved=false' : '';
        const lockerParam = selectedLockerId ? `&locker_id=${encodeURIComponent(selectedLockerId)}` : '';
        const url = `${API_BASE}/events/security?event_type=intrusion&limit=${pageSize}&offset=${offset}${resolvedParam}${lockerParam}`;
        const response = await fetch(url);
        if (!response.ok) {
            let detail = `HTTP ${response.status}`;
            try {
                const errData = await response.json();
                if (errData.detail) detail = errData.detail;
            } catch (_) {}
            showNotification(detail);
            throw new Error(detail);
        }
        const data = await response.json();

        totalItems = data.total;
        totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
        incidentsData = data.items.map(normalizeIncident);

        await Promise.all([
            loadMediaForIncidents(incidentsData),
            loadLockerNames(incidentsData.map(i => i.locker_id))
        ]);

        displayIncidents();
        hideLoading();
    } catch (error) {
        console.error('Ошибка загрузки инцидентов:', error);
        showLoadError();
    }
}

async function loadLockerNames(lockerIds) {
    const unique = [...new Set(lockerIds.filter(Boolean))];
    if (!unique.length) return;
    try {
        const response = await fetch(`${API_BASE}/lockers/batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ locker_ids: unique, view: 'brief' })
        });
        if (!response.ok) return;
        const data = await response.json();
        lockerNames = data.items || {};
    } catch (e) {
        console.error('Ошибка загрузки наименований шкафов:', e);
    }
}

async function loadMediaForIncidents(incidents) {
    await Promise.allSettled(incidents.map(async incident => {
        try {
            const response = await fetch(`${API_BASE}/events/security/${incident.id}/media`);
            if (!response.ok) return;
            const data = await response.json();
            incident.photos = data.items
                .filter(m => m.kind === 'photo')
                .map(m => `${API_BASE}/media/${m.id}`);
        } catch (e) {
            // фото не критичны
        }
    }));
}

// Отображение инцидентов (с опциональной клиентской фильтрацией по поиску)
function displayIncidents() {
    const grid = document.getElementById('incidentsGrid');
    const emptyState = document.getElementById('emptyState');

    if (incidentsData.length === 0) {
        grid.innerHTML = '';
        emptyState.style.display = 'block';
        renderPagination(false);
        updateIncidentsCount(0);
        return;
    }

    emptyState.style.display = 'none';

    const sorted = [...incidentsData].sort((a, b) => {
        const statusOrder = { 'new': 0, 'viewed': 1 };
        return statusOrder[a.status] - statusOrder[b.status];
    });

    grid.innerHTML = sorted.map(incident => createIncidentCard(incident)).join('');
    addIncidentCardEventListeners();

    renderPagination(true);
    updateIncidentsCount(totalItems);
}

function createPhotoBlock(incident) {
    const photos = incident.photos || [];
    if (photos.length === 0) {
        return `<div class="incident-photo">
            <div class="photo-placeholder">
                <i class="fas fa-camera"></i>
                <span>Фотофиксация инцидента</span>
            </div>
        </div>`;
    }
    if (photos.length === 1) {
        return `<div class="incident-photo">
            <img src="${photos[0]}" alt="Фото инцидента" class="incident-photo-img">
        </div>`;
    }
    const slides = photos.map((url, i) =>
        `<div class="carousel-slide${i === 0 ? ' active' : ''}">
            <img src="${url}" alt="Фото инцидента ${i + 1}" class="incident-photo-img">
        </div>`
    ).join('');
    const dots = photos.map((_, i) =>
        `<button class="carousel-dot${i === 0 ? ' active' : ''}" data-index="${i}"></button>`
    ).join('');
    return `<div class="incident-photo incident-carousel" data-current="0" data-total="${photos.length}">
        <div class="carousel-slides">${slides}</div>
        <button class="carousel-btn carousel-prev"><i class="fas fa-chevron-left"></i></button>
        <button class="carousel-btn carousel-next"><i class="fas fa-chevron-right"></i></button>
        <div class="carousel-dots">${dots}</div>
        <span class="carousel-counter">1 / ${photos.length}</span>
    </div>`;
}

// Создание карточки инцидента
function createIncidentCard(incident) {
    const time = formatDateTime(new Date(incident.occurred_at));

    return `
        <div class="incident-card ${incident.status}" data-incident-id="${incident.id}">
            <div class="incident-header">
                <span class="incident-time">${time}</span>
            </div>

            ${createPhotoBlock(incident)}

            <div class="incident-info">
                <div class="incident-location">
                    <span class="location-text">${lockerNames[incident.locker_id]?.inventory_number || incident.locker_id}</span>
                </div>

                <div class="incident-actions">
                    <a href="#lockers-detail" class="btn-incident btn-locker-link" data-uuid="${incident.locker_id}">
                        <i class="fas fa-external-link-alt"></i>
                        К шкафу
                    </a>
                </div>
            </div>
        </div>
    `;
}

// Добавление обработчиков для карточек
function addIncidentCardEventListeners() {
    document.querySelectorAll('.btn-locker-link').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            sessionStorage.setItem('locker_detail_uuid', link.dataset.uuid);
            location.hash = 'lockers-detail';
        });
    });

    document.querySelectorAll('.incident-photo-img').forEach(img => {
        img.addEventListener('click', function() {
            window.open(this.src, '_blank');
        });
    });

    document.querySelectorAll('.incident-carousel').forEach(carousel => {
        const slides = carousel.querySelectorAll('.carousel-slide');
        const dots = carousel.querySelectorAll('.carousel-dot');
        const counter = carousel.querySelector('.carousel-counter');
        const total = parseInt(carousel.dataset.total);
        let current = 0;

        function goTo(index) {
            slides[current].classList.remove('active');
            dots[current].classList.remove('active');
            current = (index + total) % total;
            slides[current].classList.add('active');
            dots[current].classList.add('active');
            if (counter) counter.textContent = `${current + 1} / ${total}`;
        }

        carousel.querySelector('.carousel-prev')?.addEventListener('click', e => {
            e.stopPropagation();
            goTo(current - 1);
        });
        carousel.querySelector('.carousel-next')?.addEventListener('click', e => {
            e.stopPropagation();
            goTo(current + 1);
        });
        dots.forEach((dot, i) => {
            dot.addEventListener('click', e => {
                e.stopPropagation();
                goTo(i);
            });
        });
    });
}

// Обновление счетчика инцидентов
function updateIncidentsCount(count) {
    const countElement = document.getElementById('incidentsCount');
    if (count === 0) {
        countElement.textContent = 'Нет инцидентов';
    } else {
        const word = getRussianWordForm(count, ['инцидент', 'инцидента', 'инцидентов']);
        countElement.textContent = `${count} ${word}`;
    }
}

function getRussianWordForm(number, forms) {
    number = Math.abs(number) % 100;
    const remainder = number % 10;
    if (number > 10 && number < 20) return forms[2];
    if (remainder > 1 && remainder < 5) return forms[1];
    if (remainder === 1) return forms[0];
    return forms[2];
}

// Пагинация
function renderPagination(showPages) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;

    if (!showPages || totalItems === 0) {
        container.style.display = 'none';
        return;
    }

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
            if (page && page !== currentPage) loadIncidents(page);
        });
    });

    const pageSizeSelect = container.querySelector('#pageSizeSelect');
    if (pageSizeSelect) {
        pageSizeSelect.addEventListener('change', () => {
            const size = parseInt(pageSizeSelect.value);
            if (size !== pageSize) {
                pageSize = size;
                loadIncidents(1);
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

// Вспомогательные функции
function formatDateTime(date, full = false) {
    if (full) {
        return date.toLocaleString('ru-RU');
    }

    const now = new Date();
    const diffMs = now - date;
    const diffMinutes = Math.floor(diffMs / (1000 * 60));

    if (diffMinutes < 1) return 'только что';
    if (diffMinutes < 60) return `${diffMinutes} мин. назад`;
    if (diffMinutes < 1440) return `${Math.floor(diffMinutes / 60)} ч. назад`;

    return date.toLocaleDateString('ru-RU') + ' ' + date.toLocaleTimeString('ru-RU').slice(0, 5);
}

function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

function showLoading() {
    const grid = document.getElementById('incidentsGrid');
    grid.innerHTML = `
        <div style="grid-column: 1/-1; display: flex; flex-direction: column; align-items: center; padding: 60px 40px;">
            <div class="spinner"></div>
            <p style="margin-top: 20px; color: var(--gray-color); font-size: 15px;">Загрузка инцидентов...</p>
        </div>
    `;
}

function hideLoading() {}

function showLoadError() {
    const grid = document.getElementById('incidentsGrid');
    grid.innerHTML = `
        <div style="grid-column: 1/-1; display: flex; flex-direction: column; align-items: center; padding: 60px 40px; text-align: center;">
            <i class="fas fa-exclamation-circle" style="font-size: 48px; color: var(--danger-color); opacity: 0.6; margin-bottom: 16px;"></i>
            <p style="color: var(--dark-color); font-size: 16px; font-weight: 600;">Не удалось загрузить инциденты</p>
        </div>
    `;
}

function showNotification(message) {
    const notification = document.createElement('div');
    notification.className = 'notification error';
    notification.style.cssText = `
        display: flex;
        position: fixed;
        bottom: 30px;
        right: 30px;
        z-index: 9999;
        background-color: var(--danger-color);
    `;
    notification.innerHTML = `
        <div class="notification-content">
            <i class="fas fa-exclamation-circle"></i>
            <span>${message}</span>
        </div>
    `;
    document.documentElement.appendChild(notification);
    setTimeout(() => notification.remove(), 4000);
}
