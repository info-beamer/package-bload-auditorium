#
# Part of info-beamer hosted. You can find the latest version
# of this file at:
#
# https://github.com/info-beamer/package-sdk
#
# Copyright (c) 2014-2024 Florian Wesch <fw@info-beamer.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     Redistributions of source code must retain the above copyright
#     notice, this list of conditions and the following disclaimer.
#
#     Redistributions in binary form must reproduce the above copyright
#     notice, this list of conditions and the following disclaimer in the
#     documentation and/or other materials provided with the
#     distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
# IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

VERSION = "2.0"

import os, re, sys, json, time, traceback, marshal, hashlib
import errno, socket, select, threading, Queue, ctypes
import pyinotify, requests
from functools import wraps
from collections import namedtuple
from tempfile import NamedTemporaryFile
from hosted.scheduler import timespec_from_config

_types = {}

class OptionValueWrapper(object):
    def __init__(self, value):
        self._value = value

class OptionSection(OptionValueWrapper):
    def is_selected(self, key):
        return key in self._value

class OptionExpandedSchedule(object):
    def __init__(self, value, schedules):
        self._value = value
        self._schedules = schedules

    def within_range(self, start, duration, probe):
        return start <= probe < start + duration

    def is_active_at(self, unix_time):
        if self._value == 'always':
            return True
        elif self._value == 'never':
            return False
        start, duration = self._schedules['range']
        if not self.within_range(start, duration, unix_time):
            return False
        for start, duration in self._schedules['expanded'][self._value]:
            if start > unix_time:
                return False
            elif self.within_range(start, duration, unix_time):
                return True
        return False

def init_types():
    def type(fn):
        _types[fn.__name__] = fn
        return fn

    @type
    def color(value):
        return value

    @type
    def string(value):
        return value

    @type
    def text(value):
        return value

    @type
    def section(value):
        return OptionSection(value)

    @type
    def boolean(value):
        return value

    @type
    def select(value):
        return value

    @type
    def duration(value):
        return value

    @type
    def integer(value):
        return value

    @type
    def float(value):
        return value

    @type
    def font(value):
        return value

    @type
    def device(value):
        return value

    @type
    def resource(value):
        return value

    @type
    def device_token(value):
        return value

    @type
    def json(value):
        return value

    @type
    def id(value):
        return value

    @type
    def playlist(value):
        return value

    @type
    def list_select(value):
        return value

    @type
    def custom(value):
        return value

    @type
    def date(value):
        return value

    @type
    def schedule(value):
        return timespec_from_config(value)

init_types()

def log(msg, name='hosted.py'):
    sys.stderr.write("[{}] {}\n".format(name, msg))

def abort_service(reason):
    log("restarting service (%s)" % reason)
    os._exit(0)
    time.sleep(2)
    os.kill(os.getpid(), 2)
    time.sleep(2)
    os.kill(os.getpid(), 15)
    time.sleep(2)
    os.kill(os.getpid(), 9)
    time.sleep(100)

CLOCK_MONOTONIC_RAW = 4 # see <linux/time.h>

class timespec(ctypes.Structure):
    _fields_ = [
        ('tv_sec', ctypes.c_long),
        ('tv_nsec', ctypes.c_long),
    ]

_librt = ctypes.CDLL('librt.so.1')
_clock_gettime = _librt.clock_gettime
_clock_gettime.argtypes = [ctypes.c_int, ctypes.POINTER(timespec)]

def monotonic_time():
    t = timespec()
    _clock_gettime(CLOCK_MONOTONIC_RAW , ctypes.pointer(t))
    return t.tv_sec + t.tv_nsec * 1e-9

_libc = ctypes.CDLL('libc.so.6')
_getrandom = _libc.getrandom
_getrandom.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]

_create_string_buffer = ctypes.create_string_buffer
_string_at = ctypes.string_at

def get_random_bytes(num_bytes):
    buf = _create_string_buffer(num_bytes)
    result = _getrandom(buf, num_bytes, 0)
    if result != num_bytes:
        raise OSError("Error getting random bytes from getrandom")
    return _string_at(buf, num_bytes)

class InfoBeamerQueryException(Exception):
    pass

class InfoBeamerQuery(object):
    def __init__(self, host='127.0.0.1', port=4444):
        self._sock = None
        self._conn = None
        self._host = host
        self._port = port
        self._timeout = 2
        self._version = None

    def _reconnect(self):
        if self._conn is not None:
            return
        try:
            self._sock = socket.create_connection((self._host, self._port), self._timeout)
            self._conn = self._sock.makefile()
            intro = self._conn.readline()
        except socket.timeout:
            self._reset()
            raise InfoBeamerQueryException("Timeout while reopening connection")
        except socket.error as err:
            self._reset()
            raise InfoBeamerQueryException("Cannot connect to %s:%s: %s" % (
                self._host, self._port, err))
        m = re.match("^Info Beamer PI ([^ ]+)", intro)
        if not m:
            self._reset()
            raise InfoBeamerQueryException("Invalid handshake. Not info-beamer?")
        self._version = m.group(1)

    def _parse_line(self):
        line = self._conn.readline()
        if not line:
            return None
        return line.rstrip()

    def _parse_multi_line(self):
        lines = []
        while 1:
            line = self._conn.readline()
            if not line:
                return None
            line = line.rstrip()
            if not line:
                break
            lines.append(line)
        return '\n'.join(lines)

    def _send_cmd(self, min_version, cmd, multiline=False):
        for retry in (1, 2):
            self._reconnect()
            if self._version <= min_version:
                raise InfoBeamerQueryException(
                    "This query is not implemented in your version of info-beamer. "
                    "%s or higher required, %s found" % (min_version, self._version)
                )
            try:
                self._conn.write(cmd + "\n")
                self._conn.flush()
                response = self._parse_multi_line() if multiline else self._parse_line()
                if response is None:
                    self._reset()
                    continue
                return response
            except socket.error:
                self._reset()
                continue
            except socket.timeout:
                self._reset()
                raise InfoBeamerQueryException("Timeout waiting for response")
            except Exception:
                self._reset()
                continue
        raise InfoBeamerQueryException("Failed to get a response")

    def _reset(self, close=True):
        if close:
            try:
                if self._conn: self._conn.close()
                if self._sock: self._sock.close()
            except:
                pass
        self._conn = None
        self._sock = None

    @property
    def addr(self):
        return "%s:%s" % (self._host, self._port)

    def close(self):
        self._reset()

    @property
    def ping(self):
        "tests if info-beamer is reachable"
        return self._send_cmd(
            "0.6", "*query/*ping",
        ) == "pong"

    @property
    def uptime(self):
        "returns the uptime in seconds"
        return int(self._send_cmd(
            "0.6", "*query/*uptime",
        ))

    @property
    def objects(self):
        "returns the number of allocated info-beamer objects"
        return int(self._send_cmd(
            "0.9.4", "*query/*objects",
        ))

    @property
    def version(self):
        "returns the running info-beamer version"
        return self._send_cmd(
            "0.6", "*query/*version",
        )

    @property
    def fps(self):
        "returns the FPS of the top level node"
        return float(self._send_cmd(
            "0.6", "*query/*fps",
        ))

    @property
    def display(self):
        "returns the display configuration"
        return json.loads(self._send_cmd(
            "1.0", "*query/*display",
        ))

    ResourceUsage = namedtuple("ResourceUsage", "user_time system_time memory")
    @property
    def resources(self):
        "returns information about used resources"
        return self.ResourceUsage._make(int(v) for v in self._send_cmd(
            "0.6", "*query/*resources",
        ).split(','))

    ScreenSize = namedtuple("ScreenSize", "width height")
    @property
    def screen(self):
        "returns the native screen size"
        return self.ScreenSize._make(int(v) for v in self._send_cmd(
            "0.8.1", "*query/*screen",
        ).split(','))

    @property
    def runid(self):
        "returns a unique run id that changes with every restart of info-beamer"
        return self._send_cmd(
            "0.9.0", "*query/*runid",
        )

    @property
    def nodes(self):
        "returns a list of nodes"
        nodes = self._send_cmd(
            "0.9.3", "*query/*nodes",
        ).split(',')
        return [] if not nodes[0] else nodes

    class Node(object):
        def __init__(self, ib, path):
            self._ib = ib
            self._path = path

        @property
        def mem(self):
            "returns the Lua memory usage of this node"
            return int(self._ib._send_cmd(
                "0.6", "*query/*mem/%s" % self._path
            ))

        @property
        def fps(self):
            "returns the framerate of this node"
            return float(self._ib._send_cmd(
                "0.6", "*query/*fps/%s" % self._path
            ))

        def io(self, raw=True):
            "creates a tcp connection to this node"
            status = self._ib._send_cmd(
                "0.6", "%s%s" % ("*raw/" if raw else '', self._path),
            )
            if status != 'ok!':
                raise InfoBeamerQueryException("Cannot connect to node %s" % self._path)
            sock = self._ib._sock
            sock.settimeout(None)
            return self._ib._conn

        @property
        def has_error(self):
            "queries the error flag"
            return bool(int(self._ib._send_cmd(
                "0.8.2", "*query/*has_error/%s" % self._path,
            )))

        @property
        def error(self):
            "returns the last Lua traceback"
            return self._ib._send_cmd(
                "0.8.2", "*query/*error/%s" % self._path, multiline=True
            )

        def __repr__(self):
            return "%s/%s" % (self._ib, self._path)

    def node(self, node):
        return self.Node(self, node)

    def __repr__(self):
        return "<info-beamer@%s>" % self.addr

class ParsedConfig(object):
    def __init__(self, parsed):
        self._parsed = parsed

    @property
    def metadata(self):
        return self._parsed['__metadata']

    @property
    def metadata_timezone(self):
        return self.metadata['timezone']

    @property
    def config_hash(self):
        return self.metadata['config_hash']

    @property
    def config_rev(self):
        return self.metadata['config_rev']

    def __contains__(self, key):
        return key in self._parsed

    def get(self, key, default=None):
        return self._parsed.get(key, default)

    def __getitem__(self, key):
        return self._parsed[key]

    def __getattr__(self, key):
        return self._parsed[key]

    def __repr__(self):
        return '<config @%s %s:%d>' % (
            self.metadata['node_path'],
            self.config_hash, self.config_rev,
        )

class Configuration(object):
    def __init__(self, path=''):
        self._path = path
        self._restart = False
        with open(os.path.join(self._path, "node.json")) as f:
            node_json = json.load(f)
            self._expanded_schedules = node_json.get('expand_schedules', False)
            self._options = node_json.get('options', [])
        self.parse_config_json()

    @property
    def path(self):
        return self._path

    def restart_on_update(self):
        log("going to restart when config is updated")
        self._restart = True

    def parse_config_json(self):
        with open(os.path.join(self._path, "config.json")) as f:
            config = json.load(f)

        if self._restart:
            return abort_service("restart_on_update set")

        def parse_metadata(metadata):
            parsed = {}
            for key, value in metadata.iteritems():
                if key == 'timezone':
                    import pytz
                    value = pytz.timezone(value)
                parsed[key] = value
            return parsed

        schedules = config.get('__schedules')

        def parse_recursive(options, config):
            parsed = {}
            for option in options:
                if not 'name' in option or not option['name'] in config:
                    continue
                if option['type'] == 'list':
                    items = []
                    for item in config[option['name']]:
                        items.append(parse_recursive(option['items'], item))
                    parsed[option['name']] = items
                    continue
                if option['type'] == 'schedule' and self._expanded_schedules:
                    parsed[option['name']] = OptionExpandedSchedule(
                        config[option['name']], schedules
                    )
                else:
                    parsed[option['name']] = _types[option['type']](config[option['name']])
            return parsed

        parsed = parse_recursive(self._options, config)
        parsed['__metadata'] = parse_metadata(config['__metadata'])
        log("updated %s" % os.path.join(self._path, 'config.json'))
        self._parsed, self._config = ParsedConfig(parsed), config

    @property
    def raw(self):
        return self._config

    @property
    def parsed(self):
        return self._parsed

    def __getitem__(self, key):
        return self._parsed[key]

    def __getattr__(self, key):
        return getattr(self._parsed, key)

class ConfigWatcherEventHandler(pyinotify.ProcessEvent):
    def my_init(self, watcher):
        self._watcher = watcher
        self._lock = threading.Lock()

    def process_default(self, event):
        with self._lock:
            self._watcher.on_changed(event.pathname)

class ConfigWatcher(object):
    def __init__(self):
        self._configs = {}
        self._wm = pyinotify.WatchManager()
        notifier = pyinotify.ThreadedNotifier(
            self._wm, ConfigWatcherEventHandler(watcher=self)
        )
        notifier.daemon = True
        notifier.start()

    def on_changed(self, pathname):
        full_name = os.path.relpath(pathname, '.')
        dirname = os.path.dirname(full_name)
        basename = os.path.basename(full_name)
        if basename == 'node.json':
            abort_service("node.json changed")
        elif basename == 'config.json':
            log('%s changed!' % full_name)
            configuration, _ = self._configs[dirname]
            configuration.parse_config_json()
        elif basename.endswith('.py'):
            abort_service("python file changed")

    def watch(self, configuration):
        if configuration.path in self._configs:
            return
        wd = self._wm.add_watch(configuration.path, pyinotify.IN_MOVED_TO)
        log("now watching config in %s" % (configuration.path or '<root>',))
        self._configs[configuration.path] = configuration, wd[configuration.path]

    def unwatch(self, configuration):
        if configuration.path not in self._configs:
            return
        _, wd = self._configs[configuration.path]
        self._wm.rm_watch(wd)
        del self._configs[configuration.path]
        log("no longer watching config in %s" % (configuration.path or '<root>',))

class RPC(object):
    def __init__(self, path):
        self._path = path
        self._callbacks = {}
        self._lock = threading.Lock()
        self._con = None
        thread = threading.Thread(target=self._listen_thread)
        thread.daemon = True
        thread.start()

    def _get_connection(self):
        if self._con is None:
            try:
                self._con = InfoBeamerQuery().node(
                    self._path + "/rpc/python"
                ).io(raw=True)
            except InfoBeamerQueryException:
                return None
        return self._con

    def _close_connection(self):
        with self._lock:
            if self._con:
                try:
                    self._con.close()
                except:
                    pass
            self._con = None

    def _send(self, line):
        with self._lock:
            con = self._get_connection()
        if con is None:
            return
        try:
            con.write(line + '\n')
            con.flush()
            return True
        except:
            self._close_connection()
            return False

    def _recv(self):
        with self._lock:
            con = self._get_connection()
        try:
            return con.readline()
        except:
            self._close_connection()

    def _listen_thread(self):
        while 1:
            line = self._recv()
            if not line:
                self._close_connection()
                time.sleep(0.5)
                continue
            try:
                args = json.loads(line)
                method = args.pop(0)
                callback = self._callbacks.get(method)
                if callback:
                    callback(*args)
                else:
                    log("callback '%s' not found" % (method,))
            except:
                traceback.print_exc()

    def register(self, name, fn):
        self._callbacks[name] = fn

    def register_callbacks(self, callbacks):
        self._callbacks.update(callbacks)

    def call(self, fn):
        self.register(fn.__name__, fn)

    def __getattr__(self, method):
        def call(*args):
            args = list(args)
            args.insert(0, method)
            return self._send(json.dumps(
                args,
                ensure_ascii=False,
                separators=(',',':'),
            ).encode('utf8'))
        return call

    get_method = __getattr__

class Cache(object):
    def __init__(self, scope='default'):
        self._touched = set()
        self._prefix = 'cache-%s-' % scope

    def key_to_fname(self, key):
        return self._prefix + hashlib.md5(key).hexdigest()

    def has(self, key, max_age=None):
        try:
            stat = os.stat(self.key_to_fname(key))
            if max_age is not None:
                now = time.time()
                if now > stat.st_mtime + max_age:
                    return False
            return True
        except:
            return False

    def get(self, key, max_age=None):
        try:
            with open(self.file_ref(key)) as f:
                if max_age is not None:
                    stat = os.fstat(f.fileno())
                    now = time.time()
                    if now > stat.st_mtime + max_age:
                        return None
                return f.read()
        except:
            return None

    def get_json(self, key, max_age=None):
        data = self.get(key, max_age)
        if data is None:
            return None
        return json.loads(data)

    def set(self, key, value):
        with open(self.file_ref(key), "wb") as f:
            f.write(value)

    def set_json(self, key, data):
        self.set(key, json.dumps(data))

    def file_ref(self, key):
        fname = self.key_to_fname(key)
        self._touched.add(fname)
        return fname

    def start(self):
        self._touched = set()

    def prune(self):
        existing = set()
        for fname in os.listdir("."):
            if not fname.startswith(self._prefix):
                continue
            existing.add(fname)
        prunable = existing - self._touched
        for fname in prunable:
            try:
                log("pruning %s" % fname)
                os.unlink(fname)
            except:
                pass

    def clear(self):
        self.start()
        self.prune()

    def call(self, max_age=None):
        def deco(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                key = marshal.dumps((fn.__name__, args, kwargs), 2)
                cached = self.get(key, max_age)
                if cached is not None:
                    return marshal.loads(cached)
                val = fn(*args, **kwargs)
                self.set(key, marshal.dumps(val, 2))
                return val
            return wrapper
        return deco

    def file_producer(self, max_age=None):
        def deco(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                key = marshal.dumps((fn.__name__, args, kwargs), 2)
                if self.has(key, max_age):
                    return self.file_ref(key)
                val = fn(*args, **kwargs)
                if val is None:
                    return None
                self.set(key, val)
                return self.file_ref(key)
            return wrapper
        return deco

class NodeServiceDataHandler(object):
    def __init__(self, port):
        self._lock = threading.Lock()
        self._callbacks = {}

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(('127.0.0.1', port))

        thread = threading.Thread(target=self._listen_thread)
        thread.daemon = True
        thread.start()

    def register_callbacks(self, callbacks):
        with self._lock:
            self._callbacks.update(callbacks)

    def remove_callback(self, path):
        with self._lock:
            if path in self._callbacks:
                del self._callbacks[path]

    def _listen_thread(self):
        while 1:
            try:
                pkt, _ = self._sock.recvfrom(2**16)
                path, data = pkt.split(':', 1)
                with self._lock:
                    callback = self._callbacks.get(path)
                    if callback:
                        callback(data)
                    else:
                        log("callback '%s' not found" % (path,))
            except Exception as err:
                traceback.print_exc()

class Node(object):
    def __init__(self, node):
        self._node = node
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rpc = None
        self._service_data = None

    def send_raw(self, raw):
        log("sending %r" % (raw,))
        self._sock.sendto(raw, ('127.0.0.1', 4444))

    def send(self, data):
        self.send_raw(self._node + data)

    def send_json(self, path, data):
        self.send('%s:%s' % (path, json.dumps(
            data,
            ensure_ascii=False,
            separators=(',',':'),
        ).encode('utf8')))

    @property
    def is_top_level(self):
        return self._node == "root"

    @property
    def path(self):
        return self._node

    def write_file(self, filename, content):
        f = NamedTemporaryFile(prefix='.hosted-py-tmp', dir=os.getcwd())
        try:
            f.write(content)
        except:
            traceback.print_exc()
            f.close()
            raise
        else:
            f.delete = False
            f.close()
            os.rename(f.name, filename)

    def write_json(self, filename, data):
        self.write_file(filename, json.dumps(
            data,
            ensure_ascii=False,
            separators=(',',':'),
        ).encode('utf8'))

    class Sender(object):
        def __init__(self, node, path):
            self._node = node
            self._path = path

        def __call__(self, data):
            if isinstance(data, (dict, list)):
                self._node.send_json(self._path, data)
            else:
                self._node.send("%s:%s" % (self._path, data))

    def __getitem__(self, path):
        return self.Sender(self, path)

    def __call__(self, data):
        return self.Sender(self, '')(data)

    def connect(self, suffix=""):
        ib = InfoBeamerQuery()
        return ib.node(self.path + suffix).io(raw=True)

    def rpc(self, **callbacks):
        if not self._rpc:
            self._rpc = RPC(self.path)
        self._rpc.register_callbacks(callbacks)
        return self._rpc

    def service_data(self, **callbacks):
        if not self._service_data:
            self._service_data = NodeServiceDataHandler(
                port = int(os.environ['SERVICE_DATA_PORT'])
            )
        self._service_data.register_callbacks(callbacks)
        return self._service_data

    def cache(self, scope='default'):
        return Cache(scope)

    def scratch_cached(self, filename, generator):
        cached = os.path.join(os.environ['SCRATCH'], filename)

        if not os.path.exists(cached):
            f = NamedTemporaryFile(prefix='scratch-cached-tmp', dir=os.environ['SCRATCH'])
            try:
                generator(f)
            except:
                raise
            else:
                f.delete = False
                f.close()
                os.rename(f.name, cached)

        if os.path.exists(filename):
            try:
                os.unlink(filename)
            except:
                pass
        os.symlink(cached, filename)

class APIError(Exception):
    pass

class APIProxy(object):
    def __init__(self, apis, api_name):
        self._apis = apis
        self._api_name = api_name

    @property
    def url(self):
        index = self._apis.get_api_index()
        if not self._api_name in index:
            raise APIError("api '%s' not available" % (self._api_name,))
        return index[self._api_name]['url']

    def unwrap(self, r):
        r.raise_for_status()
        if r.status_code == 304:
            return None
        if r.headers['content-type'] == 'application/json':
            resp = r.json()
            if not resp['ok']:
                raise APIError(u"api call failed: %s" % (
                    resp.get('error', '<unknown error>'),
                ))
            return resp.get(self._api_name)
        else:
            return r.content

    def add_default_args(self, kwargs):
        if not 'timeout' in kwargs:
            kwargs['timeout'] = 10
        return kwargs

    def get(self, **kwargs):
        try:
            return self.unwrap(self._apis.session.get(
                url = self.url,
                **self.add_default_args(kwargs)
            ))
        except APIError:
            raise
        except Exception as err:
            raise APIError(err)

    def post(self, **kwargs):
        try:
            return self.unwrap(self._apis.session.post(
                url = self.url,
                **self.add_default_args(kwargs)
            ))
        except APIError:
            raise
        except Exception as err:
            raise APIError(err)

    def delete(self, **kwargs):
        try:
            return self.unwrap(self._apis.session.delete(
                url = self.url,
                **self.add_default_args(kwargs)
            ))
        except APIError:
            raise
        except Exception as err:
            raise APIError(err)


class OnDeviceAPIs(object):
    def __init__(self, config):
        self._config = config
        self._index = None
        self._valid_until = 0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'hosted.py version/%s' % (VERSION,)
        })

    def update_apis(self):
        log("fetching api index")
        r = self._session.get(
            url = self._config.metadata['api'],
            timeout = 5,
        )
        r.raise_for_status()
        resp = r.json()
        if not resp['ok']:
            raise APIError("cannot retrieve api index")
        self._index = resp['apis']
        self._valid_until = resp['valid_until'] - 300

    def get_api_index(self):
        with self._lock:
            now = time.time()
            if now > self._valid_until:
                self.update_apis()
            return self._index

    @property
    def session(self):
        return self._session

    def list(self):
        try:
            index = self.get_api_index()
            return sorted(index.keys())
        except Exception as err:
            raise APIError(err)

    def __getitem__(self, api_name):
        return APIProxy(self, api_name)

    def __getattr__(self, api_name):
        return APIProxy(self, api_name)

class HostedAPI(object):
    def __init__(self, api, on_device_token):
        self._api = api
        self._on_device_token = on_device_token
        self._lock = threading.Lock()
        self._next_refresh = 0
        self._api_key = None
        self._uses = 0
        self._expire = 0
        self._base_url = None
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'hosted.py version/%s - on-device' % (VERSION,)
        })

    def use_api_key(self):
        with self._lock:
            now = time.time()
            self._uses -= 1
            if self._uses <= 0:
                log('hosted API adhoc key used up')
                self._api_key = None
            elif now > self._expire:
                log('hosted API adhoc key expired')
                self._api_key = None
            else:
                log('hosted API adhoc key usage: %d uses, %ds left' %(
                    self._uses, self._expire - now
                ))
            if self._api_key is None:
                if time.time() < self._next_refresh:
                    return None
                log('refreshing hosted API adhoc key')
                self._next_refresh = time.time() + 15
                try:
                    r = self._api['api_key'].get(
                        params = dict(
                            on_device_token = self._on_device_token
                        ),
                        timeout = 5,
                    )
                except:
                    return None
                self._api_key = r['api_key']
                self._uses = r['uses']
                self._expire = now + r['expire'] - 1
                self._base_url = r['base_url']
            return self._api_key

    def add_default_args(self, kwargs):
        if not 'timeout' in kwargs:
            kwargs['timeout'] = 10
        return kwargs

    def ensure_api_key(self, kwargs):
        api_key = self.use_api_key()
        if api_key is None:
            raise APIError('cannot retrieve API key')
        kwargs['auth'] = ('', api_key)

    def get(self, endpoint, **kwargs):
        try:
            self.ensure_api_key(kwargs)
            r = self._session.get(
                url = self._base_url + endpoint,
                **self.add_default_args(kwargs)
            )
            r.raise_for_status()
            return r.json()
        except APIError:
            raise
        except Exception as err:
            raise APIError(err)

    def post(self, endpoint, **kwargs):
        try:
            self.ensure_api_key(kwargs)
            r = self._session.post(
                url = self._base_url + endpoint,
                **self.add_default_args(kwargs)
            )
            r.raise_for_status()
            return r.json()
        except APIError:
            raise
        except Exception as err:
            raise APIError(err)

    def delete(self, endpoint, **kwargs):
        try:
            self.ensure_api_key(kwargs)
            r = self._session.delete(
                url = self._base_url + endpoint,
                **self.add_default_args(kwargs)
            )
            r.raise_for_status()
            return r.json()
        except APIError:
            raise
        except Exception as err:
            raise APIError(err)

class DeviceKV(object):
    class Error(Exception):
        pass

    def __init__(self, api):
        self._api = api
        self._cache = {}
        self._cache_complete = False
        self._use_cache = True
        self._next_refresh = monotonic_time() + 3600

    def cache_enabled(self, enabled):
        self._use_cache = enabled
        self._cache = {}
        self._cache_complete = False

    def maybe_refresh(self):
        now = monotonic_time()
        if now < self._next_refresh:
            return
        self._next_refresh = monotonic_time() + 3600
        try:
            self._api['kv'].post(data={})
        except Exception as err:
            raise self.Error(err)

    def __setitem__(self, key, value):
        if self._use_cache:
            if key in self._cache and self._cache[key] == value:
                self.maybe_refresh()
                return
        try:
            self._api['kv'].post(
                data = {
                    key: value
                }
            )
            if self._use_cache:
                self._next_refresh = monotonic_time() + 3600
                self._cache[key] = value
        except Exception as err:
            raise self.Error(err)

    def __getitem__(self, key):
        if self._use_cache:
            if key in self._cache:
                self.maybe_refresh()
                return self._cache[key]
        try:
            result = self._api['kv'].get(
                params = dict(
                    keys = key,
                ),
                timeout = 5,
            )['v']
            if key not in result:
                raise KeyError(key)
            value = result[key]
            if self._use_cache:
                self._next_refresh = monotonic_time() + 3600
                self._cache[key] = value
            return value
        except KeyError:
            raise
        except Exception as err:
            raise self.Error(err)

    # http api cannot reliably determine if a key has
    # been deleted, so __delitem__ always succeeds and
    # does not throw KeyError for missing keys.
    def __delitem__(self, key):
        if self._use_cache and self._cache_complete:
            if key not in self._cache:
                self.maybe_refresh()
                return
        try:
            self._api['kv'].delete(
                params = dict(
                    keys = key,
                ),
                timeout = 5,
            )
            if self._use_cache and key in self._cache:
                self._next_refresh = monotonic_time() + 3600
                if key in self._cache:
                    del self._cache[key]
        except Exception as err:
            raise self.Error(err)

    def update(self, dct):
        if self._use_cache:
            for key, value in dct.items():
                if key in self._cache and self._cache[key] == value:
                    dct.pop(key)
            if not dct:
                self.maybe_refresh()
                return
        try:
            self._api['kv'].post(
                data = dct
            )
            if self._use_cache:
                self._next_refresh = monotonic_time() + 3600
                for key, value in dct.iteritems():
                    self._cache[key] = value
        except Exception as err:
            raise self.Error(err)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default
        except:
            raise

    def items(self):
        if self._use_cache and self._cache_complete:
            self.maybe_refresh()
            return self._cache.items()
        try:
            result = self._api['kv'].get(
                timeout = 5,
            )['v']
            if self._use_cache:
                self._next_refresh = monotonic_time() + 3600
                for key, value in result.iteritems():
                    self._cache[key] = value
                self._cache_complete = True
            return result.items()
        except Exception as err:
            raise self.Error(err)

    iteritems = items

    def clear(self):
        try:
            self._api['kv'].delete()
            if self._use_cache:
                self._cache = {}
                self._cache_complete = False
        except Exception as err:
            raise self.Error(err)


class GPIO(object):
    def __init__(self):
        self._pin_fd = {}
        self._state = {}
        self._fd_2_pin = {}
        self._poll = select.poll()
        self._lock = threading.Lock()

    def setup_pin(self, pin, direction="in", invert=False):
        if not os.path.exists("/sys/class/gpio/gpio%d" % pin):
            with open("/sys/class/gpio/export", "wb") as f:
                f.write(str(pin))
        # mdev is giving the newly create GPIO directory correct permissions.
        for i in range(10):
            try:
                with open("/sys/class/gpio/gpio%d/active_low" % pin, "wb") as f:
                    f.write("1" if invert else "0")
                break
            except IOError as err:
                if err.errno != errno.EACCES:
                    raise
            time.sleep(0.1)
            log("waiting for GPIO permissions")
        else:
            raise IOError(errno.EACCES, "Cannot access GPIO")
        with open("/sys/class/gpio/gpio%d/direction" % pin, "wb") as f:
            f.write(direction)

    def set_pin_value(self, pin, high):
        with open("/sys/class/gpio/gpio%d/value" % pin, "wb") as f:
            f.write("1" if high else "0")

    def monitor(self, pin, invert=False):
        if pin in self._pin_fd:
            return
        self.setup_pin(pin, direction="in", invert=invert)
        with open("/sys/class/gpio/gpio%d/edge" % pin, "wb") as f:
            f.write("both")
        fd = os.open("/sys/class/gpio/gpio%d/value" % pin, os.O_RDONLY)
        self._state[pin] = bool(int(os.read(fd, 5)))
        self._fd_2_pin[fd] = pin
        self._pin_fd[pin] = fd
        self._poll.register(fd, select.POLLPRI | select.POLLERR)

    def poll(self, timeout=1000):
        changes = []
        for fd, evt in self._poll.poll(timeout):
            os.lseek(fd, 0, 0)
            state = bool(int(os.read(fd, 5)))
            pin = self._fd_2_pin[fd]
            with self._lock:
                prev_state, self._state[pin] = self._state[pin], state
            if state != prev_state:
                changes.append((pin, state))
        return changes

    def poll_forever(self):
        while 1:
            for event in self.poll():
                yield event

    def on(self, pin):
        with self._lock:
            return self._state.get(pin, False)

class SyncerAPI(object):
    def __init__(self):
        self._session = requests.Session()

    def unwrap(self, r):
        r.raise_for_status()
        return r.json()

    def get(self, path, params={}):
        return self.unwrap(self._session.get(
            'http://127.0.0.1:81%s' % path,
            params=params, timeout=10
        ))

    def post(self, path, data={}):
        return self.unwrap(self._session.post(
            'http://127.0.0.1:81%s' % path,
            data=data, timeout=10
        ))

class ProofOfPlay(object):
    def __init__(self, api, dirname):
        self._api = api
        self._prefix = os.path.join(os.environ['SCRATCH'], dirname)
        try:
            os.makedirs(self._prefix)
        except:
            pass

        pop_info = self._api.pop.get()

        self._max_delay = pop_info['max_delay']
        self._max_lines = pop_info['max_lines']
        self._submission_min_delay = pop_info['submission']['min_delay']
        self._submission_error_delay = pop_info['submission']['error_delay']

        self._q = Queue.Queue()
        self._log = None

        thread = threading.Thread(target=self._submit_thread)
        thread.daemon = True
        thread.start()

        thread = threading.Thread(target=self._writer_thread)
        thread.daemon = True
        thread.start()

    def _submit(self, fname, queue_size):
        with open(fname, 'rb') as f:
            return self._api.pop.post(
                timeout = 10,
                data = {
                    'queue_size': queue_size,
                },
                files={
                    'pop-v1': f,
                }
            )

    def _submit_thread(self):
        time.sleep(3)
        while 1:
            delay = self._submission_min_delay
            try:
                log('[pop][submit] gathering files')
                files = [
                    fname for fname
                    in os.listdir(self._prefix)
                    if fname.startswith('submit-')
                ]
                log('[pop][submit] %d files' % len(files))
                for fname in files:
                    fullname = os.path.join(self._prefix, fname)
                    if os.stat(fullname).st_size == 0:
                        os.unlink(fullname)
                        continue
                    try:
                        log('[pop][submit] submitting %s' % fullname)
                        status = self._submit(fullname, len(files))
                        if status['disabled']:
                            log('[pop][submit] WARNING: Proof of Play disabled for this device. Submission discarded')
                        else:
                            log('[pop][submit] success')
                    except APIError as err:
                        log('[pop][submit] failure to submit log %s: %s' % (
                            fullname, err
                        ))
                        delay = self._submission_error_delay
                        break
                    os.unlink(fullname)
                    break
                if not files:
                    delay = 10
            except Exception as err:
                log('[pop][submit] error: %s' % err)
            log('[pop][submit] sleeping %ds' % delay)
            time.sleep(delay)

    def reopen_log(self):
        log_name = os.path.join(self._prefix, 'current.log')
        if self._log is not None:
            self._log.close()
            self._log = None
        if os.path.exists(log_name):
            os.rename(log_name, os.path.join(
                self._prefix, 'submit-%s.log' % get_random_bytes(16).encode('hex')
            ))
        self._log = open(log_name, 'wb')
        return self._log

    def _writer_thread(self):
        submit, log_file, lines = monotonic_time() + self._max_delay, self.reopen_log(), 0
        while 1:
            reopen = False
            max_wait = max(0.1, submit - monotonic_time())
            log('[pop] got %d lines. waiting %ds for more log lines' % (lines, max_wait))
            try:
                line = self._q.get(block=True, timeout=max_wait)
                log_file.write(line + '\n')
                log_file.flush()
                os.fsync(log_file.fileno())
                lines += 1
                log('[pop] line added: %r' % line)
            except Queue.Empty:
                if lines == 0:
                    submit += self._max_delay # extend deadline
                else:
                    reopen = True
            except Exception as err:
                log("[pop] error writing pop log line")
            if lines >= self._max_lines:
                reopen = True
            if reopen:
                log('[pop] closing log of %d lines' % lines)
                submit, log_file, lines = monotonic_time() + self._max_delay, self.reopen_log(), 0

    def log(self, play_start, duration, asset_id, asset_filename):
        uuid = "%08x%s" % (
            time.time(), get_random_bytes(12).encode('hex')
        )
        self._q.put(json.dumps([
                uuid,
                play_start,
                duration,
                0 if asset_id is None else asset_id,
                asset_filename,
            ],
            ensure_ascii = False,
            separators = (',',':'),
        ).encode('utf8'))

class Device(object):
    def __init__(self, kv, api):
        self._socket = None
        self._gpio = GPIO()
        self._kv = kv
        self._api = api

    @property
    def kv(self):
        return self._kv

    @property
    def gpio(self):
        return self._gpio

    @property
    def serial(self):
        return os.environ['SERIAL']

    @property
    def screen_resolution(self):
        with open("/sys/class/graphics/fb0/virtual_size", "rb") as f:
            return [int(val) for val in f.read().strip().split(',')]

    @property
    def screen_w(self):
        return self.screen_resolution[0]

    @property
    def screen_h(self):
        return self.screen_resolution[1]

    @property
    def syncer_api(self):
        return SyncerAPI()

    def ensure_connected(self):
        if self._socket:
            return True
        try:
            log("establishing upstream connection")
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.connect(os.getenv('SYNCER_SOCKET', "/tmp/syncer"))
            return True
        except Exception as err:
            log("cannot connect to upstream socket: %s" % (err,))
            return False

    def send_raw(self, raw):
        try:
            if self.ensure_connected():
                self._socket.send(raw + '\n')
        except Exception as err:
            log("cannot send to upstream: %s" % (err,))
            if self._socket:
                self._socket.close()
            self._socket = None

    def send_upstream(self, **data):
        self.send_raw(json.dumps(data))

    def turn_screen_off(self):
        self.send_raw("tv off")

    def turn_screen_on(self):
        self.send_raw("tv on")

    def screen(self, on=True):
        if on:
            self.turn_screen_on()
        else:
            self.turn_screen_off()

    def reboot(self):
        self.send_raw("system reboot")

    def halt_until_powercycled(self):
        self.send_raw("system halt")

    def restart_infobeamer(self):
        self.send_raw("infobeamer restart")

    def verify_cache(self):
        self.send_raw("syncer verify_cache")

    def pop(self, dirname='pop'):
        return ProofOfPlay(self._api, dirname)

    def hosted_api(self, on_device_token):
        return HostedAPI(self._api, on_device_token)

if __name__ == "__main__":
    print("nothing to do here")
    sys.exit(1)
else:
    log("hosted support library version %s" % (VERSION,))

    node = NODE = Node(os.environ['NODE'])
    config = CONFIG = Configuration()
    api = API = OnDeviceAPIs(CONFIG)
    device = DEVICE = Device(
        kv = DeviceKV(api),
        api = api,
    )

    config_watcher = ConfigWatcher()
    config_watcher.watch(config)

    log("ready to go!")
