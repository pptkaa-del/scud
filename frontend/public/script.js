const API_BASE_URL = '/api/v1';
const REDIRECT_URL = '/';

document.addEventListener('DOMContentLoaded', function () {
    const redLight = document.getElementById('red-light');
    const yellowLight = document.getElementById('yellow-light');
    const greenLight = document.getElementById('green-light');
    const lightStatus = document.getElementById('light-status');

    const authForm = document.getElementById('auth-form');
    const phoneInput = document.getElementById('phone');
    const passwordInput = document.getElementById('password');
    const togglePassword = document.getElementById('toggle-password');
    const messageBox = document.getElementById('message-box');

    const otpModal = document.getElementById('otp-modal');
    const otpInputs = document.querySelectorAll('.otp-input');
    const otpSubmitBtn = document.getElementById('otp-submit-btn');
    const otpBackBtn = document.getElementById('otp-back-btn');
    const otpCloseBtn = document.getElementById('otp-close-btn');
    const otpMessageBox = document.getElementById('otp-message-box');

    let pendingPhone = null;
    let pendingName = null;
    let pendingRole = null;

    activateLight('yellow', '');

    togglePassword.addEventListener('click', function () {
        const type = passwordInput.getAttribute('type') === 'password' ? 'text' : 'password';
        passwordInput.setAttribute('type', type);
        this.classList.toggle('fa-eye');
        this.classList.toggle('fa-eye-slash');
    });

    function normalizePhone(raw) {
        const digits = raw.replace(/\D/g, '');
        if (/^\+7\d{10}$/.test(raw)) return raw;
        if (/^7\d{10}$/.test(digits)) return '+' + digits;
        if (/^8\d{10}$/.test(digits)) return '+7' + digits.slice(1);
        return null;
    }

    authForm.addEventListener('submit', function (e) {
        e.preventDefault();

        const phone = normalizePhone(phoneInput.value.trim());
        if (!phone) {
            phoneInput.style.borderColor = '#ff6b6b';
            showMessage('Введите номер в формате +79991234567 или 89991234567', 'error');
            return;
        }

        activateLight('yellow', 'Проверка данных...');

        fetch(`${API_BASE_URL}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone, password: passwordInput.value }),
        })
            .then(async response => {
                if (response.ok) {
                    const result = await response.json();

                    if (result.message === 'Authentication successful') {
                        localStorage.setItem('user_name', result.name);
                        localStorage.setItem('user_role', result.role);
                        activateLight('green', 'Авторизация успешна!');
                        showMessage(`Добро пожаловать, ${result.name}!`, 'success');
                        setTimeout(() => { window.location.href = REDIRECT_URL; }, 800);

                    } else if (result.message === 'OTP has been sent') {
                        pendingPhone = phone;
                        pendingName = result.name;
                        pendingRole = result.role;
                        activateLight('yellow', 'Ожидание кода...');
                        showOtpSection();
                        showMessage('Код отправлен на ваш телефон', 'success');

                    } else {
                        activateLight('red', 'Ошибка авторизации');
                        showMessage(result.message || 'Неизвестный ответ сервера', 'error');
                    }
                } else {
                    const result = await response.json();
                    activateLight('red', 'Ошибка авторизации');
                    showMessage(result.detail || 'Ошибка авторизации', 'error');
                    const invalidColor = response.status === 401 ? '#ff6b6b' : '';
                    phoneInput.style.borderColor = invalidColor;
                    passwordInput.style.borderColor = invalidColor;
                    setTimeout(() => {
                        phoneInput.style.borderColor = '';
                        passwordInput.style.borderColor = '';
                    }, 3000);
                }
            })
            .catch(() => {
                activateLight('red', 'Ошибка соединения с сервером');
                showMessage('Не удалось подключиться к серверу', 'error');
            });
    });

    // OTP inputs: автопереход, backspace, вставка
    otpInputs.forEach((input, index) => {
        input.addEventListener('input', function () {
            this.value = this.value.replace(/\D/g, '').slice(-1);
            if (this.value && index < otpInputs.length - 1) {
                otpInputs[index + 1].focus();
            }
            this.classList.toggle('filled', !!this.value);
        });

        input.addEventListener('keydown', function (e) {
            if (e.key === 'Backspace' && !this.value && index > 0) {
                otpInputs[index - 1].value = '';
                otpInputs[index - 1].classList.remove('filled');
                otpInputs[index - 1].focus();
            }
        });

        input.addEventListener('paste', function (e) {
            e.preventDefault();
            const digits = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, 6);
            digits.split('').forEach((char, i) => {
                if (otpInputs[i]) {
                    otpInputs[i].value = char;
                    otpInputs[i].classList.add('filled');
                }
            });
            const nextEmpty = [...otpInputs].findIndex(inp => !inp.value);
            (nextEmpty !== -1 ? otpInputs[nextEmpty] : otpInputs[5]).focus();
        });
    });

    otpSubmitBtn.addEventListener('click', submitOtp);

    function submitOtp() {
        const code = [...otpInputs].map(inp => inp.value).join('');
        if (code.length !== 6) {
            showMessage('Введите все 6 цифр кода', 'error');
            return;
        }

        activateLight('yellow', 'Проверка кода...');

        fetch(`${API_BASE_URL}/auth/verify`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phone: pendingPhone, code }),
        })
            .then(async response => {
                if (response.ok) {
                    localStorage.setItem('user_name', pendingName);
                    localStorage.setItem('user_role', pendingRole);
                    hideOtpSection();
                    activateLight('green', 'Авторизация успешна!');
                    showMessage(`Добро пожаловать, ${pendingName}!`, 'success');
                    setTimeout(() => { window.location.href = REDIRECT_URL; }, 800);
                } else {
                    const result = await response.json();
                    activateLight('red', 'Неверный код');
                    showOtpMessage(result.detail || 'Неверный код подтверждения', 'error');
                    otpInputs.forEach(inp => inp.style.borderColor = '#ff6b6b');
                    setTimeout(() => {
                        otpInputs.forEach(inp => {
                            inp.value = '';
                            inp.style.borderColor = '';
                            inp.classList.remove('filled');
                        });
                        otpInputs[0].focus();
                    }, 1500);
                }
            })
            .catch(() => {
                activateLight('red', 'Ошибка соединения с сервером');
                showOtpMessage('Не удалось подключиться к серверу', 'error');
            });
    }

    function closeOtp() {
        pendingPhone = null;
        pendingName = null;
        pendingRole = null;
        hideOtpSection();
        activateLight('yellow', '');
        showMessage('', '');
    }

    otpBackBtn.addEventListener('click', closeOtp);
    otpCloseBtn.addEventListener('click', closeOtp);
    otpModal.addEventListener('click', function (e) {
        if (e.target === otpModal) closeOtp();
    });

    function showOtpSection() {
        otpInputs.forEach(inp => {
            inp.value = '';
            inp.style.borderColor = '';
            inp.classList.remove('filled');
        });
        showOtpMessage('', '');
        otpModal.classList.add('active');
        otpInputs[0].focus();
    }

    function hideOtpSection() {
        otpModal.classList.remove('active');
    }

    function showOtpMessage(text, type) {
        otpMessageBox.textContent = text;
        otpMessageBox.className = 'message-box' + (type ? ' ' + type : '');
    }

    function activateLight(lightColor, statusText) {
        redLight.classList.remove('active');
        yellowLight.classList.remove('active');
        greenLight.classList.remove('active');

        if (lightColor === 'red') redLight.classList.add('active');
        else if (lightColor === 'yellow') yellowLight.classList.add('active');
        else if (lightColor === 'green') greenLight.classList.add('active');

        lightStatus.textContent = statusText;
    }

    function showMessage(text, type) {
        messageBox.textContent = text;
        messageBox.className = 'message-box' + (type ? ' ' + type : '');
    }

    phoneInput.addEventListener('input', function () { this.style.borderColor = ''; });
    passwordInput.addEventListener('input', function () { this.style.borderColor = ''; });
});
