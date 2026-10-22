"""HTML formatter helpers for the cppcheck-htmlreport pygments shim."""

from html import escape as html_escape


class HtmlFormatter:
    def __init__(self, linenos=False, style=None, hl_lines=None, lineanchors=None, encoding="utf-8", **kwargs):
        del style, kwargs
        self.linenos = linenos
        self.hl_lines = {int(line) for line in (hl_lines or [])}
        self.lineanchors = lineanchors
        self.encoding = encoding or "utf-8"

    def get_style_defs(self, selector=".highlight"):
        return "\n".join([
            f"{selector} {{",
            "  background: #fff;",
            "  color: #1f2937;",
            "}",
            f"{selector} table.highlighttable {{",
            "  border-collapse: collapse;",
            "  width: 100%;",
            "}",
            f"{selector} td.linenos {{",
            "  color: #9ca3af;",
            "  padding-right: 8px;",
            "  vertical-align: top;",
            "  white-space: pre;",
            "}",
            f"{selector} td.code {{",
            "  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;",
            "  white-space: pre;",
            "}",
            f"{selector} .hll {{",
            "  background: #fff7cc;",
            "}",
        ])
