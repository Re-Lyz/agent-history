"""Markdown tokens rendered directly in Tk Text; no HTML engine or resource fetching.

Parser/highlighter imports are lazy so the viewer can still offer raw text when
optional desktop dependencies have not been installed. See requirements-ui.txt.
"""
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Span:
    text: str
    styles: tuple = ()
    href: str = ''


@dataclass(frozen=True)
class Code:
    text: str
    language: str = ''
    styles: tuple = ()


@dataclass(frozen=True)
class Table:
    rows: tuple
    alignments: tuple
    styles: tuple = ()


def available():
    try:
        import markdown_it
        import pygments
        return True
    except ImportError:
        return False


def clickable_url(url):
    """Only explicit browser/mail actions; never treat a URI as a shell command."""
    try:
        parsed = urlsplit(url)
        if any(ord(char) < 32 for char in url):
            return False
        return (parsed.scheme.lower() in ('http', 'https') and bool(parsed.hostname)) or \
            (parsed.scheme.lower() == 'mailto' and bool(parsed.path))
    except ValueError:
        return False


def inline_spans(tokens, base=()):
    spans, styles, links = [], list(base), []
    names = {'strong': 'bold', 'em': 'italic', 's': 'strike'}
    for token in tokens or ():
        kind = token.type
        if kind.endswith('_open') and kind[:-5] in names:
            styles.append(names[kind[:-5]])
        elif kind.endswith('_close') and kind[:-6] in names:
            styles.pop()
        elif kind == 'link_open':
            links.append(token.attrGet('href') or '')
        elif kind == 'link_close':
            links.pop()
        elif kind in ('text', 'html_inline', 'code_inline'):
            extra = ('inline_code',) if kind == 'code_inline' else ()
            spans.append(Span(token.content, tuple(styles) + extra, links[-1] if links else ''))
        elif kind in ('softbreak', 'hardbreak'):
            spans.append(Span('\n', tuple(styles)))
        elif kind == 'image':
            spans.append(Span(f'［图片：{token.content or "无说明"} · 未加载］', tuple(styles) + ('image',)))
    return tuple(spans)


def _parse(source):
    from markdown_it import MarkdownIt
    parser = MarkdownIt('commonmark', dict(html=False, linkify=False)).enable(['table', 'strikethrough'])
    tokens = parser.parse(source)
    output, styles, lists = [], [], []
    quote_depth, index = 0, 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == 'heading_open':
            styles.append(token.tag)
        elif kind == 'heading_close':
            styles.pop()
            output.append(Span('\n\n'))
        elif kind == 'blockquote_open':
            quote_depth += 1
            styles.append(f'quote_{min(quote_depth, 6)}')
        elif kind == 'blockquote_close':
            quote_depth -= 1
            styles.pop()
        elif kind in ('bullet_list_open', 'ordered_list_open'):
            lists.append([kind.startswith('ordered'), int(token.attrGet('start') or 1)])
        elif kind in ('bullet_list_close', 'ordered_list_close'):
            lists.pop()
            if not lists:
                output.append(Span('\n'))
        elif kind == 'list_item_open':
            styles.append(f'list_{min(len(lists), 6)}')
            ordered, number = lists[-1]
            lists[-1][1] += 1
            output.append(Span(f'{number}. ' if ordered else '• ', tuple(styles)))
        elif kind == 'list_item_close':
            styles.pop()
        elif kind == 'paragraph_close':
            output.append(Span('\n' if token.hidden else '\n\n', tuple(styles)))
        elif kind == 'inline':
            output.extend(inline_spans(token.children, tuple(styles)))
        elif kind in ('fence', 'code_block'):
            output.append(Code(token.content, token.info.strip().split()[0] if token.info.strip() else '', tuple(styles)))
        elif kind == 'hr':
            output.append(Span('────────────────────────\n\n', ('rule',)))
        elif kind == 'table_open':
            rows, row, alignments = [], [], []
            index += 1
            while tokens[index].type != 'table_close':
                cell = tokens[index]
                if cell.type == 'tr_open':
                    row = []
                elif cell.type == 'inline':
                    row.append(inline_spans(cell.children))
                elif cell.type == 'tr_close':
                    rows.append(tuple(row))
                elif cell.type == 'th_open':
                    alignments.append((cell.attrGet('style') or 'text-align:left').split(':')[-1])
                index += 1
            output.append(Table(tuple(rows), tuple(alignments), tuple(styles)))
        elif kind == 'html_block':
            output.append(Span(token.content, tuple(styles)))
        index += 1
    return tuple(output)


_cached_parse = lru_cache(maxsize=128)(_parse)


def parse(source):
    # Bound retained source size as well as message count; huge blocks remain readable.
    return _cached_parse(source) if len(source) <= 200_000 else _parse(source)


def highlighted(code, language):
    """Preserve exact code text; unknown languages and very large blocks stay plain."""
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound
    from pygments.token import Comment, Keyword, String, Number, Name
    if not language or len(code) > 200_000:
        return ((code, ''),)
    try:
        lexer = get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return ((code, ''),)
    spans = []
    for _, token, text in lexer.get_tokens_unprocessed(code):
        style = 'comment' if token in Comment else 'keyword' if token in Keyword else \
            'string' if token in String else 'number' if token in Number else \
            'function' if token in Name.Function else ''
        spans.append((text, style))
    return tuple(spans) if ''.join(text for text, _ in spans) == code else ((code, ''),)


class TkRenderer:
    """Keeps selectable/searchable text, with copy buttons only as embedded controls."""
    def __init__(self, text, *, palette, font, copy_code, open_link):
        from tkinter import font as tkfont
        self.text, self.palette, self.font = text, palette, font
        self.copy_code, self.open_link = copy_code, open_link
        self.widgets, self.dynamic_tags, self.commands = [], [], []
        self.font_tags = {}
        self.table_font = tkfont.Font(root=text, family='Consolas', size=10, weight='bold')
        text.tag_configure('md_quote', foreground=palette['muted'])
        text.tag_configure('md_code', background=palette['code_bg'], wrap='none', spacing1=0, spacing3=0)
        text.tag_configure('md_inline_code', background=palette['code_bg'])
        text.tag_configure('md_strike', overstrike=True)
        text.tag_configure('md_link', foreground=palette['user'], underline=True)
        text.tag_configure('md_rule', foreground=palette['line'])
        text.tag_configure('md_image', foreground=palette['muted'])
        text.tag_configure('md_table', wrap='none', background=palette['code_bg'], spacing1=3, spacing3=3)
        text.tag_configure('md_table_header', background=palette['accent_soft'])
        for name, color in dict(comment=palette['muted'], keyword='#7663a1', string='#557953',
                                number='#b0754c', function=palette['user']).items():
            text.tag_configure('md_syntax_' + name, foreground=color)
        for depth in range(1, 7):
            text.tag_configure(f'md_quote_{depth}', lmargin1=depth * 14, lmargin2=depth * 14, rmargin=8)
            text.tag_configure(f'md_list_{depth}', lmargin1=(depth - 1) * 20, lmargin2=(depth - 1) * 20 + 22)

    def reset(self):
        self.text.configure(cursor='xterm')
        for widget in self.widgets:
            widget.destroy()
        self.widgets.clear()
        for tag in self.dynamic_tags:
            self.text.tag_delete(tag)
        self.dynamic_tags.clear()
        for command in self.commands:
            self.text.deletecommand(command)
        self.commands.clear()

    def font_tag(self, styles):
        styles = tuple(sorted(set(styles)))
        if styles not in self.font_tags:
            header = next((name for name in styles if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')), '')
            family = 'Consolas' if any(name in styles for name in ('code', 'table', 'inline_code')) else self.font
            size = dict(h1=17, h2=15, h3=13, h4=11, h5=10, h6=10).get(header, 10)
            tag = f'md_font_{len(self.font_tags)}'
            self.text.tag_configure(tag, font=(family, size,
                'bold' if 'bold' in styles or header or 'table_header' in styles else 'normal',
                'italic' if 'italic' in styles else 'roman'))
            self.font_tags[styles] = tag
        return self.font_tags[styles]

    def insert(self, span, extra=()):
        styles = span.styles + extra
        tags = ['body']
        for name in styles:
            if name.startswith('quote_'):
                tags += ['md_quote', 'md_' + name]
            elif name.startswith('list_'):
                tags.append('md_' + name)
            elif name in ('inline_code', 'strike', 'rule', 'image', 'code', 'table', 'table_header'):
                tags.append('md_' + name)
        tags.append(self.font_tag(styles))
        if span.href and clickable_url(span.href):
            tag = f'md_href_{len(self.dynamic_tags)}'
            self.dynamic_tags.append(tag)
            self.commands.append(self.text.tag_bind(tag, '<Button-1>', lambda event, href=span.href: self.open_link(href)))
            self.commands.append(self.text.tag_bind(tag, '<Enter>', lambda event: self.text.configure(cursor='hand2')))
            self.commands.append(self.text.tag_bind(tag, '<Leave>', lambda event: self.text.configure(cursor='xterm')))
            tags += ['md_link', tag]
        self.text.insert('end', span.text, tuple(tags))

    def render(self, source):
        for _ in self.iter_render(source):
            pass

    def iter_render(self, source, elements=None, highlights=None):
        from tkinter import ttk
        for element in parse(source) if elements is None else elements:
            if isinstance(element, Span):
                self.insert(element)
            elif isinstance(element, Code):
                self.insert(Span((element.language or '代码') + '  ', element.styles + ('code',)))
                button = ttk.Button(self.text, text='复制代码', style='Soft.TButton',
                                    command=lambda code=element.text: self.copy_code(code))
                self.widgets.append(button)
                self.text.window_create('end', window=button, padx=4)
                self.insert(Span('\n', element.styles + ('code',)))
                tokens = highlights.get((element.text, element.language)) if highlights is not None else None
                for value, name in tokens if tokens is not None else highlighted(element.text, element.language):
                    start = self.text.index('end-1c')
                    self.insert(Span(value, element.styles + ('code',)))
                    if name:
                        self.text.tag_add('md_syntax_' + name, start, 'end-1c')
                    yield
                if element.text and not element.text.endswith('\n'):
                    self.insert(Span('\n', element.styles + ('code',)))
                self.insert(Span('\n'))
            else:
                self.render_table(element)
            yield

    def render_table(self, table):
        widths = [max(self.table_font.measure(''.join(span.text for span in row[index]))
                      for row in table.rows) + 24 for index in range(len(table.alignments))]
        tag = f'md_columns_{len(self.dynamic_tags)}'
        self.dynamic_tags.append(tag)
        stops, offset = [], 0
        for index, width in enumerate(widths):
            alignment = table.alignments[index]
            stops += [offset + (width - 12 if alignment == 'right' else width // 2 if alignment == 'center' else 8), alignment]
            offset += width
        self.text.tag_configure(tag, tabs=tuple(stops), tabstyle='wordprocessor')
        for row_index, row in enumerate(table.rows):
            start = self.text.index('end-1c')
            for cell in row:
                self.insert(Span('\t', table.styles + ('table',)))
                for span in cell:
                    self.insert(span, table.styles + ('table',) + (('table_header',) if row_index == 0 else ()))
            self.insert(Span('\n', table.styles + ('table',)))
            self.text.tag_add(tag, start, 'end-1c')
        self.insert(Span('\n'))
