import { h } from 'preact';
import htm from 'htm';
import { MarkdownEditor } from './markdown-editor.js';
import { ProjectChips } from './tag-chips.js';

const html = htm.bind(h);

export function EntityForm({ fields = [], values = {}, onChange, onSubmit, onCancel, submitLabel = 'Save' }) {
    function handleSubmit(e) {
        e.preventDefault();
        onSubmit();
    }

    function handleChange(name, value) {
        onChange({ ...values, [name]: value });
    }

    function renderField(field) {
        const { name, label, type = 'text', required, placeholder, readOnly, options = [] } = field;
        const value = values[name] ?? '';

        if (type === 'markdown') {
            return html`
                <div key=${name} class="form-group">
                    <${MarkdownEditor}
                        label=${label}
                        value=${value}
                        onChange=${v => handleChange(name, v)}
                    />
                </div>
            `;
        }

        if (type === 'projects') {
            const projects = Array.isArray(value) ? value : [];
            return html`
                <div key=${name} class="form-group">
                    ${label && html`<label class="form-label">${label}</label>`}
                    <${ProjectChips}
                        projects=${projects}
                        onRemove=${pName => handleChange(name, projects.filter(p => (p.name || p) !== pName))}
                        onAdd=${pName => handleChange(name, [...projects, pName])}
                        readOnly=${readOnly}
                    />
                </div>
            `;
        }

        if (type === 'select') {
            return html`
                <div key=${name} class="form-group">
                    ${label && html`<label class="form-label">${label}${required ? ' *' : ''}</label>`}
                    <select
                        class="form-select"
                        value=${value}
                        onChange=${e => handleChange(name, e.target.value)}
                        disabled=${readOnly}
                        required=${required}
                    >
                        ${options.map(opt => {
                            const optVal = typeof opt === 'object' ? opt.value : opt;
                            const optLabel = typeof opt === 'object' ? opt.label : opt;
                            return html`<option key=${optVal} value=${optVal}>${optLabel}</option>`;
                        })}
                    </select>
                </div>
            `;
        }

        // Default: text input
        return html`
            <div key=${name} class="form-group">
                ${label && html`<label class="form-label">${label}${required ? ' *' : ''}</label>`}
                <input
                    class="form-input"
                    type="text"
                    value=${value}
                    placeholder=${placeholder || ''}
                    onInput=${e => handleChange(name, e.target.value)}
                    readOnly=${readOnly}
                    required=${required}
                />
            </div>
        `;
    }

    return html`
        <form onSubmit=${handleSubmit}>
            ${fields.map(field => renderField(field))}
            <div class="form-actions">
                ${onCancel && html`
                    <button type="button" class="btn btn-secondary" onClick=${onCancel}>Cancel</button>
                `}
                <button type="submit" class="btn btn-primary">${submitLabel}</button>
            </div>
        </form>
    `;
}
