// doors.js — Модуль "Двери" (демо на мок-данных, без интеграции с бэкендом)

const DOORS_MOCK_DATA = [
    { id: 'd-01', inventory_number: 'ДВ №001', location_address: 'г. Москва, Тверская ул., 7', connectivity: 'online', door: 'open', location_lat: 55.7601, location_lon: 37.6094 },
    { id: 'd-02', inventory_number: 'ДВ №002', location_address: 'г. Москва, Ленинградский пр-т, 39', connectivity: 'online', door: 'closed', location_lat: 55.7887, location_lon: 37.5544 },
    { id: 'd-03', inventory_number: 'ДВ №003', location_address: 'г. Москва, Кутузовский пр-т, 12', connectivity: 'offline', door: 'closed', location_lat: 55.7415, location_lon: 37.5540 },
    { id: 'd-04', inventory_number: 'ДВ №004', location_address: 'г. Москва, Профсоюзная ул., 45', connectivity: 'online', door: 'open', location_lat: 55.6664, location_lon: 37.5372 },
    { id: 'd-05', inventory_number: 'ДВ №005', location_address: 'г. Москва, Варшавское шоссе, 87', connectivity: 'online', door: 'closed', location_lat: 55.6234, location_lon: 37.6122 },
    { id: 'd-06', inventory_number: 'ДВ №006', location_address: 'г. Москва, Волгоградский пр-т, 32', connectivity: 'offline', door: 'closed', location_lat: 55.7115, location_lon: 37.7274 },
    { id: 'd-07', inventory_number: 'ДВ №007', location_address: 'г. Москва, Щёлковское шоссе, 5', connectivity: 'online', door: 'closed', location_lat: 55.7975, location_lon: 37.7532 },
    { id: 'd-08', inventory_number: 'ДВ №008', location_address: 'г. Москва, Дмитровское шоссе, 100', connectivity: 'online', door: 'open', location_lat: 55.8721, location_lon: 37.5462 },
    { id: 'd-09', inventory_number: 'ДВ №009', location_address: 'г. Москва, Ленинский пр-т, 68', connectivity: 'online', door: 'closed', location_lat: 55.6746, location_lon: 37.5560 },
    { id: 'd-10', inventory_number: 'ДВ №010', location_address: 'г. Москва, Каширское шоссе, 24', connectivity: 'offline', door: 'closed', location_lat: 55.6417, location_lon: 37.6706 },
    { id: 'd-11', inventory_number: 'ДВ №011', location_address: 'г. Москва, Рязанский пр-т, 15', connectivity: 'online', door: 'closed', location_lat: 55.7147, location_lon: 37.7692 },
    { id: 'd-12', inventory_number: 'ДВ №012', location_address: 'г. Москва, Ярославское шоссе, 42', connectivity: 'online', door: 'open', location_lat: 55.8632, location_lon: 37.7208 },
    { id: 'd-13', inventory_number: 'ДВ №013', location_address: 'г. Москва, Можайское шоссе, 19', connectivity: 'online', door: 'closed', location_lat: 55.7311, location_lon: 37.4392 },
    { id: 'd-14', inventory_number: 'ДВ №014', location_address: 'г. Москва, Осенний бульвар, 12', connectivity: 'offline', door: 'closed', location_lat: 55.7602, location_lon: 37.4210 },
    { id: 'd-15', inventory_number: 'ДВ №015', location_address: 'г. Москва, New Арбат ул., 21', connectivity: 'online', door: 'closed', location_lat: 55.7522, location_lon: 37.5866 },
    { id: 'd-16', inventory_number: 'ДВ №016', location_address: 'г. Москва, Садовническая наб., 33', connectivity: 'online', door: 'closed', location_lat: 55.7419, location_lon: 37.6339 },
];

let doorsFiltered = [...DOORS_MOCK_DATA]; // текущий результат поиска (все страницы)
let doorsData = [...DOORS_MOCK_DATA];     // текущая страница (для карточек)
let doorsMap = null;
let doorsMarkers = [];
let doorsCurrentPage = 1;
let doorsPageSize = 8;

const doorsBlueIcon = L.icon({
    iconUrl: '/libs/leaflet/images/marker-icon.png',
    iconRetinaUrl: '/libs/leaflet/images/marker-icon-2x.png',
    shadowUrl: '/libs/leaflet/marker-shadow.png',
    iconSize: [25, 41],
    iconAnchor: [12, 41],
    popupAnchor: [1, -34],
    shadowSize: [41, 41]
});

const doorsGrayIcon = L.icon({
    iconUrl: '/libs/leaflet/images/marker-icon-red.png',
    iconRetinaUrl: '/libs/leaflet/images/marker-icon-2x-red.png',
    shadowUrl: '/libs/leaflet/marker-shadow.png',
    iconSize: [25, 41],
    iconAnchor: [12, 41],
    popupAnchor: [1, -34],
    shadowSize: [41, 41]
});

export function initDoorsModule() {
    // При повторной навигации уничтожаем старый экземпляр карты,
    // иначе Leaflet не сможет инициализировать новый контейнер
    if (doorsMap) {
        doorsMap.remove();
        doorsMap = null;
        doorsMarkers = [];
    }
    setupDoorsEventListeners();
    initDoorsMap();
    performDoorsSearch('');
}

function setupDoorsEventListeners() {
    const searchInput = document.getElementById('doorsSearch');
    const clearSearchBtn = document.getElementById('clearDoorsSearch');

    if (searchInput) {
        searchInput.addEventListener('input', debounceDoors(function (e) {
            const query = e.target.value.trim();
            clearSearchBtn.style.display = query ? 'flex' : 'none';
            performDoorsSearch(query);
        }, 300));
    }

    if (clearSearchBtn) {
        clearSearchBtn.addEventListener('click', function () {
            searchInput.value = '';
            performDoorsSearch('');
            this.style.display = 'none';
        });
    }

    const closeBtn = document.getElementById('closeDoorQuickView');
    if (closeBtn) closeBtn.addEventListener('click', closeDoorQuickView);

    const openBtn = document.getElementById('openDoorBtn');
    if (openBtn) openBtn.addEventListener('click', toggleCurrentDoor);

    const modalOverlay = document.getElementById('doorQuickViewModal');
    if (modalOverlay) {
        modalOverlay.addEventListener('click', function (e) {
            if (e.target === modalOverlay) closeDoorQuickView();
        });
    }
}

let doorsSearchQuery = '';

function performDoorsSearch(query, page = 1) {
    doorsSearchQuery = query;
    const q = query.trim().toLowerCase();
    doorsFiltered = !q
        ? [...DOORS_MOCK_DATA]
        : DOORS_MOCK_DATA.filter(door =>
            door.inventory_number.toLowerCase().includes(q) ||
            door.location_address.toLowerCase().includes(q)
        );

    const totalPages = Math.max(1, Math.ceil(doorsFiltered.length / doorsPageSize));
    doorsCurrentPage = Math.min(Math.max(1, page), totalPages);

    const start = (doorsCurrentPage - 1) * doorsPageSize;
    doorsData = doorsFiltered.slice(start, start + doorsPageSize);

    displayDoors();
    renderDoorsPagination(doorsFiltered.length);
    addDoorsMarkersToMap(doorsFiltered);
}

function displayDoors() {
    const grid = document.getElementById('doorsGrid');
    const emptyState = document.getElementById('doorsEmptyState');
    const paginationContainer = document.getElementById('doorsPaginationContainer');
    if (!grid || !emptyState) return;

    if (doorsFiltered.length === 0) {
        grid.innerHTML = '';
        grid.style.display = 'none';
        if (paginationContainer) paginationContainer.style.display = 'none';
        emptyState.style.display = 'block';
        return;
    }

    emptyState.style.display = 'none';
    grid.style.display = '';
    if (paginationContainer) paginationContainer.style.display = '';
    grid.innerHTML = doorsData.map(door => createDoorCard(door)).join('');

    document.querySelectorAll('#doorsGrid .cabinet-card').forEach(card => {
        card.addEventListener('click', function () {
            showDoorQuickView(this.dataset.doorId);
        });
    });
}

function createDoorCard(door) {
    const stateClass = door.connectivity === 'online' ? 'online' : 'offline';
    const stateText = door.connectivity === 'online' ? 'В сети' : 'Не в сети';
    const openClass = door.door === 'open' ? ' cabinet-card--open' : '';
    const offlineClass = door.connectivity !== 'online' ? ' cabinet-card--offline' : '';

    return `
        <div class="cabinet-card${openClass}${offlineClass}" data-door-id="${door.id}">
            <div class="cabinet-header">
                <div class="cabinet-id">${door.inventory_number}</div>
                <span class="connection-status ${stateClass}">
                    <i class="fas fa-circle"></i>
                    ${stateText}
                </span>
            </div>
            <div class="cabinet-location">
                <i class="fas fa-map-marker-alt"></i>
                <span>${door.location_address}</span>
            </div>
        </div>
    `;
}

function initDoorsMap() {
    const mapContainer = document.getElementById('doors-map');
    if (!mapContainer) return;
    if (!doorsMap) {
        doorsMap = L.map('doors-map').setView([55.751244, 37.618423], 10);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        }).addTo(doorsMap);
    }
}

function addDoorsMarkersToMap(items) {
    if (!doorsMap) return;

    doorsMarkers.forEach(marker => doorsMap.removeLayer(marker));
    doorsMarkers = [];

    items.forEach(door => {
        if (!door.location_lat || !door.location_lon) return;
        const marker = L.marker([door.location_lat, door.location_lon], {
            icon: door.connectivity === 'online' ? doorsBlueIcon : doorsGrayIcon
        });
        marker.on('click', () => showDoorQuickView(door.id));
        marker.addTo(doorsMap);
        doorsMarkers.push(marker);
    });
}

function showDoorQuickView(doorId) {
    const modal = document.getElementById('doorQuickViewModal');
    const invElement = document.getElementById('doorQuickViewInventory');
    const addrElement = document.getElementById('doorQuickViewAddress');
    const statusElement = document.getElementById('doorQuickViewStatus');
    if (!modal) return;

    const door = DOORS_MOCK_DATA.find(d => d.id === doorId);
    if (!door) return;

    invElement.textContent = door.inventory_number;
    addrElement.textContent = door.location_address;
    statusElement.textContent = door.connectivity === 'online' ? 'Онлайн' : 'Офлайн';
    statusElement.style.color = door.connectivity === 'online' ? 'var(--success-color)' : 'var(--gray-color)';
    modal.dataset.doorId = door.id;

    const openBtn = document.getElementById('openDoorBtn');
    if (openBtn) {
        openBtn.disabled = door.connectivity !== 'online';
        const isOpen = door.door === 'open';
        openBtn.innerHTML = isOpen
            ? '<i class="fas fa-door-closed"></i> Закрыть'
            : '<i class="fas fa-door-open"></i> Открыть';
    }

    modal.style.display = 'flex';
}

function closeDoorQuickView() {
    const modal = document.getElementById('doorQuickViewModal');
    if (modal) modal.style.display = 'none';
}

function toggleCurrentDoor() {
    const modal = document.getElementById('doorQuickViewModal');
    const doorId = modal ? modal.dataset.doorId : null;
    const door = DOORS_MOCK_DATA.find(d => d.id === doorId);
    if (!door || door.connectivity !== 'online') return;

    door.door = door.door === 'open' ? 'closed' : 'open';

    showDoorsNotification(
        `Дверь «${door.inventory_number}» ${door.door === 'open' ? 'открыта' : 'закрыта'}`,
        'success'
    );
    closeDoorQuickView();
    performDoorsSearch(doorsSearchQuery, doorsCurrentPage);
}

function showDoorsNotification(message, type) {
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

// ==================== ПАГИНАЦИЯ ====================
function renderDoorsPagination(total) {
    const container = document.getElementById('doorsPaginationContainer');
    if (!container) return;

    const totalPages = Math.max(1, Math.ceil(total / doorsPageSize));
    const pageSizeOptions = [4, 8, 12, 20];
    let html = '<div class="pagination-footer">';

    html += '<div class="page-size-selector">';
    html += '<div class="page-size-select-wrapper">';
    html += '<select class="page-size-select" id="doorsPageSizeSelect">';
    pageSizeOptions.forEach(size => {
        html += `<option value="${size}"${size === doorsPageSize ? ' selected' : ''}>${size}</option>`;
    });
    html += '</select>';
    html += '<i class="fas fa-chevron-down page-size-arrow"></i>';
    html += '</div>';
    html += '</div>';

    if (totalPages > 1) {
        const pages = getDoorsPaginationPages(doorsCurrentPage, totalPages);
        html += '<div class="pagination">';

        html += `<button class="pagination-btn pagination-prev" ${doorsCurrentPage === 1 ? 'disabled' : ''} data-page="${doorsCurrentPage - 1}">
            <i class="fas fa-chevron-left"></i>
        </button>`;

        pages.forEach(p => {
            if (p === '...') {
                html += '<span class="pagination-ellipsis">…</span>';
            } else {
                html += `<button class="pagination-btn pagination-page${p === doorsCurrentPage ? ' active' : ''}" data-page="${p}">${p}</button>`;
            }
        });

        html += `<button class="pagination-btn pagination-next" ${doorsCurrentPage === totalPages ? 'disabled' : ''} data-page="${doorsCurrentPage + 1}">
            <i class="fas fa-chevron-right"></i>
        </button>`;

        html += '</div>';
    }

    html += '</div>';
    container.innerHTML = html;

    container.querySelectorAll('.pagination-btn:not(:disabled)').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page, 10);
            if (page && page !== doorsCurrentPage) {
                performDoorsSearch(doorsSearchQuery, page);
            }
        });
    });

    const pageSizeSelect = container.querySelector('#doorsPageSizeSelect');
    if (pageSizeSelect) {
        pageSizeSelect.addEventListener('change', () => {
            const size = parseInt(pageSizeSelect.value, 10);
            if (size !== doorsPageSize) {
                doorsPageSize = size;
                performDoorsSearch(doorsSearchQuery, 1);
            }
        });
    }
}

function getDoorsPaginationPages(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
    if (current <= 4) return [1, 2, 3, 4, 5, '...', total];
    if (current >= total - 3) return [1, '...', total - 4, total - 3, total - 2, total - 1, total];
    return [1, '...', current - 1, current, current + 1, '...', total];
}

function debounceDoors(func, wait) {
    let timeout;
    return function (...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}
