"""Regression coverage for the self-hosted Markdown/KaTeX pipeline."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class TestFrontendMath(unittest.TestCase):
    def test_katex_assets_are_local_and_all_referenced_fonts_exist(self):
        index = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="katex/katex-swap.min.css"', index)
        self.assertIn('src="katex/katex.min.js"', index)
        self.assertIn('src="math-renderer.js"', index)
        self.assertNotIn("cdn.jsdelivr.net/npm/katex", index)

        css = (STATIC / "katex" / "katex-swap.min.css").read_text(
            encoding="utf-8"
        )
        font_paths = set(re.findall(r"url\((fonts/[^)]+)\)", css))
        self.assertTrue(font_paths)
        for relative_path in font_paths:
            self.assertTrue(
                (STATIC / "katex" / relative_path).is_file(),
                f"KaTeX font is missing: {relative_path}",
            )

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_markdown_math_delimiters_code_and_currency(self):
        script = r"""
const assert = require('node:assert/strict');
const marked = require('./static/marked.min.js');
const katex = require('./static/katex/katex.min.js');
const {createRenderer} = require('./static/math-renderer.js');
const render = createRenderer(marked, katex);
const sanitize = html => html.replace(/<script[\s\S]*?<\/script>/gi, '');

const inline = render('勾股定理：$a^2+b^2=c^2$', sanitize);
assert.match(inline, /class="katex"/);

const display = render('前文\n$$\n\\frac{a_1}{b^2}\n$$\n后文', sanitize);
assert.match(display, /class="katex-display"/);
assert.doesNotMatch(display, /<h1>/);

const explicit = render('支持 \\(x+y\\) 与 \\[x-y\\]', sanitize);
assert.equal((explicit.match(/class="katex"/g) || []).length, 2);

const code = render('`$x_1$`\n\n```tex\n$$x^2$$\n```', sanitize);
assert.match(code, /<code>\$x_1\$<\/code>/);
assert.doesNotMatch(code, /class="katex"/);
assert.doesNotMatch(render('价格 \\$5，折后 \\$10', sanitize), /class="katex"/);
assert.doesNotMatch(render('价格 $5 and $10', sanitize), /class="katex"/);

const secure = render('$\\href{javascript:alert(1)}{x}$', sanitize);
assert.doesNotMatch(secure, /<a\b|href=/i);
"""
        completed = subprocess.run(
            [shutil.which("node"), "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
