import inspect
import json
import os
import sys
import tempfile
import thread
import time
import types
import errno
import httplib2
import contextlib

from eventlet import patcher, tpool, corolocal

from oslo.config import cfg

trace_opts = [
    cfg.BoolOpt('trace_enabled', default=False),
    cfg.StrOpt('trace_dir', default=tempfile.gettempdir()),
]

CONF = cfg.CONF
CONF.register_cli_opts(trace_opts)

_native_threading = patcher.original('threading')

_native_local = _native_threading.local()
_eventlet_local = corolocal.local()

class RequestIdHook(tpool.ExecuteHook):
    request_id = None

    def setup(self):
        try:
            self.request_id = _eventlet_local.request_id
        except AttributeError:
            self.request_id = None
        else:
            if hasattr(self.meth, 'im_class'):
                name = '%s.%s' % (self.meth.im_class.__name__,
                                  self.meth.__name__)
            else:
                name = self.meth.__name__
            self.tracer = Tracer('tpool: %s' % name)
            self.tracer.begin()

    def teardown(self):
        if self.request_id != None:
            self.tracer.end()
            self.request_id = None

    def pre_meth(self):
        if self.request_id != None:
            _native_local.request_id = self.request_id

    def post_meth(self):
        if self.request_id != None:
            del _native_local.request_id

tpool.add_execute_hook(RequestIdHook)

def _dict_union(a, b):
    if a == None and b == None:
        return None
    elif a == None:
        return b
    elif b == None:
        return a
    else:
        r = dict(a)
        r.update(b)
        return r

def _trace_path(request_id):
    return os.path.join(CONF.trace_dir, '%s.trace' % request_id)

def _open_trace_file(request_id, extra_flags=0,):
    flags = os.O_RDWR | os.O_APPEND | extra_flags
    return os.fdopen(os.open(_trace_path(request_id), flags, 0666), 'a+')

def current_trace_id():
    try:
        return _current_request_id()
    except AttributeError:
        return None

def _current_request_id():
    try:
        return _native_local.request_id
    except AttributeError:
        # Might throw AttributeError as well!
        return _eventlet_local.request_id

def _traced_http_request(fn):
    def wrapper(*args, **kwargs):
        if not CONF.trace_enabled:
            return fn(*args, **kwargs)
        try:
            request_id = _current_request_id()
        except AttributeError:
            pass
        else:
            if 'headers' not in kwargs:
                kwargs['headers'] = {}
            kwargs['headers']['X-Trace-Id'] = request_id
        with Tracer(args[1], begin_args={'Method': kwargs.get('method', 'GET')}):
            return fn(*args, **kwargs)
    return wrapper

httplib2.Http.request = _traced_http_request(httplib2.Http.request)

BEGIN = 'B'
END = 'E'
METADATA = 'M'

@contextlib.contextmanager
def trace(request_id, args=None, resume=False):
    if not CONF.trace_enabled:
        yield
        return

    if not resume:
        # Make sure the file exists and is empty.
        with _open_trace_file(request_id, os.O_CREAT | os.O_TRUNC) as f:
            if not resume:
                f.write('[\n')

    assert not hasattr(_eventlet_local, 'requset_id')
    _eventlet_local.request_id = request_id
    try:
        # Hack: use "thread_name" metadata for args ...
        emit(METADATA, "thread_name",
             args=_dict_union({'name': 'no name'}, args))
        yield
    finally:
        del _eventlet_local.request_id

def emit(type, name=None, args=None, tags=None):
    try:
        request_id = _current_request_id()
    except AttributeError:
        # No request id, so we aren't tracing.
        return
    else:
        f = None
        try:
            f = _open_trace_file(request_id)
        except OSError, e:
            # If the trace file doesn't exist, then nobody called
            # trace_current_request.
            if e.errno != errno.ENOENT:
                raise
        else:
            categories = [os.path.basename(sys.argv[0])]
            if tags != None:
                categories.extend(tags)
            event = {'ph': type,
                     'ts': time.time() * 1e6,
                     'cat': ','.join(categories),
                     'pid': '%s:%s' % (request_id, os.getpid()),
                     'tid': str(thread.get_ident())}
            if args != None:
                event['args'] = args
            if name != None:
                event['name'] = name
            json.dump(event, f, sort_keys=True, indent=4)
            f.write(',\n')
        finally:
            if f != None:
                f.close()

class Tracer(object):
    def __init__(self, name, begin_args=None, end_args=None):
        self.begin_args = begin_args
        self.end_args = end_args
        self.name = name
        self.ended = False

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, type, value, traceback):
        self.end()

    def begin(self, args=None):
        if CONF.trace_enabled:
            emit(BEGIN, self.name, _dict_union(self.begin_args, args))

    def end(self, args=None):
        if CONF.trace_enabled and not self.ended:
            emit(END, self.name, _dict_union(self.end_args, args))
            self.ended = True

def trace_class_dict(class_name, class_dict):
    '''Call from a metaclass's __new__ method.'''
    # Wrap all of the functions in our @traced decorator. Take special care
    # for @classmethod and @staticmethod functions to wrap the functions
    # that they wrap; they're actually descriptor objects and very tricky!
    for key, value in class_dict.items():
        def traced_func(fn):
            return traced(name='%s.%s' % (class_name, fn.__name__))(fn)
        if isinstance(value, types.FunctionType):
            class_dict[key] = traced_func(value)
        elif isinstance(value, classmethod):
            class_dict[key] = classmethod(traced_func(value.__func__))
        elif isinstance(value, staticmethod):
            class_dict[key] = staticmethod(traced_func(value.__func__))

class TracedMetaClass(type):
    def __new__(mcs, name, bases, dict_):
        trace_class_dict(name, dict_)
        return super(TracedMetaClass, mcs).__new__(mcs, name, bases, dict_)

metaclass = TracedMetaClass

def traced(begin_args=None, end_args=None, begin_cb=None,
           end_cb=None, name_cb=None, name=None):
    if begin_cb == None:
        begin_cb = lambda fn, args, kwargs: None

    if end_cb == None:
        end_cb = lambda fn, ret, args, kwargs: None

    if name_cb == None:
        name_cb = lambda dflt, fn, args, kwargs: dflt

    def decorator(o):
        if isinstance(o, (types.TypeType, types.ClassType)):
            return class_decorator(o)
        elif isinstance(o, types.ModuleType):
            return module_decorator(o)
        elif isinstance(o, (types.FunctionType, types.MethodType)):
            return function_decorator(o)
        else:
            return o

    def class_decorator(cls):
        for name, method in inspect.getmembers(cls, inspect.ismethod):
            # If method is defined in cls and it's decorated with @classmethod,
            # then we want to wrap the pre-decorated method then apply
            # @classmethod again.
            if name in cls.__dict__ and isinstance(cls.__dict__[name], classmethod):
                setattr(cls, name,
                        classmethod(function_decorator(method.__func__)))
            else:
                setattr(cls, name, function_decorator(method))
        return cls

    def function_decorator(fn):
        begin_args_ = _dict_union({'Module': fn.__module__}, begin_args)

        if name != None:
            default_name = name
        elif hasattr(fn, 'im_class'):
            default_name = '%s.%s' % (fn.im_class.__name__, fn.__name__)
        else:
            default_name = fn.__name__

        def wrapper(*args, **kwargs):
            tracer = Tracer(name_cb(default_name, fn, args, kwargs),
                            _dict_union(begin_args_, end_args))
            tracer.begin(begin_cb(fn, args, kwargs))
            r = None
            try:
                r = fn(*args, **kwargs)
            finally:
                tracer.end(end_cb(fn, r, args, kwargs))
            return r
        
        wrapper.__name__ = fn.__name__
        return wrapper

    return decorator
