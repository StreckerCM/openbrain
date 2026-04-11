import { h } from 'preact';
import htm from 'htm';

const html = htm.bind(h);

export function Modal({ title, children, onClose, actions }) {
    function stopPropagation(e) {
        e.stopPropagation();
    }

    return html`
        <div class="modal-backdrop" onClick=${onClose}>
            <div class="modal" onClick=${stopPropagation}>
                ${title && html`<div class="modal-title">${title}</div>`}
                <div class="modal-body">${children}</div>
                ${actions && html`<div class="modal-actions">${actions}</div>`}
            </div>
        </div>
    `;
}

export function ConfirmModal({ title, message, confirmLabel = 'Confirm', confirmClass = 'btn btn-danger', onConfirm, onCancel }) {
    const actions = html`
        <>
            <button class="btn btn-secondary" onClick=${onCancel}>Cancel</button>
            <button class=${confirmClass} onClick=${onConfirm}>${confirmLabel}</button>
        </>
    `;

    return html`
        <${Modal} title=${title} onClose=${onCancel} actions=${actions}>
            ${message}
        </${Modal}>
    `;
}
