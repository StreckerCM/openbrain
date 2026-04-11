import '/vendor/marked.min.js';
import '/vendor/purify.min.js';

const { marked } = window;
const DOMPurify = window.DOMPurify;

marked.setOptions({
    breaks: true,
    gfm: true,
});

export function renderMarkdown(text) {
    if (!text) return '';
    return DOMPurify.sanitize(marked.parse(text));
}
