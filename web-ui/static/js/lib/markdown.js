import '/vendor/marked.min.js';

const { marked } = window;

marked.setOptions({
    breaks: true,
    gfm: true,
});

export function renderMarkdown(text) {
    if (!text) return '';
    return marked.parse(text);
}
