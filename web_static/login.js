const loginForm = document.getElementById('loginForm');
const passwordInput = document.getElementById('passwordInput');
const loginButton = document.getElementById('loginButton');
const loginError = document.getElementById('loginError');
const togglePassword = document.getElementById('togglePassword');

togglePassword.addEventListener('click', () => {
    const type = passwordInput.getAttribute('type') === 'password' ? 'text' : 'password';
    passwordInput.setAttribute('type', type);
    togglePassword.textContent = type === 'password' ? 'Göster' : 'Gizle';
});

loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = passwordInput.value.trim();
    
    if (!password) {
        showError('Lütfen şifre girin');
        return;
    }

    loginButton.disabled = true;
    loginButton.textContent = 'Giriş yapılıyor...';
    loginError.textContent = '';

    try {
        const response = await fetch('/api/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ password })
        });

        const data = await response.json();

        if (response.ok && data.ok) {
            loginButton.textContent = 'Başarılı! Yönlendiriliyor...';
            loginButton.style.background = '#10b981'; // Green success color
            loginButton.style.color = '#fff';
            setTimeout(() => {
                window.location.href = '/';
            }, 800);
        } else {
            showError(data.error || 'Şifre hatalı');
        }
    } catch (err) {
        showError('Bağlantı hatası oluştu');
    } finally {
        if (loginButton.textContent !== 'Başarılı! Yönlendiriliyor...') {
            loginButton.disabled = false;
            loginButton.textContent = 'Giriş yap';
        }
    }
});

function showError(msg) {
    loginError.textContent = msg;
    loginError.style.animation = 'none';
    loginError.offsetHeight; /* trigger reflow */
    loginError.style.animation = 'shake 0.4s ease-in-out';
}

// Add shake animation to the stylesheet dynamically for the error message
const style = document.createElement('style');
style.textContent = `
@keyframes shake {
    0%, 100% { transform: translateX(0); }
    25% { transform: translateX(-5px); }
    50% { transform: translateX(5px); }
    75% { transform: translateX(-5px); }
}
`;
document.head.appendChild(style);
