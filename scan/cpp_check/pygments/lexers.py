"""Lexer helpers for the cppcheck-htmlreport pygments shim."""

from .util import ClassNotFound


class _DummyLexer:
    def __init__(self, filename=None):
        self.filename = filename
        self.name = _guess_name(filename)
        self.encoding = "utf-8"


def _guess_name(filename):
    if not filename:
        return "text"

    lower = filename.lower()
    if lower.endswith(('.cpp', '.cc', '.cxx', '.c', '.hpp', '.hh', '.hxx', '.h')):
        return "c++"
    if lower.endswith('.py'):
        return "python"
    if lower.endswith('.sh'):
        return "bash"
    return "text"


def guess_lexer(code, stripnl=False, **kwargs):
    del code, stripnl, kwargs
    return _DummyLexer()


def guess_lexer_for_filename(filename, code, stripnl=False, **kwargs):
    del code, stripnl, kwargs
    if not filename:
        raise ClassNotFound("no filename provided")
    return _DummyLexer(filename)
