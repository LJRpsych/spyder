# -*- coding: utf-8 -*-
#
# Copyright © 2013 The Spyder Development Team
# Licensed under the terms of the MIT License
# (see spyderlib/__init__.py for details)

"""
Introspection utilities used by Spyder
"""
from __future__ import print_function
import imp
import os
import re
import time
import functools
from collections import OrderedDict

from spyderlib.baseconfig import DEBUG, get_conf_path, debug_print
from spyderlib.utils.debug import log_dt, log_last_error
from spyderlib.utils import sourcecode
from spyderlib.qt.QtGui import QApplication


PLUGINS = ['jedi', 'rope']
LOG_FILENAME = get_conf_path('introspection.log')
DEBUG_EDITOR = DEBUG >= 3


#-----------------------------------------------------------------------------
# Introspection API
#-----------------------------------------------------------------------------

def get_plugin(editor_widget):
    """Get and load a plugin, checking in order of PLUGINS"""
    plugin = None
    for plugin_name in PLUGINS:
        mod_name = plugin_name + '_plugin'
        try:
            mod = __import__('spyderlib.utils.introspection.' + mod_name,
                             fromlist=[mod_name])
            cls = getattr(mod, '%sPlugin' % plugin_name.capitalize())
            plugin = cls()
            plugin.load_plugin(editor_widget)
        except Exception:
            if DEBUG_EDITOR:
                log_last_error(LOG_FILENAME)
        else:
            break
    if not plugin:
        plugin = IntrospectionPlugin()
    debug_print('Instropection Plugin Loaded: %s' % plugin.name)
    return plugin


class IntrospectionPlugin(object):

    name = 'fallback'
    editor_widget = None

    def load_plugin(self, editor_widget):
        raise NotImplementedError

    def get_completion_list(self, source_code, offset, filename):
        """Return a list of completion strings"""
        return self.get_token_completion_list(source_code, offset, filename)

    def get_calltip_and_docs(self, source_code, offset, filename):
        """
        Find the calltip and docs

        Calltip is a string with the function or class and its arguments
            e.g. 'match(patern, string, flags=0)'
                 'ones(shape, dtype=None, order='C')'
        Docs is a a string or a dict with the following keys:
           e.g. 'Try to apply the pattern at the start of the string...'
           or {'note': 'Function of numpy.core.numeric...',
               'argspec': "(shape, dtype=None, order='C')'
               'docstring': 'Return an array of given...'
               'name': 'ones'}
        """
        return None

    def get_definition_location(self, source_code, offset, filename):
        """Find a path and line number for a definition"""
        return self.get_definition_location_regex(source_code, offset, 
                                                  filename)
        
    def get_definition_location_regex(self, source_code, offset, filename):
        """Find a path an line number for a definition using regex"""
        ret = None, None
        try:
            ret = get_definition_location_regex(source_code, offset, filename)
            debug_print('get regex definition: ' + str(ret))
        except Exception as e:
            debug_print('Regex error: %s' % e)
            if DEBUG_EDITOR:
                log_last_error(LOG_FILENAME)
        return ret
        
    def get_token_completion_list(self, source_code, offset, filename):
        """Return a list of completion strings using token matching"""
        ret = None
        try:
            ret = token_based_completion(source_code, offset)
            debug_print('token completion: %s ...(%s)' % (ret[:2], len(ret)))
        except Exception:
            if DEBUG_EDITOR:
                log_last_error(LOG_FILENAME)
        return ret or []

    def set_pref(self, name, value):
        """Set a plugin preference to a value"""
        pass

    def validate(self):
        """Validate the plugin"""
        pass

    def is_editor_ready(self):
        """Check if the main app is starting up"""
        if self.editor_widget:
            return self.editor_widget.window().is_starting_up
        else:
            return False

    def get_current_source(self):
        """Get the source code in the current file"""
        if self.editor_widget:
            finfo = self.editor_widget.get_current_finfo()
            if finfo:
                return finfo.get_source_code()

    def post_message(self, message, timeout=60000):
        """
        Post a message to the main window status bar with a timeout in ms
        """
        if self.editor_widget:
            statusbar = self.editor_widget.window().statusBar()
            statusbar.showMessage(message, timeout)
            QApplication.processEvents()


#-----------------------------------------------------------------------------
# Helper functions
#-----------------------------------------------------------------------------

def memoize(obj):
    """
    Memoize objects to trade memory for execution speed

    Use a limited size cache to store the value, which takes into account
    The calling args and kwargs

    See https://wiki.python.org/moin/PythonDecoratorLibrary#Memoize
    """
    cache = obj.cache = OrderedDict()

    @functools.wraps(obj)
    def memoizer(*args, **kwargs):
        key = str(args) + str(kwargs)
        if key not in cache:
            cache[key] = obj(*args, **kwargs)
        # only keep the most recent 100 entries
        if len(cache) > 100:
            cache.popitem(last=False)
        return cache[key]
    return memoizer


def token_based_completion(script, offset):
    """Simple completion based on python-like identifiers and whitespace"""
    base = sourcecode.get_primary_at(script, offset)
    tokens = sourcecode.get_identifiers(script)
    items = [item for item in tokens if
             item.startswith(base) and len(item) > len(base)]
    if '.' in base:
        start = base.rfind('.') + 1
    else:
        start = 0
    items = [i[start:len(base)] + i[len(base):].split('.')[0] for i in items]
    return list(sorted(items))


@memoize
def python_like_mod_finder(import_line, alt_path=None, stop_token=None):
    """
    Locate a module path based on an import line in an python-like file

    import_line is the line of source code containing the import
    alt_path specifies an alternate base path for the module
    stop_token specifies the desired name to stop on

    This is used to a find path python-like modules (e.g. cython and enaml)
    to find a definition.
    """
    if stop_token and '.' in stop_token:
        stop_token = stop_token.split('.')[-1]
    tokens = re.split(r'\W', import_line)
    if tokens[0] in ['from', 'import']:
        # find the base location
        try:
            _, path, _ = imp.find_module(tokens[1])
        except ImportError:
            if alt_path:
                path = os.path.join(alt_path, tokens[1])
            else:
                path = None
        if path:
            path = os.path.realpath(path)
            if not tokens[1] == stop_token:
                for part in tokens[2:]:
                    if part in ['import', 'cimport', 'as']:
                        break
                    path = os.path.join(path, part)
                    if part == stop_token:
                        break
            # from package import module
            if stop_token and not stop_token in path:
                for ext in python_like_exts():
                    fname = '%s%s' % (stop_token, ext)
                    if os.path.exists(os.path.join(path, fname)):
                        return os.path.join(path, fname)
            # from module import name
            for ext in python_like_exts():
                fname = '%s%s' % (path, ext)
                if os.path.exists(fname):
                    return fname
            # if it is a file, return it
            if os.path.exists(path) and not os.path.isdir(path):
                return path
            # default to the package file
            path = os.path.join(path, '__init__.py')
            if os.path.exists(path):
                return path


def get_definition_location_regex(source_code, offset, filename):
    """Find the definition for an object within a set of source code"""
    token = sourcecode.get_primary_at(source_code, offset)
    eol = sourcecode.get_eol_chars(source_code) or '\n'
    lines = source_code[:offset].split(eol)
    line_nr = get_definition_with_regex(source_code, token, len(lines))
    line = source_code.split(eol)[line_nr - 1].strip()
    if not os.path.splitext(filename)[-1] in python_like_exts():
        line_nr = get_definition_with_regex(source_code, token, line_nr)
        return filename, line_nr
    if line.startswith('import ') or line.startswith('from '):
        alt_path = os.path.dirname(filename)
        source_file = python_like_mod_finder(line, alt_path=alt_path,
                                             stop_token=token)
        if (not source_file or
                not os.path.splitext(source_file)[-1] in python_like_exts()):
            line_nr = get_definition_with_regex(source_code, token, line_nr)
            return filename, line_nr
        mod_name = os.path.basename(source_file).split('.')[0]
        if mod_name == token or mod_name == '__init__':
            return source_file, 1
        else:
            line_nr = get_definition_from_file(source_file, token)
            return source_file, line_nr
    return filename, line_nr


@memoize
def get_definition_from_file(filename, name, line_nr=-1):
    """Find the definition for an object in a filename"""
    with open(filename, 'rb') as fid:
        code = fid.read()
    return get_definition_with_regex(code, name, line_nr)


def get_definition_with_regex(source, token, start_line=-1):
    """
    Find the definition of an object within a source closest to a given line
    """
    if not token:
        return None
    if DEBUG_EDITOR:
        t0 = time.time()
    patterns = [  # python / cython keyword definitions
                '^c?import.*\W{0}{1}',
                'from.*\W{0}\W.*c?import ',
                'from .* c?import.*\W{0}{1}',
                'class\s*{0}{1}',
                'c?p?def[^=]*\W{0}{1}',
                'cdef.*\[.*\].*\W{0}{1}',
                # "self.item =" or "item ="
                '.*\Wself.{0}{1}[^=!<>]*=[^=]',
                '.*\W{0}{1}[^=!<>]*=[^=]',
                'self.{0}{1}[^=!<>]*=[^=]',
                '{0}{1}[^=!<>]*=[^=]',
                # enaml keyword definitions
                'enamldef.*\W{0}{1}',
                'attr.*\W{0}{1}',
                'event.*\W{0}{1}',
                'id\s*:.*\W{0}{1}']
    patterns = [pattern.format(token, r'[^0-9a-zA-Z.[]')
                for pattern in patterns]
    pattern = re.compile('|^'.join(patterns))
    # add the trailing space to allow some regexes to match
    eol = sourcecode.get_eol_chars(source) or '\n'
    lines = source.split(eol)
    lines = [line.strip() + ' ' for line in lines]
    if start_line == -1:
        start_line = len(lines)
    matches = []
    for (index, line) in enumerate(lines):
        if re.match(pattern, line):
            matches.append(index + 1)
    # find the one closest to the start line (prefer before the start line)
    if matches:
        min_dist = len(lines)
        best_ind = 0
        for match in matches:
            dist = abs(start_line - match)
            if match <= start_line or not best_ind:
                if dist < min_dist:
                    min_dist = dist
                    best_ind = match
    if matches:
        if DEBUG_EDITOR:
            log_dt(LOG_FILENAME, 'regex definition match', t0)
        return best_ind
    else:
        if DEBUG_EDITOR:
            log_dt(LOG_FILENAME, 'regex definition failed match', t0)
        return None


@memoize
def get_parent_until(path):
    """
    Given a file path, determine the full module path

    e.g. '/usr/lib/python2.7/dist-packages/numpy/core/__init__.pyc' yields
    'numpy.core'
    """
    dirname = os.path.dirname(path)
    try:
        mod = os.path.basename(path)
        mod = os.path.splitext(mod)[0]
        imp.find_module(mod, [dirname])
    except ImportError:
        return
    items = [mod]
    while 1:
        items.append(os.path.basename(dirname))
        try:
            dirname = os.path.dirname(dirname)
            imp.find_module('__init__', [dirname + os.sep])
        except ImportError:
            break
    return '.'.join(reversed(items))


def python_like_exts():
    """Return a list of all python-like extensions"""
    exts = []
    for lang in sourcecode.PYTHON_LIKE_LANGUAGES:
        exts.extend(list(sourcecode.ALL_LANGUAGES[lang]))
    return ['.' + ext for ext in exts]


def all_editable_exts():
    """Return a list of all editable extensions"""
    exts = []
    for (language, extensions) in sourcecode.ALL_LANGUAGES:
        exts.extend(list(extensions))
    return ['.' + ext for ext in exts]


if __name__ == '__main__':
    get_plugin()
    with open(__file__) as fid:
        code = fid.read()
    code += '\nget_conf_path'
    print(get_definition_location_regex(code, len(code), __file__))
    print(token_based_completion(code[:-2], len(code) - 2))
    print(python_like_exts())
    print(all_editable_exts())
    print(get_parent_until(__file__))
    line = 'from spyderlib.widgets.sourcecode.codeeditor import CodeEditor'
    print(python_like_mod_finder(line))
    print(python_like_mod_finder(line, stop_token='sourcecode'))
    print(get_parent_until(os.path.expanduser(r'~/.spyder2/temp.py')))
    code = 'import re\n\nre'
    print(get_definition_location_regex(code, len(code), 'dummy.txt'))
    code = 'self.proxy.widget; self.'
    print(token_based_completion(code, len(code)))
    code = 'self.sigMessageReady.emit; self.'
    print(token_based_completion(code, len(code)))