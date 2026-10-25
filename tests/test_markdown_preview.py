"""Markdown parsing and code preservation, without real history or network access."""
import unittest
import importlib.util
from unittest.mock import patch

import markdown_preview as md


@unittest.skipUnless(md.available(), 'Install requirements-ui.txt to verify Markdown rendering.')
class MarkdownTests(unittest.TestCase):
    def test_cjk_emphasis_punctuation_nested_styles_links_and_tables(self):
        source = '前文**“重点”**后文，**加粗：**文字，*（斜体）*文字，***「混合」***结束。\n\n' \
                 '**[本地说明](file:///C:/demo/a.md)**和 **`x_y`**\n\n' \
                 '| 内容 |\n| --- |\n| 前**（加粗）**后 |'
        elements = md.parse(source)
        spans = [e for e in elements if isinstance(e, md.Span)]
        self.assertTrue(any(s.text == '“重点”' and 'bold' in s.styles for s in spans))
        self.assertTrue(any(s.text == '加粗：' and 'bold' in s.styles for s in spans))
        self.assertTrue(any(s.text == '（斜体）' and 'italic' in s.styles for s in spans))
        self.assertTrue(any(s.text == '「混合」' and {'bold', 'italic'} <= set(s.styles) for s in spans))
        self.assertTrue(any(s.href and 'bold' in s.styles for s in spans))
        self.assertTrue(any(s.text == 'x_y' and {'bold', 'inline_code'} <= set(s.styles) for s in spans))
        table = next(e for e in elements if isinstance(e, md.Table))
        self.assertTrue(any(s.text == '（加粗）' and 'bold' in s.styles for s in table.rows[1][0]))
        # The extension is parser-scoped; raw escapes, code and English rules survive.
        from markdown_it import MarkdownIt
        self.assertIn('**“重点”**', MarkdownIt().render('前文**“重点”**后文'))
        rendered = md.parse(r'\*\*原样\*\* `**代码**` snake_case_name **未闭合')
        self.assertFalse(any('bold' in s.styles for s in rendered if isinstance(s, md.Span)))

    def test_math_dollars_brackets_fences_and_table_cells(self):
        source = r'公式 $x^2$ 和 \(\frac{1}{2}\)，**$\alpha$**。' + '\n\n' \
                 '$$\n\\sum_{i=1}^n i\n$$\n\n' \
                 '\\[\n\\begin{pmatrix}a&b\\\\c&d\\end{pmatrix}\n\\]\n\n' \
                 '```math\n\\sqrt{x}\n```\n\n| 值 |\n| --- |\n| $x_1$ |'
        elements = md.parse(source)
        math = [e for e in elements if isinstance(e, md.Formula)]
        self.assertEqual([e.display for e in math], [False, False, False, True, True, True])
        self.assertEqual(math[0].text, '$x^2$')
        self.assertEqual(math[1].text, r'\(\frac{1}{2}\)')
        self.assertIn('bold', math[2].styles)
        self.assertIn(r'\begin{pmatrix}', math[4].latex)
        self.assertEqual(math[5].latex, '\\sqrt{x}\n')
        table = next(e for e in elements if isinstance(e, md.Table))
        self.assertIsInstance(table.rows[1][0][0], md.Formula)

    def test_math_does_not_consume_currency_code_escapes_or_trailing_text(self):
        source = r'价格 $5 和 $10，\$20；`$x$`；未闭合 $x；\(未闭合' + '\n\n' \
                 '```python\nvalue = "$x$"  # **code**\n```'
        self.assertFalse(any(isinstance(e, md.Formula) for e in md.parse(source)))
        self.assertEqual(next(e for e in md.parse(source) if isinstance(e, md.Code)).text,
                         'value = "$x$"  # **code**\n')
        for source in (r'\[x^2\] 后文 **粗体**', r'前文 \[x^2\] 后文',
                       '\\[\nx^2\n\\] 后文', '> \\[\nx^2\n\\] 后文'):
            elements = md.parse(source)
            self.assertIn('后文', ''.join(e.text for e in elements if isinstance(e, md.Span)))
        elements = md.parse('\\[\nx^2\n\ny^2\n\\]\n\n后文')
        self.assertEqual(len([e for e in elements if isinstance(e, md.Formula)]), 1)
        self.assertIn('后文', ''.join(e.text for e in elements if isinstance(e, md.Span)))
        math = next(e for e in md.parse('\\[x^2\\]   \n') if isinstance(e, md.Formula))
        self.assertEqual(math.text, r'\[x^2\]')
        escaped = next(e for e in md.parse('\\[x\\\\] + y\\]\n') if isinstance(e, md.Formula))
        self.assertEqual(escaped.latex, 'x\\\\] + y')
        for source in ('~~~LaTeX\nx^2\n~~~', '```tex\nx^2\n```', '> ```math\nx^2\n> ```'):
            self.assertTrue(md.formulas.might_have_math(source))

    def test_headings_nested_lists_quote_and_inline_styles(self):
        source = '# 标题 **粗体**\n\n> 引用 *强调*\n>\n> - 项目 `code`\n>   - 子项\n\n3. 第三项\n4. 第四项\n\n~~删除~~'
        spans = [element for element in md.parse(source) if isinstance(element, md.Span)]
        rendered = ''.join(span.text for span in spans)
        self.assertIn('标题 粗体', rendered)
        self.assertNotIn('**', rendered)
        self.assertIn('3. 第三项', rendered)
        self.assertIn('4. 第四项', rendered)
        self.assertTrue(any('h1' in span.styles and 'bold' in span.styles for span in spans))
        self.assertTrue(any('quote_1' in span.styles and 'inline_code' in span.styles for span in spans))
        self.assertTrue(any('list_2' in span.styles for span in spans))
        self.assertTrue(any('strike' in span.styles for span in spans))

    def test_table_preserves_chinese_alignment_links_and_escaped_pipes(self):
        elements = md.parse('| 中文 | 数值 | 链接 |\n| :--- | ---: | :---: |\n| 值\\|二 | **42** | [官网](https://example.com) |')
        table = next(element for element in elements if isinstance(element, md.Table))
        self.assertEqual(table.alignments, ('left', 'right', 'center'))
        self.assertEqual([''.join(span.text for span in cell) for cell in table.rows[1]], ['值|二', '42', '官网'])
        self.assertEqual(table.rows[1][2][0].href, 'https://example.com')

    def test_fences_keep_exact_code_and_do_not_leak_across_messages(self):
        code = 'def hello():\n    return "你好"\n'
        first = md.parse('```python\n' + code)  # unclosed fence
        self.assertEqual(next(element for element in first if isinstance(element, md.Code)).text, code)
        second = md.parse('## 下一条\n\n正常 **文字**')
        self.assertFalse(any(isinstance(element, md.Code) for element in second))
        self.assertTrue(any(isinstance(element, md.Span) and 'h2' in element.styles for element in second))
        indented = md.parse('    x = 1\n    y = 2\n')
        self.assertEqual(next(element for element in indented if isinstance(element, md.Code)).text, 'x = 1\ny = 2\n')

    def test_local_links_preserve_labels_and_decode_native_path_forms(self):
        source = '[说明](file:///C:/demo/report.md:12) [代码](/C:/demo/app.py:8) ' \
                 '[相对路径](<../docs/local note.md>) [反斜杠](<C:\\demo\\app.py:3>)'
        spans = [span for span in md.parse(source) if isinstance(span, md.Span) and span.href]
        self.assertEqual([span.text for span in spans], ['说明', '代码', '相对路径', '反斜杠'])
        self.assertEqual([md.local_path(span.href) for span in spans],
                         ['C:/demo/report.md', 'C:/demo/app.py', '../docs/local note.md', 'C:\\demo\\app.py'])
        self.assertEqual(md.local_path('/tmp/demo/report.md:4:2'), '/tmp/demo/report.md')
        self.assertEqual(md.local_path('file:///tmp/local%20note.md#L4'), '/tmp/local note.md')
        for value in ('javascript:alert(1)', 'https://example.com', 'file://remote-host/private/file.md',
                      '//remote-host/file.md', 'file:///tmp/bad%00name', '#section'):
            self.assertIsNone(md.local_path(value))

    def test_paragraphs_use_one_line_break_without_changing_source(self):
        source = '# Heading\n\nFirst paragraph.\n\nSecond paragraph.'
        spans = [span for span in md.parse(source) if isinstance(span, md.Span)]
        self.assertEqual(''.join(span.text for span in spans), 'Heading\nFirst paragraph.\nSecond paragraph.\n')
        self.assertEqual(sum('paragraph_end' in span.styles for span in spans), 3)

    def test_highlighting_never_changes_code_or_adds_a_newline(self):
        for language, code in (('python', 'def f():\n    return "中文"'), ('json', '{"n": 12}'),
                               ('powershell', 'Write-Output "中文"'), ('not-a-language', 'x\t= 1\n')):
            spans = md.highlighted(code, language)
            self.assertEqual(''.join(value for value, _ in spans), code)
        self.assertIn('keyword', {style for _, style in md.highlighted('def f(): pass', 'python')})
        large = 'x = 1\n' * 40_000
        self.assertEqual(md.highlighted(large, 'python'), ((large, ''),))

    def test_html_scripts_images_and_unsafe_links_stay_inert(self):
        source = '<script>alert(1)</script>\n\n![示意](https://example.com/image.png)\n\n' \
                 '[危险](javascript:alert(1)) [本地](file:///C:/program.exe) [网页](https://example.com)'
        with patch('urllib.request.urlopen') as fetch, patch('webbrowser.open') as browser:
            spans = [element for element in md.parse(source) if isinstance(element, md.Span)]
        rendered = ''.join(span.text for span in spans)
        self.assertIn('<script>alert(1)</script>', rendered)
        self.assertIn('图片：示意 · 未加载', rendered)
        self.assertEqual([span.href for span in spans if md.clickable_url(span.href)], ['https://example.com'])
        fetch.assert_not_called()
        browser.assert_not_called()
        for url in ('javascript:alert(1)', 'file:///C:/program.exe', 'data:text/html,x', '//example.com',
                    '#section', 'https://example.com\nmalicious', 'https:///missing-host'):
            self.assertFalse(md.clickable_url(url))
        self.assertTrue(md.clickable_url('mailto:user@example.com'))


@unittest.skipUnless(importlib.util.find_spec('ziamath') and importlib.util.find_spec('resvg_py'),
                     'Install requirements-ui.txt for offline formula verification.')
class FormulaTests(unittest.TestCase):
    def test_offline_fractions_matrices_alignment_and_styles_produce_png(self):
        from history_app.ui import formulas as f
        samples = (r'\frac{-b\pm\sqrt{b^2-4ac}}{2a}', r'\sum_{i=1}^n i^2',
                   r'\begin{pmatrix}a&b\\c&d\end{pmatrix}',
                   r'\begin{aligned}a&=b+c\\d&=e\end{aligned}', r'\text{loss}=\alpha x^2')
        with patch('urllib.request.urlopen') as fetch, patch('webbrowser.open') as browser:
            for latex in samples:
                with self.subTest(latex=latex):
                    formula = f.Formula('$$' + latex + '$$', latex, True)
                    image = f.render(formula)
                    self.assertFalse(image.error)
                    self.assertTrue(image.png.startswith(b'\x89PNG\r\n\x1a\n'))
                    self.assertGreater(image.width * image.height, 0)
                    self.assertIs(f.render(formula), image)
            bold = f.render(f.Formula('$x$', 'x', styles=('bold',)))
            self.assertTrue(bold.png)
        fetch.assert_not_called()
        browser.assert_not_called()

    def test_unsupported_oversized_or_missing_backend_keeps_fallback(self):
        from history_app.ui import formulas as f
        for latex in ('x' * (f.MAX_SOURCE + 1), '{' * 80 + 'x' + '}' * 80, r'\frac{1}', r'\text{中文}'):
            with self.subTest(latex=latex[:20]):
                result = f.render(f.Formula(latex, latex))
                self.assertFalse(result.png)
                self.assertTrue(result.error)
        with patch.object(f, '_render', side_effect=ImportError):
            result = f.render(f.Formula('$missing_backend$', 'missing_backend'))
            self.assertIn('依赖未安装', result.error)

    def test_external_svg_and_huge_dimensions_never_reach_rasterizer(self):
        from history_app.ui import formulas as f
        from types import SimpleNamespace
        samples = ('<svg width="10" height="10"><image href="https://example.com/x"/></svg>',
                   '<svg width="10" height="10"><use href="file:///private"/></svg>',
                   '<svg width="100000" height="100000"/>')
        for index, svg in enumerate(samples):
            with patch('ziamath.Latex', return_value=SimpleNamespace(svg=lambda: svg)), \
                    patch('resvg_py.svg_to_bytes') as rasterize:
                result = f.render(f.Formula(str(index), 'svg_reject_' + str(index)))
                self.assertFalse(result.png)
                self.assertTrue(result.error)
                rasterize.assert_not_called()


if __name__ == '__main__':
    unittest.main()
