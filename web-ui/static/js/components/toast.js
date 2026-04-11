import { h } from 'preact';
import htm from 'htm';
import { toasts, removeToast } from '../lib/state.js';

const html = htm.bind(h);

export function ToastContainer() {
    const items = toasts.value;

    if (items.length === 0) return null;

    return html`
        <div class="toast-container">
            ${items.map(toast => html`
                <div
                    key=${toast.id}
                    class=${'toast toast-' + toast.type}
                    onClick=${() => removeToast(toast.id)}
                >
                    ${toast.message}
                </div>
            `)}
        </div>
    `;
}
