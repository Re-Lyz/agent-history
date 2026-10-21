"""Markdown parsing and code preservation, without real history or network access."""
import unittest
from unittest.mock import patch

import markdown_preview as md


@unittest.skipUnless(md.available(), 'Install requirements-ui.txt to verify Markdown rendering.')
class MarkdownTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
