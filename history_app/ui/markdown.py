"""Markdown tokens rendered directly in Tk Text; no HTML engine or resource fetching.

Parser/highlighter imports are lazy so the viewer can still offer raw text when
optional desktop dependencies have not been installed. See requirements-ui.txt.
"""
from dataclasses import dataclass
from functools import lru_cache
import re
from urllib.parse import unquote, urlsplit
from . import formulas
from .formulas import Formula


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
        import mdit_py_cjk_friendly
        import mdit_py_plugins.dollarmath
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


def local_path(url):
    """Decode a local Markdown target, without touching files or executing it."""
    if not url or any(ord(char) < 32 for char in url):
        return None
    try:
        decoded = unquote(url.split('#', 1)[0].split('?', 1)[0])
        if re.match(r'^[A-Za-z]:[\\/]', decoded):
            value = decoded
        else:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in ('', 'file') or parsed.netloc not in ('', 'localhost'):
                return None
            value = unquote(parsed.path)
        if not value or any(ord(char) < 32 for char in value) or value.startswith(('//', '\\\\')):
            return None
        if re.match(r'^/[A-Za-z]:[\\/]', value):
            value = value[1:]
        return re.sub(r':\d+(?::\d+)?$', '', value)
    except ValueError:
        return None


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
        elif kind in ('math_inline', 'math_inline_double'):
            spans.append(formulas.from_token(token, tuple(styles), links[-1] if links else ''))
    return tuple(spans)


def _parse(source):
    from markdown_it import MarkdownIt
    parser = MarkdownIt('commonmark', dict(html=False, linkify=False)).enable(['table', 'strikethrough'])
    formulas.configure_parser(parser)
    validate = parser.validateLink
    parser.validateLink = lambda target: validate(target) or local_path(target) is not None
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
            output.append(Span('\n', ('paragraph_end',)))
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
            output.append(Span('\n', tuple(styles) + (() if token.hidden else ('paragraph_end',))))
        elif kind == 'inline':
            output.extend(inline_spans(token.children, tuple(styles)))
        elif kind in ('fence', 'code_block'):
            language = token.info.strip().split()[0] if token.info.strip() else ''
            if kind == 'fence' and language.lower() in ('math', 'latex', 'tex'):
                output.append(Formula(token.markup + token.info + '\n' + token.content + token.markup,
                                      formulas.fenced_latex(token.content), True, tuple(styles)))
            else:
                output.append(Code(token.content, language, tuple(styles)))
        elif kind == 'math_block':
            output.append(formulas.from_token(token, tuple(styles)))
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
        self.math_widgets, self.math_pixels = [], 0
        # Native Tk's copy excludes elided text, which would drop formula sources.
        text.bind('<<Copy>>', self.copy_selection)
        self.font_tags = {}
        self.table_font = tkfont.Font(root=text, family='Consolas', size=10, weight='bold')
        text.tag_configure('md_quote', foreground=palette['muted'])
        text.tag_configure('md_code', background=palette['code_bg'], wrap='none', spacing1=0, spacing3=0)
        text.tag_configure('md_inline_code', background=palette['code_bg'])
        text.tag_configure('md_strike', overstrike=True)
        text.tag_configure('md_link', foreground=palette['user'], underline=True)
        text.tag_configure('md_paragraph_end', spacing3=6)
        text.tag_configure('md_rule', foreground=palette['line'])
        text.tag_configure('md_image', foreground=palette['muted'])
        text.tag_configure('md_math_source', elide=True)
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
        self.math_widgets.clear()
        self.math_pixels = 0
        for tag in self.dynamic_tags:
            self.text.tag_delete(tag)
        self.dynamic_tags.clear()
        for command in self.commands:
            self.text.deletecommand(command)
        self.commands.clear()

    def copy_selection(self, event=None):
        if self.text.tag_ranges('sel'):
            self.copy_code(self.text.get('sel.first', 'sel.last'))
        return 'break'

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
            elif name in ('inline_code', 'strike', 'rule', 'image', 'code', 'table', 'table_header', 'paragraph_end', 'math_source'):
                tags.append('md_' + name)
        tags.append(self.font_tag(styles))
        if span.href and (clickable_url(span.href) or local_path(span.href) is not None):
            tag = f'md_href_{len(self.dynamic_tags)}'
            self.dynamic_tags.append(tag)
            self.commands.append(self.text.tag_bind(tag, '<Button-1>', lambda event, href=span.href: self.open_link(href)))
            self.commands.append(self.text.tag_bind(tag, '<Enter>', lambda event: self.text.configure(cursor='hand2')))
            self.commands.append(self.text.tag_bind(tag, '<Leave>', lambda event: self.text.configure(cursor='xterm')))
            tags += ['md_link', tag]
        self.text.insert(getattr(self, 'index', 'end'), span.text, tuple(tags))

    def position(self):
        index = getattr(self, 'index', 'end')
        return self.text.index('end-1c' if index == 'end' else index)

    def render(self, source):
        for _ in self.iter_render(source):
            pass

    def math_options(self):
        return dict(size=round(11 * float(self.text.tk.call('tk', 'scaling')), 1), color=self.palette['text'])

    def insert_formula(self, element, rasters=None, extra=()):
        import tkinter as tk
        from tkinter import ttk
        raster = rasters.get(element) if rasters is not None else formulas.render(element, **self.math_options())
        if raster is None or not raster.png or self.math_pixels + raster.width * raster.height > 8_000_000:
            self.insert(Span(element.text, element.styles + ('inline_code',) + extra, element.href))
            if element.display:
                self.insert(Span('\n［' + (raster.error if raster and raster.error else '公式预览达到上限，显示原式') + '］\n', ('image',)))
            return
        if element.display and self.text.get(self.position() + ' linestart', self.position()).strip():
            self.insert(Span('\n', element.styles + extra))
        image = tk.PhotoImage(master=self.text, data=raster.png)
        background = self.palette['accent_soft'] if 'table_header' in extra else \
            self.palette['code_bg'] if 'table' in extra else self.palette['card']
        label = tk.Label(self.text, image=image, bg=background, borderwidth=0,
                         highlightthickness=0, cursor='hand2', takefocus=True)
        label.image, label.formula, label.normal_background = image, element, background
        label.bind('<Button-1>', lambda event: self.copy_code(element.latex))
        label.bind('<Return>', lambda event: self.copy_code(element.latex))
        self.widgets.append(label)
        self.math_widgets.append(label)
        self.math_pixels += raster.width * raster.height
        start = self.position()
        self.text.window_create(getattr(self, 'index', 'end'), window=label, padx=2, align='center')
        source_start = self.position()
        self.text.tag_remove('md_math_source', start, source_start)
        # Original notation remains in Text for selection, copying and exact search.
        self.insert(Span(element.text, element.styles + extra + ('math_source',), element.href))
        for tag in self.text.tag_names(source_start):
            if tag != 'md_math_source':
                self.text.tag_add(tag, start, source_start)
        if element.display:
            button = ttk.Button(self.text, text='复制公式', style='Soft.TButton',
                                command=lambda: self.copy_code(element.latex))
            self.widgets.append(button)
            self.text.window_create(getattr(self, 'index', 'end'), window=button, padx=6)
            self.insert(Span('\n', element.styles + extra))

    def highlight_math(self, query):
        for label in self.math_widgets:
            label.configure(bg=self.palette['accent_soft'] if query and query.casefold() in label.formula.text.casefold()
                            else label.normal_background)

    def iter_render(self, source, elements=None, highlights=None, rasters=None):
        from tkinter import ttk
        for element in parse(source) if elements is None else elements:
            if isinstance(element, Span):
                self.insert(element)
            elif isinstance(element, Formula):
                self.insert_formula(element, rasters)
            elif isinstance(element, Code):
                self.insert(Span((element.language or '代码') + '  ', element.styles + ('code',)))
                button = ttk.Button(self.text, text='复制代码', style='Soft.TButton',
                                    command=lambda code=element.text: self.copy_code(code))
                self.widgets.append(button)
                self.text.window_create(getattr(self, 'index', 'end'), window=button, padx=4)
                self.insert(Span('\n', element.styles + ('code',)))
                tokens = highlights.get((element.text, element.language)) if highlights is not None else None
                for value, name in tokens if tokens is not None else highlighted(element.text, element.language):
                    start = self.position()
                    self.insert(Span(value, element.styles + ('code',)))
                    if name:
                        self.text.tag_add('md_syntax_' + name, start, self.position())
                    yield
                if element.text and not element.text.endswith('\n'):
                    self.insert(Span('\n', element.styles + ('code',)))
                self.insert(Span('\n'))
            else:
                yield from self.iter_table(element, rasters)
            yield

    def render_table(self, table):
        for _ in self.iter_table(table):
            pass

    def iter_table(self, table, rasters=None):
        widths = [0] * len(table.alignments)
        for row in table.rows:
            for index, cell in enumerate(row):
                width = 0
                for span in cell:
                    raster = rasters.get(span) if rasters is not None and isinstance(span, Formula) else None
                    width += raster.width + 4 if raster and raster.png else self.table_font.measure(span.text)
                widths[index] = max(widths[index], width + 24)
            yield
        tag = f'md_columns_{len(self.dynamic_tags)}'
        self.dynamic_tags.append(tag)
        stops, offset = [], 0
        for index, width in enumerate(widths):
            alignment = table.alignments[index]
            stops += [offset + (width - 12 if alignment == 'right' else width // 2 if alignment == 'center' else 8), alignment]
            offset += width
        self.text.tag_configure(tag, tabs=tuple(stops), tabstyle='wordprocessor')
        for row_index, row in enumerate(table.rows):
            start = self.position()
            for cell in row:
                self.insert(Span('\t', table.styles + ('table',)))
                for span in cell:
                    extra = table.styles + ('table',) + (('table_header',) if row_index == 0 else ())
                    if isinstance(span, Formula):
                        self.insert_formula(span, rasters, extra)
                    else:
                        self.insert(span, extra)
            self.insert(Span('\n', table.styles + ('table',)))
            self.text.tag_add(tag, start, self.position())
            yield
        self.insert(Span('\n'))
