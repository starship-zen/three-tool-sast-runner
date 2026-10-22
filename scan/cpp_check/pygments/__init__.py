"""Minimal pygments compatibility layer for cppcheck-htmlreport."""

from html import escape as html_escape
import os

from .formatters import HtmlFormatter
from .lexers import guess_lexer, guess_lexer_for_filename
from .util import ClassNotFound


def _render_error_marker(error):
    msg = html_escape(error.get("msg", ""))
    verbose = error.get("verbose")
    if error.get("inconclusive") == "true":
        if verbose and verbose != error.get("msg"):
            return (
                '<div class="verbose expandable">'
                '<span class="inconclusive2">&lt;--- %s <span class="marker">[+]</span></span>'
                '<div class="content">%s</div></div>'
                % (msg, html_escape(verbose.replace("\\012", "\n")))
            )
        return '<span class="inconclusive2">&lt;--- %s</span>' % msg

    if verbose and verbose != error.get("msg"):
        return (
            '<div class="verbose expandable">'
            '<span class="error2">&lt;--- %s <span class="marker">[+]</span></span>'
            '<div class="content">%s</div></div>'
            % (msg, html_escape(verbose.replace("\\012", "\n")))
        )
    return '<span class="error2">&lt;--- %s</span>' % msg


def highlight(code, lexer, formatter):
    """Render highlighted HTML using the compatibility formatter."""

    del lexer

    if isinstance(code, bytes):
        encoding = getattr(formatter, "encoding", None) or "utf-8"
        code = code.decode(encoding, errors="replace")

    lines = code.splitlines()
    html_lines = ['<div class="highlight"><table class="highlighttable"><tbody>']
    errors = list(getattr(formatter, "errors", []))
    hl_lines = {int(line) for line in getattr(formatter, "hl_lines", [])}
    context_lines = int(os.environ.get("CPPCHECK_HTML_CONTEXT_LINES", "0"))
    if context_lines < 0:
        context_lines = 0
    visible_lines = set()
    for line_no in hl_lines:
        visible_lines.update(range(max(1, line_no - context_lines), line_no + context_lines + 1))
    lineanchors = getattr(formatter, "lineanchors", None)
    linenos = bool(getattr(formatter, "linenos", False))

    for line_no, line in enumerate(lines, 1):
        if os.environ.get("CPPCHECK_HTML_ONLY_ALERT_LINES", "1") != "0" and line_no not in visible_lines:
            continue
        row_id = f' id="{lineanchors}-{line_no}"' if lineanchors else ""
        code_cell = html_escape(line)
        if line_no in hl_lines:
            code_cell = f'<span class="hll">{code_cell}</span>'

        for error in errors:
            if error.get("line") == line_no:
                code_cell += _render_error_marker(error)

        row_html = f'<tr{row_id}>'
        if linenos:
            row_html += f'<td class="linenos">{line_no}</td>'
        row_html += f'<td class="code">{code_cell}</td></tr>'
        html_lines.append(row_html)

    html_lines.append('</tbody></table></div>')
    rendered = "\n".join(html_lines)

    encoding = getattr(formatter, "encoding", None) or "utf-8"
    return rendered.encode(encoding)
