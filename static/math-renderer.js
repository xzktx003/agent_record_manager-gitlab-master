/* Markdown-aware KaTeX rendering for Work Memory. */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.WorkMemoryMath = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const blockDelimiters = [
        {left: '$$', right: '$$', display: true},
        {left: '\\[', right: '\\]', display: true},
    ];
    const inlineDelimiters = [
        ...blockDelimiters,
        {left: '\\(', right: '\\)', display: false},
        {left: '$', right: '$', display: false},
    ];
    let renderSequence = 0;

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // Keep delimiters inside balanced braces from ending a formula early.
    function findEndOfMath(delimiter, text, startIndex) {
        let index = startIndex;
        let braceLevel = 0;
        while (index < text.length) {
            const character = text[index];
            if (braceLevel <= 0 && text.startsWith(delimiter, index)) return index;
            if (character === '\\') index += 1;
            else if (character === '{') braceLevel += 1;
            else if (character === '}') braceLevel -= 1;
            index += 1;
        }
        return -1;
    }

    function findInlineDollarEnd(text, startIndex) {
        let searchFrom = startIndex;
        while (searchFrom < text.length) {
            const index = findEndOfMath('$', text, searchFrom);
            if (index < 0) return -1;
            const before = text[index - 1] || '';
            const after = text[index + 1] || '';
            // These CommonMark-style boundaries avoid treating "$5 and $10"
            // as a formula while retaining normal forms such as "$x_1$".
            if (!/\s/.test(before) && !/\d/.test(after)) return index;
            searchFrom = index + 1;
        }
        return -1;
    }

    function earliestDelimiter(src, delimiters) {
        let earliest = -1;
        for (const delimiter of delimiters) {
            const index = src.indexOf(delimiter.left);
            if (index >= 0 && (earliest < 0 || index < earliest)) earliest = index;
        }
        return earliest < 0 ? undefined : earliest;
    }

    function findBlockStart(src) {
        const candidate = /(^|\n)(\$\$|\\\[)/g;
        let match;
        while ((match = candidate.exec(src))) {
            const index = match.index + match[1].length;
            const delimiter = blockDelimiters.find(item => src.startsWith(item.left, index));
            if (!delimiter) continue;
            const end = findEndOfMath(
                delimiter.right,
                src,
                index + delimiter.left.length,
            );
            if (end < 0) continue;
            const remaining = src.slice(end + delimiter.right.length);
            if (/^[ \t]*(?:\n|$)/.test(remaining)) return index;
        }
        return undefined;
    }

    function tokenizeMath(src, delimiters, type, block) {
        for (const delimiter of delimiters) {
            if (!src.startsWith(delimiter.left)) continue;
            if (delimiter.left === '$' && /\s/.test(src[1] || '')) return undefined;
            const start = delimiter.left.length;
            const end = delimiter.left === '$'
                ? findInlineDollarEnd(src, start)
                : findEndOfMath(delimiter.right, src, start);
            if (end < 0) return undefined;
            const source = src.slice(0, end + delimiter.right.length);
            let raw = source;
            if (block) {
                const lineEnd = src.slice(source.length).match(/^[ \t]*(?:\n|$)/);
                if (!lineEnd) return undefined;
                raw += lineEnd[0];
            }
            return {
                type,
                raw,
                source,
                math: src.slice(start, end),
                display: delimiter.display,
            };
        }
        return undefined;
    }

    function renderFormula(katex, token) {
        try {
            return katex.renderToString(token.math, {
                displayMode: token.display,
                output: 'htmlAndMathml',
                throwOnError: false,
                trust: false,
                strict: 'warn',
            });
        } catch (error) {
            if (typeof console !== 'undefined' && console.warn) {
                console.warn('KaTeX 渲染失败：', error);
            }
            return escapeHtml(token.source);
        }
    }

    function createRenderer(marked, katex) {
        if (!marked || typeof marked.Marked !== 'function') {
            throw new Error('Marked is required for Markdown rendering.');
        }
        if (!katex || typeof katex.renderToString !== 'function') {
            throw new Error('KaTeX is required for LaTeX rendering.');
        }

        return function renderMarkdown(text, sanitizeHtml) {
            if (typeof sanitizeHtml !== 'function') {
                throw new Error('A sanitizer is required before rendering formulas.');
            }
            const formulas = [];
            const salt = [
                Date.now().toString(36),
                (++renderSequence).toString(36),
                Math.random().toString(36).slice(2),
            ].join('');
            const stash = token => {
                const marker = `WMMATHTOKEN${salt}X${formulas.length}END`;
                formulas.push({...token, marker});
                return marker;
            };
            const parser = new marked.Marked({
                extensions: [
                    {
                        name: 'workMemoryMathBlock',
                        level: 'block',
                        start: findBlockStart,
                        tokenizer(src) {
                            return tokenizeMath(
                                src,
                                blockDelimiters,
                                'workMemoryMathBlock',
                                true,
                            );
                        },
                        renderer: token => stash(token) + '\n',
                    },
                    {
                        name: 'workMemoryMathInline',
                        level: 'inline',
                        start: src => earliestDelimiter(src, inlineDelimiters),
                        tokenizer(src) {
                            return tokenizeMath(
                                src,
                                inlineDelimiters,
                                'workMemoryMathInline',
                                false,
                            );
                        },
                        renderer: stash,
                    },
                ],
            });
            let html = String(sanitizeHtml(parser.parse(String(text == null ? '' : text))) || '');
            for (const formula of formulas) {
                html = html.split(formula.marker).join(renderFormula(katex, formula));
            }
            return html;
        };
    }

    return {createRenderer};
});
