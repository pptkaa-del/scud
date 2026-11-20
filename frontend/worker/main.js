// main.js — инициализация оболочки приложения и загрузка модулей
window.API_BASE = '/api/v1';

const ROLE_LABELS_MAIN = {
    worker: 'Рабочий',
    administrator: 'Администратор',
    operator: 'Оператор'
};

const API_HEADERS_MAIN = { 'Content-Type': 'application/json' };

// Имена функций инициализации для каждого модуля.
// Скрипт модуля загружается один раз; при повторной навигации
// вызываем уже существующую глобальную функцию напрямую.
const MODULE_INITS = {
    'open-lockers':    'initOpenLockersModule',
    'lockers':         'initLockersModule',
    'lockers-detail':  'initLockersDetailModule',
    'doors':           'initDoorsModule',
};

// JS-зависимости, которые нужно загрузить до скрипта модуля.
// Каждый скрипт загружается единожды (проверяем по src).
const MODULE_DEPS = {
    'lockers': [
        '/libs/leaflet/leaflet.js',
        '/libs/leaflet-awesome-markers/leaflet.awesome-markers.js',
        '/libs/leaflet-markercluster/leaflet.markercluster.js',
    ],
    'doors': [
        '/libs/leaflet/leaflet.js',
    ],
};

// Кэш промисов загрузки скриптов: параллельные вызовы ждут один и тот же
// реальный onload, а не факт наличия тега <script> (иначе второй вызов
// резолвится до того, как скрипт докачался).
const scriptPromises = new Map();
function loadScript(src) {
    if (scriptPromises.has(src)) return scriptPromises.get(src);
    const p = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = () => {
            scriptPromises.delete(src);
            reject(new Error(`Failed to load: ${src}`));
        };
        document.body.appendChild(s);
    });
    scriptPromises.set(src, p);
    return p;
}

function loadModuleStyle(name) {
    return new Promise((resolve) => {
        let styleEl = document.getElementById('module-style');
        if (!styleEl) {
            styleEl = document.createElement('link');
            styleEl.id = 'module-style';
            styleEl.rel = 'stylesheet';
            document.head.appendChild(styleEl);
        }
        if (styleEl.dataset.module === name) {
            resolve();
            return;
        }
        styleEl.onload = resolve;
        styleEl.onerror = resolve;
        styleEl.dataset.module = name;
        styleEl.href = `${name}.css`;
    });
}

document.addEventListener('DOMContentLoaded', function () {
    initSidebar();
    const module = location.hash.slice(1) || 'lockers';
    loadModule(module);
});

function initSidebar() {
    const profileName = document.querySelector('.profile-name');
    const profileRole = document.querySelector('.profile-role');
    if (profileName) profileName.textContent = localStorage.getItem('user_name') || '';
    if (profileRole) {
        const rawRole = localStorage.getItem('user_role');
        profileRole.textContent = (rawRole && ROLE_LABELS_MAIN[rawRole]) || rawRole || '';
    }

    const menuToggle = document.getElementById('menuToggle');
    const sidebar = document.querySelector('.sidebar');
    const sidebarOverlay = document.getElementById('sidebarOverlay');

    function openSidebar() {
        sidebar.classList.add('active');
        sidebarOverlay?.classList.add('active');
    }
    function closeSidebar() {
        sidebar.classList.remove('active');
        sidebarOverlay?.classList.remove('active');
    }

    menuToggle?.addEventListener('click', () => {
        sidebar.classList.contains('active') ? closeSidebar() : openSidebar();
    });
    sidebarOverlay?.addEventListener('click', closeSidebar);
    window.addEventListener('resize', () => {
        if (window.innerWidth > 768) closeSidebar();
    });

    document.getElementById('logoutBtn')?.addEventListener('click', async () => {
        try {
            await fetch('/api/v1/auth/logout', { method: 'POST', headers: API_HEADERS_MAIN });
        } catch (e) {
            console.error(e);
        }
        localStorage.clear();
        window.location.href = '/login.html';
    });

    document.querySelectorAll('.nav-item[data-module]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const name = item.dataset.module;
            // Единственный источник загрузки — hashchange. Прямой вызов делаем
            // только когда хэш не меняется (клик по активному разделу), иначе
            // получим два параллельных loadModule и гонку загрузки.
            if (location.hash.slice(1) === name) {
                loadModule(name);
            } else {
                location.hash = name;
            }
        });
    });

    window.addEventListener('hashchange', () => {
        const name = location.hash.slice(1);
        if (name) loadModule(name);
    });
}

let loadToken = 0;
async function loadModule(name) {
    // Токен последней навигации: если пока мы ждём (await), пользователь
    // переключился на другой раздел — устаревшая загрузка тихо выходит,
    // не затирая текущий контент.
    const token = ++loadToken;

    document.querySelectorAll('.nav-item[data-module]').forEach(el => {
        el.classList.toggle('active', el.dataset.module === name);
    });

    const mainContent = document.getElementById('mainContent');
    mainContent.innerHTML = '<div class="module-loading"><i class="fas fa-spinner fa-spin"></i></div>';

    try {
        // Параллельно: HTML и CSS — вставляем контент только когда оба готовы
        const [htmlText] = await Promise.all([
            fetch(`${name}-content.html`).then(r => {
                if (!r.ok) throw new Error(`HTML not found: ${name}-content.html`);
                return r.text();
            }),
            loadModuleStyle(name),
        ]);
        if (token !== loadToken) return;

        // Зависимости (Leaflet, flatpickr и т.д.) — загружаются один раз.
        // Грузим до вставки HTML и import, чтобы глобальные (L и т.п.) были
        // готовы к моменту выполнения верхнего уровня модуля.
        for (const dep of (MODULE_DEPS[name] || [])) {
            await loadScript(dep);
        }
        if (token !== loadToken) return;

        mainContent.innerHTML = htmlText;

        // Dynamic import кэширует модуль после первой загрузки
        const mod = await import(`./${name}.js`);
        if (token !== loadToken) return;
        const initFn = MODULE_INITS[name];
        if (typeof mod[initFn] === 'function') mod[initFn]();

    } catch (error) {
        console.error('Ошибка загрузки модуля:', error);
        if (token !== loadToken) return;
        mainContent.innerHTML = `
            <div class="module-error">
                <i class="fas fa-exclamation-circle"></i>
                <p>Модуль «${name}» недоступен</p>
            </div>
        `;
    }
}
