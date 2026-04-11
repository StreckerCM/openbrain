import { signal } from '@preact/signals';

export const currentRoute = signal(window.location.hash || '#/');
export const sidebarOpen = signal(false);
export const toasts = signal([]);

window.addEventListener('hashchange', () => {
    currentRoute.value = window.location.hash || '#/';
});

export function navigate(hash) {
    window.location.hash = hash;
}

let toastId = 0;
export function addToast(message, type = 'error') {
    const id = ++toastId;
    toasts.value = [...toasts.value, { id, message, type }];
    setTimeout(() => removeToast(id), 5000);
}

export function removeToast(id) {
    toasts.value = toasts.value.filter(t => t.id !== id);
}
