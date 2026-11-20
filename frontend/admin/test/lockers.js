// cabinets.js - Модуль мониторинга шкафов управления (версия с запросом к API при клике на маркер)


let cabinetsData = [];         // текущая страница (для карточек)
let map = null;
let clusterGroup = null;
let currentPage = 1;
let totalPages = 1;
let pageSize = 20;
let currentSearchQuery = '';
let currentFilters = { connectivity: null, door: null, operational_status: null };

const ROLE_LABELS = {
    worker: 'Рабочий',
    administrator: 'Администратор',
    operator: 'Оператор'
};

export function initLockersModule() {
    // При повторной навигации уничтожаем старый экземпляр карты,
    // иначе Leaflet не сможет инициализировать новый контейнер
    if (map) {
        map.remove();
        map = null;
        clusterGroup = null;
    }
    setupEventListeners();
    initMap();
    loadMapMarkers();
    loadCabinets();
}

function setupEventListeners() {
    const searchInput = document.getElementById('cabinetsSearch');
    const clearSearchBtn = document.getElementById('clearCabinetsSearch');

    if (searchInput) {
        searchInput.addEventListener('input', debounce(function(e) {
            const query = e.target.value.trim();
            clearSearchBtn.style.display = query ? 'flex' : 'none';
            currentPage = 1;
            performSearch(query);
        }, 300));
    }

    if (clearSearchBtn) {
        clearSearchBtn.addEventListener('click', function() {
            searchInput.value = '';
            currentPage = 1;
            performSearch('');
            this.style.display = 'none';
        });
    }

    const filterSelects = [
        { id: 'filterConnectivity', key: 'connectivity' },
        { id: 'filterDoor', key: 'door' },
        { id: 'filterOperationalStatus', key: 'operational_status' }
    ];
    filterSelects.forEach(({ id, key }) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', function() { applyFilter(key, this.value); });
    });

    const closeBtn = document.getElementById('closeQuickView');
    if (closeBtn) closeBtn.addEventListener('click', closeQuickView);

    const goToBtn = document.getElementById('goToDetailBtn');
    if (goToBtn) goToBtn.addEventListener('click', goToDetail);

    // Закрытие по клику на overlay
    const modalOverlay = document.getElementById('quickViewModal');
    if (modalOverlay) {
        modalOverlay.addEventListener('click', function(e) {
            if (e.target === modalOverlay) {
                closeQuickView();
            }
        });
    }
}

// Поиск с сервера (для карточек)
async function performSearch(query, page = null) {
    if (page !== null) currentPage = page;
    currentSearchQuery = query;

    try {
        showLoading();
        const params = new URLSearchParams({
            limit: pageSize,
            offset: (currentPage - 1) * pageSize
        });
        if (query) params.set('q', query);
        if (currentFilters.connectivity) params.set('connectivity', currentFilters.connectivity);
        if (currentFilters.door) params.set('door', currentFilters.door);
        if (currentFilters.operational_status) params.set('operational_status', currentFilters.operational_status);
        const response = await fetch(`${API_BASE}/lockers?${params}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`Ошибка сервера: ${response.status}`);
        const data = await response.json();
        cabinetsData = data.items ?? [];
        const total = data.total ?? 0;
        totalPages = Math.max(1, Math.ceil(total / pageSize));
        displayCabinets();
        renderPagination(total);
        const hasActiveSearch = query || currentFilters.connectivity || currentFilters.door || currentFilters.operational_status;
        hasActiveSearch ? updateMapFromSearch(cabinetsData) : loadMapMarkers();
        hideLoading();
    } catch (error) {
        console.error('Ошибка при поиске:', error);
        showNotification('Ошибка при выполнении поиска', 'error');
        showLoadError();
    }
}

async function loadCabinets() {
    await performSearch('');
}

function displayCabinets() {
    const grid = document.getElementById('cabinetsGrid');
    const emptyState = document.getElementById('emptyState');

    if (!grid || !emptyState) return;

    const paginationContainer = document.getElementById('paginationContainer');

    if (cabinetsData.length === 0) {
        grid.innerHTML = '';
        grid.style.display = 'none';
        if (paginationContainer) paginationContainer.style.display = 'none';
        emptyState.style.display = 'block';
        return;
    }

    emptyState.style.display = 'none';
    grid.style.display = '';
    if (paginationContainer) paginationContainer.style.display = '';
    grid.innerHTML = cabinetsData.map(cab => createCabinetCard(cab)).join('');

    // Обработчики клика на карточки
    document.querySelectorAll('.cabinet-card').forEach(card => {
        card.addEventListener('click', function() {
            const id = this.dataset.cabinetId;
            goToCabinetDetail(id);
        });
    });
}

function createCabinetCard(cab) {
    const stateClass = cab.connectivity === 'online' ? 'online' : 'offline';
    const stateText = cab.connectivity === 'online' ? 'В сети' : 'Не в сети';
    const warningClass = cab.operational_status !== 'normal' ? ' cabinet-card--warning' : '';
    const openClass = cab.door === 'open' ? ' cabinet-card--open' : '';

    return `
        <div class="cabinet-card${warningClass}${openClass}" data-cabinet-id="${cab.id}">
            <div class="cabinet-header">
                <div class="cabinet-id">${cab.inventory_number}</div>
                <span class="connection-status ${stateClass}">
                    <i class="fas fa-circle"></i>
                    ${stateText}
                </span>
            </div>
            <div class="cabinet-location">
                <i class="fas fa-map-marker-alt"></i>
                <span>${cab.location_address}</span>
            </div>
        </div>
    `;
}

function goToCabinetDetail(cabinetId) {
    sessionStorage.setItem('locker_detail_uuid', cabinetId);
    location.hash = 'lockers-detail';
}

// ==================== КАРТА ====================
async function updateMapFromSearch(items) {
    if (!items || items.length === 0) {
        if (clusterGroup) clusterGroup.clearLayers();
        return;
    }
    const locker_ids = items.map(item => item.id);
    try {
        const response = await fetch(`${API_BASE}/lockers/batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ locker_ids, view: 'location' }),
            cache: 'no-store'
        });
        if (!response.ok) throw new Error(`Ошибка сервера: ${response.status}`);
        const data = await response.json();
        addMarkersToMap(Object.values(data.items ?? {}));
    } catch (error) {
        console.error('Ошибка обновления маркеров карты:', error);
        showNotification('Ошибка обновления маркеров карты', 'error');
    }
}

async function loadMapMarkers() {
    try {
        const params = new URLSearchParams({ view: 'location' });
        if (currentFilters.connectivity) params.set('connectivity', currentFilters.connectivity);
        if (currentFilters.door) params.set('door', currentFilters.door);
        if (currentFilters.operational_status) params.set('operational_status', currentFilters.operational_status);
        const response = await fetch(`${API_BASE}/lockers?${params}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`Ошибка сервера: ${response.status}`);
        const data = await response.json();
        addMarkersToMap(data.items ?? []);
    } catch (error) {
        console.error('Ошибка загрузки маркеров карты:', error);
        showNotification('Ошибка загрузки маркеров карты', 'error');
    }
}

function initMap() {
    console.log('initMap called');
    const mapContainer = document.getElementById('cabinets-map');
    if (!mapContainer) {
        console.error('Контейнер карты #cabinets-map не найден!');
        return;
    }
    if (!map) {
        map = L.map('cabinets-map').setView([55.751244, 37.618423], 10);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        }).addTo(map);
        clusterGroup = L.markerClusterGroup({
            chunkedLoading: true,
            chunkSize: 500,
            chunkInterval: 100,
            maxClusterRadius: 60,
            showCoverageOnHover: false,
            iconCreateFunction: function(cluster) {
                const hasWarning = cluster.getAllChildMarkers().some(m => m.options.isWarning);
                return hasWarning ? redIcon : blueIcon;
            }
        });
        map.addLayer(clusterGroup);
        console.log('Map initialized');
    }
}

const blueIcon = L.icon({
    iconUrl: '/libs/leaflet/images/marker-icon.png',
    iconRetinaUrl: '/libs/leaflet/images/marker-icon-2x.png',
    shadowUrl: '/libs/leaflet/marker-shadow.png',
    iconSize: [25, 41],
    iconAnchor: [12, 41],
    popupAnchor: [1, -34],
    shadowSize: [41, 41]
});

const redIcon = L.icon({
    iconUrl: '/libs/leaflet/images/marker-icon-red.png',
    iconRetinaUrl: '/libs/leaflet/images/marker-icon-2x-red.png',
    shadowUrl: '/libs/leaflet/marker-shadow.png',
    iconSize: [25, 41],
    iconAnchor: [12, 41],
    popupAnchor: [1, -34],
    shadowSize: [41, 41]
});

function addMarkersToMap(cabinetsArray) {
    if (!map || !clusterGroup) {
        console.error('Map or clusterGroup not ready');
        return;
    }
    clusterGroup.clearLayers();

    const markers = [];
    cabinetsArray.forEach(cab => {
        if (cab.location_lat && cab.location_lon) {
            const isWarning = cab.operational_status !== 'normal';
            const marker = L.marker([cab.location_lat, cab.location_lon], {
                icon: isWarning ? redIcon : blueIcon,
                isWarning
            });
            marker.on('click', function() {
                showQuickViewViaAPI(cab.id);
            });
            markers.push(marker);
        }
    });

    clusterGroup.addLayers(markers);   // массовое добавление — быстрее чем по одному
}

// ==================== МОДАЛЬНОЕ ОКНО (через API) ====================
async function showQuickViewViaAPI(cabinetId) {
    const modal = document.getElementById('quickViewModal');
    const invElement = document.getElementById('quickViewInventory');
    const addrElement = document.getElementById('quickViewAddress');
    const statusElement = document.getElementById('quickViewStatus');

    if (!modal) return;

    invElement.textContent = 'Загрузка...';
    addrElement.textContent = '...';
    statusElement.textContent = '...';
    modal.style.display = 'flex';

    try {
        const url = `${API_BASE}/locker/${encodeURIComponent(cabinetId)}`;
        const response = await fetch(url, { cache: 'no-store' });
        if (!response.ok) throw new Error(`Ошибка сервера: ${response.status}`);
        const data = await response.json();
        //const items = data.items ?? [];

        //if (items.length === 0) throw new Error('Шкаф не найден');

        //const cabinet = items[0];
        invElement.textContent = data.inventory_number;
        addrElement.textContent = data.location_address;
        statusElement.textContent = data.connectivity === 'online' ? 'Онлайн' : 'Офлайн';
        statusElement.style.color = data.connectivity === 'online' ? 'var(--success-color)' : 'var(--gray-color)';
        modal.dataset.cabinetId = data.id;
    } catch (error) {
        console.error('Error in showQuickViewViaAPI:', error);
        invElement.textContent = 'Ошибка загрузки';
        addrElement.textContent = '—';
        statusElement.textContent = '—';
        showNotification('Не удалось загрузить данные шкафа', 'error');
    }
}

function closeQuickView() {
    const modal = document.getElementById('quickViewModal');
    if (modal) modal.style.display = 'none';
}

function goToDetail() {
    const modal = document.getElementById('quickViewModal');
    const cabinetId = modal ? modal.dataset.cabinetId : null;
    if (cabinetId) {
        closeQuickView();
        goToCabinetDetail(cabinetId);
    }
}

// ==================== ПАГИНАЦИЯ ====================
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
                performSearch(currentSearchQuery, page);
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
                performSearch(currentSearchQuery);
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

// ==================== ФИЛЬТРЫ ====================
const FILTER_LABELS = {
    connectivity: { online: 'Онлайн', offline: 'Офлайн' },
    door: { closed: 'Дверь: закрыта', open: 'Дверь: открыта' },
    operational_status: { normal: 'Норма', maintenance: 'Обслуживание', alarm: 'Тревога' }
};

const FILTER_SELECT_IDS = {
    connectivity: 'filterConnectivity',
    door: 'filterDoor',
    operational_status: 'filterOperationalStatus'
};

function applyFilter(key, value) {
    currentFilters[key] = value || null;
    const select = document.getElementById(FILTER_SELECT_IDS[key]);
    if (select) select.classList.toggle('active', !!value);
    renderFilterChips();
    currentPage = 1;
    loadMapMarkers();
    performSearch(currentSearchQuery);
}

function removeFilter(key) {
    currentFilters[key] = null;
    const select = document.getElementById(FILTER_SELECT_IDS[key]);
    if (select) { select.value = ''; select.classList.remove('active'); }
    renderFilterChips();
    currentPage = 1;
    loadMapMarkers();
    performSearch(currentSearchQuery);
}

function renderFilterChips() {
    const container = document.getElementById('filterChips');
    if (!container) return;
    container.innerHTML = Object.entries(currentFilters)                                 
            .filter(([, v]) => v)                                                                   
            .map(([key, value]) => `
            <span class="filter-chip">
                ${FILTER_LABELS[key]?.[value] ?? value}
                <button type="button" class="filter-chip-remove" data-key="${key}">
                    <i class="fas fa-times"></i>
                </button>
            </span>
        `).join('');
    container.querySelectorAll('.filter-chip-remove').forEach(btn => {
        btn.addEventListener('click', () => removeFilter(btn.dataset.key));
    });
}

// ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
function debounce(func, wait) {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}

function showLoading() {
    const grid = document.getElementById('cabinetsGrid');
    if (grid) {
        grid.innerHTML = `
            <div style="grid-column: 1/-1; display: flex; flex-direction: column; align-items: center; padding: 60px 40px;">
                <div class="spinner"></div>
                <p style="margin-top: 20px; color: var(--gray-color); font-size: 15px;">Загрузка данных...</p>
            </div>
        `;
    }
}

function hideLoading() {
    // Можно оставить пустым или скрыть индикатор, если он был добавлен отдельно
}

function showLoadError() {
    const grid = document.getElementById('cabinetsGrid');
    if (grid) {
        grid.innerHTML = `
            <div style="grid-column: 1/-1; display: flex; flex-direction: column; align-items: center; padding: 60px 40px;">
                <i class="fas fa-exclamation-triangle" style="font-size: 32px; color: var(--danger-color, #e74c3c);"></i>
                <p style="margin-top: 20px; color: var(--gray-color); font-size: 15px;">Не удалось загрузить данные. Перезагрузите страницу.</p>
            </div>
        `;
    }
    const paginationContainer = document.getElementById('paginationContainer');
    if (paginationContainer) paginationContainer.style.display = 'none';
    const emptyState = document.getElementById('emptyState');
    if (emptyState) emptyState.style.display = 'none';
}

function showNotification(message, type) {
    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.innerHTML = `
        <div class="notification-content">
            <i class="fas fa-${type === 'error' ? 'exclamation-circle' : 'check-circle'}"></i>
            <span>${message}</span>
        </div>
    `;
    document.body.appendChild(notification);
    notification.style.display = 'flex';
    setTimeout(() => notification.remove(), 3000);
}
