"""Offline TeX layout; only generated SVG paths reach the rasterizer.

Imports stay lazy so CLI/MCP and raw-text reading need no desktop packages.
PNG work belongs on a read worker; Tk images are created by the UI thread.
"""
from collections import OrderedDict
from dataclasses import dataclass
import math
import re
import threading
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class Formula:
    text: str
    latex: str
    display: bool = False
    styles: tuple = ()
    href: str = ''


@dataclass(frozen=True)
class Raster:
    png: bytes = b''
    width: int = 0
    height: int = 0
    error: str = ''


MAX_SOURCE = 4000
MAX_PIXELS = 1_000_000
MAX_CACHE_BYTES = 8_000_000
_cache = OrderedDict()
_cache_bytes = 0
_lock = threading.Lock()  # Ziamath's shared font caches are not thread-safe.


def might_have_math(source):
    return '$' in source or r'\(' in source or r'\[' in source or bool(
        re.search(r'(?:`{3,}|~{3,})\s*(?:math|latex|tex)\b', source, re.I))


def _closing_position(source, closing, start, end):
    pos = source.find(closing, start, end)
    while pos >= 0:
        back = pos - 1
        while back >= 0 and source[back] == '\\':
            back -= 1
        if (pos - back - 1) % 2 == 0:
            return pos
        pos = source.find(closing, pos + 2, end)
    return -1


def _bracket_inline(state, silent):
    """Adapt LaTeX delimiters to Markdown tokens, before Markdown escapes them."""
    start = state.pos
    opening = state.src[start:start + 2]
    if opening not in (r'\(', r'\['):
        return False
    closing = r'\)' if opening == r'\(' else r'\]'
    end = _closing_position(state.src, closing, start + 2, state.posMax)
    if end < 0 or not state.src[start + 2:end].strip():
        return False
    if not silent:
        token = state.push('math_inline' if opening == r'\(' else 'math_inline_double', 'math', 0)
        token.content, token.markup = state.src[start + 2:end], opening
    state.pos = end + 2
    return True


def _bracket_block(state, start, end, silent):
    if state.sCount[start] - state.blkIndent >= 4:
        return False
    # getLines respects quote/list indentation instead of scanning raw source.
    first = state.getLines(start, start + 1, state.blkIndent, False)
    if not first.startswith(r'\['):
        return False
    lines = []
    for line in range(start, end):
        value = state.getLines(line, line + 1, state.blkIndent, False)
        lines.append(value)
        close = _closing_position(value, r'\]', 0, len(value))
        if close < 0:
            continue
        # Never consume text following a closing delimiter; inline parsing keeps it.
        if value[close + 2:].strip():
            return False
        content = '\n'.join(lines[:-1] + [value[:close]])[2:]
        if not content.strip():
            return False
        if not silent:
            token = state.push('math_block', 'math', 0)
            token.block, token.content, token.markup = True, content, r'\['
            token.map = [start, line + 1]
        if not silent:
            state.line = line + 1
        return True
    return False


def configure_parser(parser):
    from mdit_py_cjk_friendly import cjk_friendly
    from mdit_py_plugins.dollarmath import dollarmath_plugin
    parser.use(cjk_friendly)
    parser.use(dollarmath_plugin, allow_space=False, allow_digits=False,
               allow_labels=False, double_inline=True, allow_blank_lines=True)
    # Do not let a currency opening swallow a later Markdown code span.
    from mdit_py_plugins.dollarmath.index import math_inline_dollar
    dollar = math_inline_dollar(allow_space=False, allow_digits=False, allow_double=True)
    def dollar_without_code(state, silent):
        start = state.pos
        found = dollar(state, True)
        end, state.pos = state.pos, start
        if not found or '`' in state.src[start:end]:
            return False
        return dollar(state, silent)
    parser.inline.ruler.at('math_inline', dollar_without_code)
    parser.inline.ruler.before('escape', 'bracket_math', _bracket_inline)
    parser.block.ruler.before('fence', 'bracket_math', _bracket_block,
                              {'alt': ['paragraph', 'reference', 'blockquote', 'list']})


def from_token(token, styles=(), href=''):
    opening = token.markup or '$$'
    closing = {r'\(': r'\)', r'\[': r'\]'}.get(opening, opening)
    return Formula(opening + token.content + closing, token.content,
                   token.type != 'math_inline', styles, href)


def render(formula, *, size=18, color='#282824'):
    """Return a bounded, cached PNG or a recoverable source-text fallback."""
    global _cache_bytes
    if len(formula.latex) > MAX_SOURCE:
        return Raster(error='公式过长，显示原式')
    key = (formula.latex, formula.display, 'bold' in formula.styles, size, color)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        try:
            result = _render(*key)
        except ImportError:
            result = Raster(error='公式依赖未安装，显示原式；请安装 requirements-ui.txt')
        except Exception:
            # Unsupported TeX must not blank the rest of a message or log its text.
            result = Raster(error='暂不支持此公式，显示原式')
        _cache[key] = result
        _cache_bytes += len(result.png)
        while len(_cache) > 128 or _cache_bytes > MAX_CACHE_BYTES:
            _, old = _cache.popitem(last=False)
            _cache_bytes -= len(old.png)
        return result


def _render(latex, display, bold, size, color):
    import ziamath
    import resvg_py
    # Bound recursive layouts before handing them to third-party parsers.
    depth = 0
    for char in latex:
        depth += (char == '{') - (char == '}')
        if depth > 64:
            return Raster(error='公式嵌套过深，显示原式')
    if re.search('[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0002ffff]', latex):
        return Raster(error='数学字体未覆盖此公式的文字，显示原式')
    layout = ziamath.Latex(latex.strip(), size=size, color=color, inline=not display,
                           mathstyle='bold' if bold else None)
    svg = layout.svg()
    if len(svg) > 2_000_000:
        return Raster(error='公式图像过大，显示原式')
    root = ET.fromstring(svg)
    width, height = math.ceil(float(root.attrib['width'])), math.ceil(float(root.attrib['height']))
    if not (0 < width <= 4096 and 0 < height <= 1024 and width * height <= MAX_PIXELS):
        return Raster(error='公式图像过大，显示原式')
    # Ziamath emits glyph paths/uses. Reject all external resources and active SVG.
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] not in ('svg', 'path', 'symbol', 'use', 'g', 'rect', 'ellipse', 'title'):
            raise ValueError('Unexpected SVG element')
        if any(key.rsplit('}', 1)[-1] == 'href' and not value.startswith('#')
               for key, value in node.attrib.items()):
            raise ValueError('External SVG resource')
    png = resvg_py.svg_to_bytes(svg_string=svg, skip_system_fonts=True, width=width, height=height)
    if len(png) > 512_000:
        return Raster(error='公式图像过大，显示原式')
    return Raster(png, width, height)
