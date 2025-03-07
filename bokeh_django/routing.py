# -----------------------------------------------------------------------------
# Copyright (c) 2012 - 2022, Anaconda, Inc., and Bokeh Contributors.
# All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Boilerplate
# -----------------------------------------------------------------------------
from __future__ import annotations

import logging # isort:skip
log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------

# Standard library imports
import re
from pathlib import Path
from typing import Callable, List, Union
import weakref

# External imports
from django.core.asgi import get_asgi_application
from django.urls import re_path
from django.urls.resolvers import URLPattern
from channels.db import database_sync_to_async
from tornado import gen

# Bokeh imports
from bokeh.application import Application
from bokeh.application.handlers.document_lifecycle import DocumentLifecycleHandler
from bokeh.application.handlers.function import FunctionHandler
from bokeh.command.util import build_single_handler_application, build_single_handler_applications
from bokeh.server.contexts import ApplicationContext, BokehSessionContext, _RequestProxy, ServerSession
from bokeh.document import Document
from bokeh.util.token import get_token_payload

# Local imports
from .consumers import AutoloadJsConsumer, DocConsumer, WSConsumer

# -----------------------------------------------------------------------------
# Globals and constants
# -----------------------------------------------------------------------------

__all__ = (
    'RoutingConfiguration',
)

ApplicationLike = Union[Application, Callable, Path]

# -----------------------------------------------------------------------------
# General API
# -----------------------------------------------------------------------------

class DjangoApplicationContext(ApplicationContext):
    async def create_session_if_needed(self, session_id: ID, request: HTTPServerRequest | None = None,
            token: str | None = None) -> ServerSession:
        # this is because empty session_ids would be "falsey" and
        # potentially open up a way for clients to confuse us
        if len(session_id) == 0:
            raise ProtocolError("Session ID must not be empty")

        if session_id not in self._sessions and \
           session_id not in self._pending_sessions:
            future = self._pending_sessions[session_id] = gen.Future()

            doc = Document()

            session_context = BokehSessionContext(session_id,
                                                  self.server_context,
                                                  doc,
                                                  logout_url=self._logout_url)
            if request is not None:
                payload = get_token_payload(token) if token else {}
                if ('cookies' in payload and 'headers' in payload
                    and not 'Cookie' in payload['headers']):
                    # Restore Cookie header from cookies dictionary
                    payload['headers']['Cookie'] = '; '.join([
                        f'{k}={v}' for k, v in payload['cookies'].items()
                    ])
                # using private attr so users only have access to a read-only property
                session_context._request = _RequestProxy(request,
                                                         cookies=payload.get('cookies'),
                                                         headers=payload.get('headers'))
            session_context._token = token

            # expose the session context to the document
            # use the _attribute to set the public property .session_context
            doc._session_context = weakref.ref(session_context)

            try:
                await self._application.on_session_created(session_context)
            except Exception as e:
                log.error("Failed to run session creation hooks %r", e, exc_info=True)

            # This needs to be wrapped in the database_sync_to_async wrapper just in case the handler function accesses
            # Django ORM.

            await database_sync_to_async(self._application.initialize_document)(doc)

            session = ServerSession(session_id, doc, io_loop=self._loop, token=token)
            del self._pending_sessions[session_id]
            self._sessions[session_id] = session
            session_context._set_session(session)
            self._session_contexts[session_id] = session_context

            # notify anyone waiting on the pending session
            future.set_result(session)

        if session_id in self._pending_sessions:
            # another create_session_if_needed is working on
            # creating this session
            session = await self._pending_sessions[session_id]
        else:
            session = self._sessions[session_id]

        return session


class Routing:
    url: str
    app: Application
    app_context: ApplicationContext
    document: bool
    autoload: bool

    def __init__(self, url: str, app: ApplicationLike, *, document: bool = False, autoload: bool = False) -> None:
        self.url = url
        self.app = self._fixup(self._normalize(app))
        self.app_context = DjangoApplicationContext(self.app, url=self.url)
        self.document = document
        self.autoload = autoload

    def _normalize(self, obj: ApplicationLike) -> Application:
        if callable(obj):
            return Application(FunctionHandler(obj, trap_exceptions=True))
        elif isinstance(obj, Path):
            return build_single_handler_application(obj)
        else:
            return obj

    def _fixup(self, app: Application) -> Application:
        if not any(isinstance(handler, DocumentLifecycleHandler) for handler in app.handlers):
            app.add(DocumentLifecycleHandler())
        return app


def document(url: str, app: ApplicationLike) -> Routing:
    return Routing(url, app, document=True)


def autoload(url: str, app: ApplicationLike) -> Routing:
    return Routing(url, app, autoload=True)


def directory(*apps_paths: Path) -> List[Routing]:
    paths: List[Path] = []

    for apps_path in apps_paths:
        if apps_path.exists():
            paths += [ entry for entry in apps_path.glob("*") if is_bokeh_app(entry) ]
        else:
            log.warn(f"bokeh applications directory '{apps_path}' doesn't exist")

    return [ document(url, app) for url, app in build_single_handler_applications(paths).items() ]


class RoutingConfiguration:
    _http_urlpatterns: List[str] = []
    _websocket_urlpatterns: List[str] = []

    def __init__(self, routings: List[Routing]) -> None:
        for routing in routings:
            self._add_new_routing(routing)

    def get_http_urlpatterns(self) -> List[URLPattern]:
        return self._http_urlpatterns + [re_path(r"", get_asgi_application())]

    def get_websocket_urlpatterns(self) -> List[URLPattern]:
        return self._websocket_urlpatterns

    def _add_new_routing(self, routing: Routing) -> None:
        kwargs = dict(app_context=routing.app_context)

        def join(*components):
            return "/".join([ component.strip("/") for component in components if component ])

        def urlpattern(suffix=""):
            return r"^{}$".format(join(re.escape(routing.url)) + suffix)

        if routing.document:
            self._http_urlpatterns.append(re_path(urlpattern(), DocConsumer.as_asgi(), kwargs=kwargs))
        if routing.autoload:
            self._http_urlpatterns.append(re_path(urlpattern("/autoload.js"), AutoloadJsConsumer.as_asgi(), kwargs=kwargs))

        self._websocket_urlpatterns.append(re_path(urlpattern("/ws"), WSConsumer.as_asgi(), kwargs=kwargs))

# -----------------------------------------------------------------------------
# Dev API
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Private API
# -----------------------------------------------------------------------------


def is_bokeh_app(entry: Path) -> bool:
    return (entry.is_dir() or entry.name.endswith(('.py', '.ipynb'))) and not entry.name.startswith((".", "_"))

# -----------------------------------------------------------------------------
# Code
# -----------------------------------------------------------------------------
