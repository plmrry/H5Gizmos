
from . import H5Gizmos
from . import gz_resources

from aiohttp import web
import aiohttp
import asyncio
import weakref
import mimetypes
import os

DEFAULT_PORT = 9091
GET = "GET"
POST = "POST"
WS = "ws"
REQUEST_METHODS = frozenset([GET, POST, WS])
#UTF8 = "utf-8"


def get_file_bytes(path):
    # xxxx what about binary files?
    f = open(path, "rb")
    bytes_content = f.read()
    return bytes_content

class WebInterface:
    "External interface encapsulation to help support debugging and testing."

    def __init__(
        self,
        respond = web.Response,
        stream_respond = web.StreamResponse,
        ws_respond = web.WebSocketResponse,
        get_file_bytes = get_file_bytes,
        file_exists = os.path.isfile,
        app_factory=web.Application,
        async_run=web._run_app,
    ):
        self.respond = respond
        self.stream_respond = stream_respond
        self.ws_respond = ws_respond
        self.get_file_bytes = get_file_bytes
        self.file_exists = file_exists
        self.app_factory = app_factory
        self.async_run = async_run


STDInterface = WebInterface()

def gizmo_task_server(
        prefix="gizmo", 
        server="localhost", 
        port=DEFAULT_PORT, 
        interface=STDInterface,
        **args,
        ):
    S = GzServer(
        prefix=prefix,
        server=server,
        port=port,
        interface=interface,
    )
    S.run_in_task(app_factory=interface.app_factory, async_run=interface.async_run, **args)
    return S

def gizmo_standalone_server(
        prefix="gizmo", 
        server="localhost", 
        port=DEFAULT_PORT, 
        interface=STDInterface,
        **args,
        ):
    S = GzServer(
        prefix=prefix,
        server=server,
        port=port,
        interface=interface,
    )
    S.run_standalone(app_factory=interface.app_factory, async_run=interface.async_run, **args)
    return S


async def run_gizmo_standalone(server, gizmo, delay=0.1, interface=STDInterface, **args):
    server_task = server.run_in_task(
        app_factory=interface.app_factory, 
        async_run=interface.async_run, 
        **args)
    async def start_gizmo():
        import webbrowser
        await asyncio.sleep(delay)
        webbrowser.open(gizmo._entry_url)
    start_task = H5Gizmos.schedule_task(start_gizmo())
    await start_task
    #await server_task
    # terminate with control-c

def standalone_gizmo(server, gizmo, run_forever=True, delay=0.1, interface=STDInterface, **args):
    start_server = run_gizmo_standalone(server, gizmo, delay, interface, **args)
    asyncio.get_event_loop().run_until_complete(start_server)
    if run_forever:
        print ("entering run_forever loop -- terminate with control-C")
        asyncio.get_event_loop().run_forever()


class GzServer:

    verbose = False

    def __init__(
            self, 
            prefix="gizmo", 
            server="localhost", 
            port=DEFAULT_PORT, 
            interface=STDInterface,
            ):
        self.prefix = prefix
        self.server = server
        self.port = port
        self.interface = interface
        self.status = "initialized"
        self.task = None
        self.app = None
        self.stopped = False
        self.cancelled = False
        self.identifier_to_manager = {}
        self.counter = 0

    def gizmo(
            self, 
            title="Gizmo",
            packet_limit=1000000, 
            auto_flush=True,
            entry_filename="index.html"
            ):
        result = H5Gizmos.Gizmo()
        handler = GizmoPipelineSocketHandler(result, packet_limit=packet_limit, auto_flush=auto_flush)
        result._set_pipeline(handler.pipeline)
        mgr = self.get_new_manager(websocket_handler=handler)
        result._set_manager(self, mgr)
        result._configure_entry_page(title=title, filename=entry_filename)
        return result

    def get_new_manager(self, websocket_handler=None):
        c = self.counter
        self.counter = c + 1
        identifier = "MGR" + str(c)
        result = GizmoManager(identifier, self, websocket_handler)
        self.identifier_to_manager[identifier] = result
        return result

    def run_standalone(self, app_factory=web.Application, sync_run=web.run_app, **args):
        app = self.get_app(app_factory=app_factory)
        self.status = "running standalone"
        result = sync_run(app, port=self.port, **args)
        self.status = "done standalone"
        return result

    def run_in_task(self, app_factory=web.Application, async_run=web._run_app, **args):
        loop = asyncio.get_event_loop()
        app = self.get_app(app_factory=app_factory)
        self.status = "making runner"
        if self.verbose:
            print("making runner")
        runner = self.make_runner(app, async_run=async_run, **args)
        if self.verbose:
            print("creating task")
        task = loop.create_task(runner)
        self.task = task
        return task

    def get_app(self, app_factory=web.Application):
        self.app = app_factory()
        self.add_routes()
        self.app.on_shutdown.append(self.on_shutdown)
        return self.app

    async def on_shutdown(self, app):
        # https://docs.aiohttp.org/en/v0.22.4/web.html#aiohttp-web-graceful-shutdown
        for mgr in self.identifier_to_manager.values():
            h = mgr.web_socket_handler
            if h is not None:
                ws = h.ws
                if ws is not None:
                    await ws.close(code=999, message='Server shutdown')
          
    async def make_runner(self, app, async_run=web._run_app, **args):
        self.status = "starting runner"
        #app = self.get_app()
        try:
            port = self.port
            #pr ("runner using port", port)
            await async_run(app, port=port, **args)
        except asyncio.CancelledError:
            self.status = "app has been cancelled,"
            #pr(self.status)
            self.cancelled = True
        finally:
            self.status = "app has stopped."
            #pr(self.status)
            self.stopped = True

    def add_routes(self):
        app = self.app
        prefix = "/" + self.prefix
        app.router.add_route(GET, prefix + '/http/{tail:.*}', self.handle_http_get)
        app.router.add_route(POST, prefix + '/http/{tail:.*}', self.handle_http_post)
        app.router.add_route(GET, prefix + '/ws/{tail:.*}', self.handle_web_socket)

    async def handle(self, request, method="GET", interface=None):
        if interface is None:
            interface = self.interface
        #pr(" ... server handling", request.path)
        i2m = self.identifier_to_manager
        try:
            info = RequestUrlInfo(request, self.prefix)
            identifier = info.identifier
            mgr = i2m.get(identifier)
            assert mgr is not None, "could not resolve " + repr(identifier)
            #pr(" ... delegate handle to mgr", mgr)
            return await mgr.handle(method, info, request, interface=interface)
        except AssertionError as e:
            #pr("... 404 for assertion failure: ", e)
            return interface.stream_respond(status=404, reason=repr(e))

    def handle_http_get(self, request, interface=None):
        #pr(" ... server get", request.path)
        if interface is None:
            interface = self.interface
        return self.handle(request, method=GET, interface=interface)

    def handle_http_post(self, request, interface=None):
        #pr(" ... server post", request.path)
        if interface is None:
            interface = self.interface
        return self.handle(request, method=POST, interface=interface)

    def handle_web_socket(self, request, interface=None):
        #pr(" ... server socket", request.path)
        if interface is None:
            interface = self.interface
        return self.handle(request, method=WS, interface=interface)

    async def shutdown(self):
        app = self.app
        if self.task is not None:
            self.task.cancel()
        if app is not None:
            # https://stackoverflow.com/questions/55236254/cant-stop-aiohttp-websocket-server
            await app.shutdown()
            await app.cleanup()
            # should also clean up any outstanding web sockets xxxx ????

class RequestUrlInfo:

    request_methods = ("http", "ws")

    def __init__(self, request, prefix):
        self.request = request
        path = self.path = request.path
        sp = self.splitpath = path.split("/")
        ln = len(sp)
        # ??? xxx eventually allow mounting directories?
        assert 4 <= ln <= 5, "expected 4 or 5 components to path: " + repr(sp)
        assert sp[0] == "", "path should start with slash: " + repr(path)
        assert sp[1] == prefix, "path should have prefix: " + repr((prefix, path))
        method = self.method = sp[2]
        assert method in self.request_methods, "unknown request method: " + repr((method, path))
        self.identifier = sp[3]
        self.filename = None
        if ln == 5:
            self.filename = sp[4]

class GizmoManager:

    def __init__(self, identifier, server, websocket_handler=None):
        #self.server = server  # xxx maybe make this a weak ref?
        self.identifier = identifier
        #self.web_socket = None
        self.web_socket_handler = websocket_handler
        self.filename_to_http_handler = {}
        #self.url_path = "/%s/%s" % (server.prefix, identifier)
        self.prefix = server.prefix
        #pr(self.identifier, "manager init with socket handler", self.web_socket_handler)

    def add_file(self, at_path, filename=None, content_type=None, interface=STDInterface):
        if filename is None:
            filename = os.path.split(at_path)[-1]
        handler = FileGetter(at_path, filename, self, content_type, interface=interface)
        return self.add_http_handler(filename, handler)

    def add_http_handler(self, filename, handler):
        self.filename_to_http_handler[filename] = handler
        return handler

    async def handle(self, method, info, request, interface=STDInterface):
        #pr("... mgr handling", request.path, "method", method)
        filename = info.filename
        f2h = self.filename_to_http_handler
        if method == WS:
            assert filename is None, "WS request should have no filename " + repr(info.splitpath)
            return await self.handle_ws(info, request, interface)
        else:
            assert filename is not None, "HTTP requests should have a filename " + repr(info.splitpath)
            handler = f2h.get(filename)
            assert handler is not None, "No handler for filename " + repr(info.splitpath)
            #pr("... mgr delegating to handler", handler)
            if method == GET:
                return handler.handle_get(info, request, interface=interface)
            elif method == POST:
                return handler.handle_post(info, request, interface=interface)
            else:
                raise AssertionError("unknown http method: " + repr(method))

    async def handle_ws(self, info, request, interface=STDInterface):
        handler = self.web_socket_handler
        #pr ("delegating web socket handling to", handler)
        assert handler is not None, "No web socket handler for id " + repr(self.identifier)
        await handler.handle(info, request, interface)

    def local_url(
            self, 
            for_gizmo, 
            method,
            protocol="http", 
            server=None,
            port=None,
            prefix=None,
            identifier=None,
            filename=None,
            ):
        assert method in ("http", "ws"), "method should be http or ws: " + repr(method)
        server = server or for_gizmo._server
        port = port or for_gizmo._port
        prefix = prefix or self.prefix
        identifier = identifier or self.identifier
        path_components = [prefix, method, identifier]
        if filename is not None:
            path_components.append(filename)
        path = "/".join(path_components)
        url = "%s://%s:%s/%s" % (protocol, server, port, path)
        return url

class FileGetter:

    def __init__(self, fs_path, filename, mgr, content_type=None, interface=STDInterface):
        assert interface.file_exists(fs_path)
        #self.url_path = "%s/%s" % (mgr.url_path, filename)
        self.prefix = mgr.prefix
        self.identifier = mgr.identifier
        self.fs_path = fs_path
        self.filename = filename
        self.encoding = None
        if content_type is None:
            (content_type, encoding) = mimetypes.guess_type(fs_path)
            self.encoding = encoding # not used... xxx
        self.content_type = content_type

    def method_path(self, method=GET):
        # xxxx duplicated code with local_url above
        mprefix = None
        if method == GET:
            mprefix = "http"
        elif method == POST:
            mprefix = "http"
        elif method == WS:
             mprefix = "ws"
        else:
            raise ValueError("unknown method: " + repr(method))
        components = ["", self.prefix, mprefix, self.identifier, self.filename]
        return "/".join(components)

    def handle_get(self, info, request, interface=STDInterface):
        path = self.fs_path
        assert interface.file_exists(path)
        bytes = interface.get_file_bytes(path)
        return interface.respond(body=bytes, content_type=self.content_type)

    def handle_post(self, info, request, interface=STDInterface):
        return self.handle_get(info, request, interface=interface)

class GizmoPipelineSocketHandler:

    def __init__(self, gizmo, packet_limit=1000000, auto_flush=True):
        pipeline = H5Gizmos.GZPipeline(gizmo, packet_limit=packet_limit, auto_flush=auto_flush)
        self.pipeline = pipeline
        self.ws = None

    async def handle(self, info, request, interface):
        print("**** pipeline handler started")
        await self.pipeline.handle_websocket_request(request)