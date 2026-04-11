import { h } from 'preact';
import { useState } from 'preact/hooks';
import htm from 'htm';
import { renderMarkdown } from '../lib/markdown.js';

const html = htm.bind(h);

export function MarkdownEditor({ value = '', onChange, label }) {
    const [mode, setMode] = useState('write');

    return html`
        <div>
            ${label && html`<label class="form-label">${label}</label>`}
            <div class="md-toggle" style="margin-bottom:6px;">
                <button
                    type="button"
                    class=${'md-toggle-btn' + (mode === 'write' ? ' active' : '')}
                    onClick=${() => setMode('write')}
                >
                    Write
                </button>
                <button
                    type="button"
                    class=${'md-toggle-btn' + (mode === 'preview' ? ' active' : '')}
                    onClick=${() => setMode('preview')}
                >
                    Preview
                </button>
            </div>
            ${mode === 'write'
                ? html`
                    <textarea
                        class="form-textarea"
                        value=${value}
                        onInput=${e => onChange(e.target.value)}
                        placeholder="Write markdown here..."
                    ></textarea>
                `
                : html`
                    <div
                        class="md-content"
                        dangerouslySetInnerHTML=${{ __html: renderMarkdown(value) }}
                    ></div>
                `
            }
        </div>
    `;
}
