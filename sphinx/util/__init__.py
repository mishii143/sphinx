"""
    sphinx.util
    ~~~~~~~~~~~

    Utility functions for Sphinx.

    :copyright: Copyright 2007-2020 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

import functools
import os
import posixpath
import re
import sys
import tempfile
import traceback
import unicodedata
from datetime import datetime
from hashlib import md5
from importlib import import_module
from os import path
from time import mktime, strptime
from typing import Any, Callable, Dict, IO, Iterable, Iterator, List, Pattern, Set, Tuple, Type
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit, quote_plus, parse_qsl, urlencode

from sphinx.errors import SphinxParallelError, ExtensionError, FiletypeNotFoundError
from sphinx.locale import __
from sphinx.util import logging
from sphinx.util.console import strip_colors, colorize, bold, term_width_line  # type: ignore
from sphinx.util.typing import PathMatcher
from sphinx.util import smartypants  # noqa

# import other utilities; partly for backwards compatibility, so don't
# prune unused ones indiscriminately
from sphinx.util.osutil import (  # noqa
    SEP, os_path, relative_uri, ensuredir, mtimes_of_files, movefile,
    copyfile, copytimes, make_filename)
from sphinx.util.nodes import (   # noqa
    nested_parse_with_titles, split_explicit_title, explicit_title_re,
    caption_ref_re)
from sphinx.util.matching import patfilter  # noqa


if TYPE_CHECKING:
    from sphinx.application import Sphinx


logger = logging.getLogger(__name__)

# Generally useful regular expressions.
ws_re = re.compile(r'\s+')                      # type: Pattern
url_re = re.compile(r'(?P<schema>.+)://.*')     # type: Pattern


# High-level utility functions.

def docname_join(basedocname: str, docname: str) -> str:
    return posixpath.normpath(
        posixpath.join('/' + basedocname, '..', docname))[1:]


def path_stabilize(filepath: str) -> str:
    "normalize path separater and unicode string"
    newpath = filepath.replace(os.path.sep, SEP)
    return unicodedata.normalize('NFC', newpath)


def get_matching_files(dirname: str,
                       exclude_matchers: Tuple[PathMatcher, ...] = ()) -> Iterable[str]:  # NOQA
    """Get all file names in a directory, recursively.

    Exclude files and dirs matching some matcher in *exclude_matchers*.
    """
    # dirname is a normalized absolute path.
    dirname = path.normpath(path.abspath(dirname))
    dirlen = len(dirname) + 1    # exclude final os.path.sep

    for root, dirs, files in os.walk(dirname, followlinks=True):
        relativeroot = root[dirlen:]

        qdirs = enumerate(path_stabilize(path.join(relativeroot, dn))
                          for dn in dirs)  # type: Iterable[Tuple[int, str]]
        qfiles = enumerate(path_stabilize(path.join(relativeroot, fn))
                           for fn in files)  # type: Iterable[Tuple[int, str]]
        for matcher in exclude_matchers:
            qdirs = [entry for entry in qdirs if not matcher(entry[1])]
            qfiles = [entry for entry in qfiles if not matcher(entry[1])]

        dirs[:] = sorted(dirs[i] for (i, _) in qdirs)

        for i, filename in sorted(qfiles):
            yield filename


def get_filetype(source_suffix: Dict[str, str], filename: str) -> str:
    for suffix, filetype in source_suffix.items():
        if filename.endswith(suffix):
            # If default filetype (None), considered as restructuredtext.
            return filetype or 'restructuredtext'
    else:
        raise FiletypeNotFoundError


class FilenameUniqDict(dict):
    """
    A dictionary that automatically generates unique names for its keys,
    interpreted as filenames, and keeps track of a set of docnames they
    appear in.  Used for images and downloadable files in the environment.
    """
    def __init__(self) -> None:
        self._existing = set()  # type: Set[str]

    def add_file(self, docname: str, newfile: str) -> str:
        if newfile in self:
            self[newfile][0].add(docname)
            return self[newfile][1]
        uniquename = path.basename(newfile)
        base, ext = path.splitext(uniquename)
        i = 0
        while uniquename in self._existing:
            i += 1
            uniquename = '%s%s%s' % (base, i, ext)
        self[newfile] = ({docname}, uniquename)
        self._existing.add(uniquename)
        return uniquename

    def purge_doc(self, docname: str) -> None:
        for filename, (docs, unique) in list(self.items()):
            docs.discard(docname)
            if not docs:
                del self[filename]
                self._existing.discard(unique)

    def merge_other(self, docnames: Set[str], other: Dict[str, Tuple[Set[str], Any]]) -> None:
        for filename, (docs, unique) in other.items():
            for doc in docs & set(docnames):
                self.add_file(doc, filename)

    def __getstate__(self) -> Set[str]:
        return self._existing

    def __setstate__(self, state: Set[str]) -> None:
        self._existing = state


class DownloadFiles(dict):
    """A special dictionary for download files.

    .. important:: This class would be refactored in nearly future.
                   Hence don't hack this directly.
    """

    def add_file(self, docname: str, filename: str) -> str:
        if filename not in self:
            digest = md5(filename.encode()).hexdigest()
            dest = '%s/%s' % (digest, os.path.basename(filename))
            self[filename] = (set(), dest)

        self[filename][0].add(docname)
        return self[filename][1]

    def purge_doc(self, docname: str) -> None:
        for filename, (docs, dest) in list(self.items()):
            docs.discard(docname)
            if not docs:
                del self[filename]

    def merge_other(self, docnames: Set[str], other: Dict[str, Tuple[Set[str], Any]]) -> None:
        for filename, (docs, dest) in other.items():
            for docname in docs & set(docnames):
                self.add_file(docname, filename)


_DEBUG_HEADER = '''\
# Sphinx version: %s
# Python version: %s (%s)
# Docutils version: %s %s
# Jinja2 version: %s
# Last messages:
%s
# Loaded extensions:
'''


def save_traceback(app: "Sphinx") -> str:
    """Save the current exception's traceback in a temporary file."""
    import sphinx
    import jinja2
    import docutils
    import platform
    exc = sys.exc_info()[1]
    if isinstance(exc, SphinxParallelError):
        exc_format = '(Error in parallel process)\n' + exc.traceback
    else:
        exc_format = traceback.format_exc()
    fd, path = tempfile.mkstemp('.log', 'sphinx-err-')
    last_msgs = ''
    if app is not None:
        last_msgs = '\n'.join(
            '#   %s' % strip_colors(s).strip()
            for s in app.messagelog)
    os.write(fd, (_DEBUG_HEADER %
                  (sphinx.__display_version__,
                   platform.python_version(),
                   platform.python_implementation(),
                   docutils.__version__, docutils.__version_details__,
                   jinja2.__version__,  # type: ignore
                   last_msgs)).encode())
    if app is not None:
        for ext in app.extensions.values():
            modfile = getattr(ext.module, '__file__', 'unknown')
            if ext.version != 'builtin':
                os.write(fd, ('#   %s (%s) from %s\n' %
                              (ext.name, ext.version, modfile)).encode())
    os.write(fd, exc_format.encode())
    os.close(fd)
    return path


def get_full_modname(modname: str, attribute: str) -> str:
    if modname is None:
        # Prevents a TypeError: if the last getattr() call will return None
        # then it's better to return it directly
        return None
    module = import_module(modname)

    # Allow an attribute to have multiple parts and incidentially allow
    # repeated .s in the attribute.
    value = module
    for attr in attribute.split('.'):
        if attr:
            value = getattr(value, attr)

    return getattr(value, '__module__', None)


# a regex to recognize coding cookies
_coding_re = re.compile(r'coding[:=]\s*([-\w.]+)')


class UnicodeDecodeErrorHandler:
    """Custom error handler for open() that warns and replaces."""

    def __init__(self, docname: str) -> None:
        self.docname = docname

    def __call__(self, error: UnicodeDecodeError) -> Tuple[str, int]:
        linestart = error.object.rfind(b'\n', 0, error.start)
        lineend = error.object.find(b'\n', error.start)
        if lineend == -1:
            lineend = len(error.object)
        lineno = error.object.count(b'\n', 0, error.start) + 1
        logger.warning(__('undecodable source characters, replacing with "?": %r'),
                       (error.object[linestart + 1:error.start] + b'>>>' +
                        error.object[error.start:error.end] + b'<<<' +
                        error.object[error.end:lineend]),
                       location=(self.docname, lineno))
        return ('?', error.end)


# Low-level utility functions and classes.

class Tee:
    """
    File-like object writing to two streams.
    """
    def __init__(self, stream1: IO, stream2: IO) -> None:
        self.stream1 = stream1
        self.stream2 = stream2

    def write(self, text: str) -> None:
        self.stream1.write(text)
        self.stream2.write(text)

    def flush(self) -> None:
        if hasattr(self.stream1, 'flush'):
            self.stream1.flush()
        if hasattr(self.stream2, 'flush'):
            self.stream2.flush()


def parselinenos(spec: str, total: int) -> List[int]:
    """Parse a line number spec (such as "1,2,4-6") and return a list of
    wanted line numbers.
    """
    items = list()
    parts = spec.split(',')
    for part in parts:
        try:
            begend = part.strip().split('-')
            if ['', ''] == begend:
                raise ValueError
            elif len(begend) == 1:
                items.append(int(begend[0]) - 1)
            elif len(begend) == 2:
                start = int(begend[0] or 1)  # left half open (cf. -10)
                end = int(begend[1] or max(start, total))  # right half open (cf. 10-)
                if start > end:  # invalid range (cf. 10-1)
                    raise ValueError
                items.extend(range(start - 1, end))
            else:
                raise ValueError
        except Exception:
            raise ValueError('invalid line number spec: %r' % spec)

    return items


def rpartition(s: str, t: str) -> Tuple[str, str]:
    """Similar to str.rpartition from 2.5, but doesn't return the separator."""
    i = s.rfind(t)
    if i != -1:
        return s[:i], s[i + len(t):]
    return '', s


def split_into(n: int, type: str, value: str) -> List[str]:
    """Split an index entry into a given number of parts at semicolons."""
    parts = [x.strip() for x in value.split(';', n - 1)]
    if sum(1 for part in parts if part) < n:
        raise ValueError('invalid %s index entry %r' % (type, value))
    return parts


def split_index_msg(type: str, value: str) -> List[str]:
    # new entry types must be listed in directives/other.py!
    if type == 'single':
        try:
            result = split_into(2, 'single', value)
        except ValueError:
            result = split_into(1, 'single', value)
    elif type == 'pair':
        result = split_into(2, 'pair', value)
    elif type == 'triple':
        result = split_into(3, 'triple', value)
    elif type == 'see':
        result = split_into(2, 'see', value)
    elif type == 'seealso':
        result = split_into(2, 'see', value)
    else:
        raise ValueError('invalid %s index entry %r' % (type, value))

    return result


def format_exception_cut_frames(x: int = 1) -> str:
    """Format an exception with traceback, but only the last x frames."""
    typ, val, tb = sys.exc_info()
    # res = ['Traceback (most recent call last):\n']
    res = []  # type: List[str]
    tbres = traceback.format_tb(tb)
    res += tbres[-x:]
    res += traceback.format_exception_only(typ, val)
    return ''.join(res)


def import_object(objname: str, source: str = None) -> Any:
    """Import python object by qualname."""
    try:
        objpath = objname.split('.')
        modname = objpath.pop(0)
        obj = import_module(modname)
        for name in objpath:
            modname += '.' + name
            try:
                obj = getattr(obj, name)
            except AttributeError:
                obj = import_module(modname)

        return obj
    except (AttributeError, ImportError) as exc:
        if source:
            raise ExtensionError('Could not import %s (needed for %s)' %
                                 (objname, source), exc)
        else:
            raise ExtensionError('Could not import %s' % objname, exc)


def encode_uri(uri: str) -> str:
    split = list(urlsplit(uri))
    split[1] = split[1].encode('idna').decode('ascii')
    split[2] = quote_plus(split[2].encode(), '/')
    query = list((q, v.encode()) for (q, v) in parse_qsl(split[3]))
    split[3] = urlencode(query)
    return urlunsplit(split)


def display_chunk(chunk: Any) -> str:
    if isinstance(chunk, (list, tuple)):
        if len(chunk) == 1:
            return str(chunk[0])
        return '%s .. %s' % (chunk[0], chunk[-1])
    return str(chunk)


def old_status_iterator(iterable: Iterable, summary: str, color: str = "darkgreen",
                        stringify_func: Callable[[Any], str] = display_chunk) -> Iterator:
    l = 0
    for item in iterable:
        if l == 0:
            logger.info(bold(summary), nonl=True)
            l = 1
        logger.info(stringify_func(item), color=color, nonl=True)
        logger.info(" ", nonl=True)
        yield item
    if l == 1:
        logger.info('')


# new version with progress info
def status_iterator(iterable: Iterable, summary: str, color: str = "darkgreen",
                    length: int = 0, verbosity: int = 0,
                    stringify_func: Callable[[Any], str] = display_chunk) -> Iterable:
    if length == 0:
        yield from old_status_iterator(iterable, summary, color, stringify_func)
        return
    l = 0
    summary = bold(summary)
    for item in iterable:
        l += 1
        s = '%s[%3d%%] %s' % (summary, 100 * l / length, colorize(color, stringify_func(item)))
        if verbosity:
            s += '\n'
        else:
            s = term_width_line(s)
        logger.info(s, nonl=True)
        yield item
    if l > 0:
        logger.info('')


class SkipProgressMessage(Exception):
    pass


class progress_message:
    def __init__(self, message: str) -> None:
        self.message = message

    def __enter__(self) -> None:
        logger.info(bold(self.message + '... '), nonl=True)

    def __exit__(self, exc_type: "Type[Exception]", exc_value: Exception, traceback: Any) -> bool:  # NOQA
        if isinstance(exc_value, SkipProgressMessage):
            logger.info(__('skipped'))
            if exc_value.args:
                logger.info(*exc_value.args)
            return True
        elif exc_type:
            logger.info(__('failed'))
        else:
            logger.info(__('done'))

        return False

    def __call__(self, f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self:
                return f(*args, **kwargs)

        return wrapper


def epoch_to_rfc1123(epoch: float) -> str:
    """Convert datetime format epoch to RFC1123."""
    from babel.dates import format_datetime

    dt = datetime.fromtimestamp(epoch)
    fmt = 'EEE, dd LLL yyyy hh:mm:ss'
    return format_datetime(dt, fmt, locale='en') + ' GMT'


def rfc1123_to_epoch(rfc1123: str) -> float:
    return mktime(strptime(rfc1123, '%a, %d %b %Y %H:%M:%S %Z'))


def xmlname_checker() -> Pattern:
    # https://www.w3.org/TR/REC-xml/#NT-Name
    name_start_chars = [
        ':', ['A', 'Z'], '_', ['a', 'z'], ['\u00C0', '\u00D6'],
        ['\u00D8', '\u00F6'], ['\u00F8', '\u02FF'], ['\u0370', '\u037D'],
        ['\u037F', '\u1FFF'], ['\u200C', '\u200D'], ['\u2070', '\u218F'],
        ['\u2C00', '\u2FEF'], ['\u3001', '\uD7FF'], ['\uF900', '\uFDCF'],
        ['\uFDF0', '\uFFFD'], ['\U00010000', '\U000EFFFF']]

    name_chars = [
        "\\-", "\\.", ['0', '9'], '\u00B7', ['\u0300', '\u036F'],
        ['\u203F', '\u2040']
    ]

    def convert(entries: Any, splitter: str = '|') -> str:
        results = []
        for entry in entries:
            if isinstance(entry, list):
                results.append('[%s]' % convert(entry, '-'))
            else:
                results.append(entry)
        return splitter.join(results)

    start_chars_regex = convert(name_start_chars)
    name_chars_regex = convert(name_chars)
    return re.compile('(%s)(%s|%s)*' % (
        start_chars_regex, start_chars_regex, name_chars_regex))
