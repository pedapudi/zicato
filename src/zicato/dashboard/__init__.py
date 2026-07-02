"""The dashboard driver: the HTTP server and its front-end bundle.

One of zicato's three drivers (cli / dashboard / builder). This package
owns the Starlette server (:mod:`~zicato.dashboard.server`), the route
handlers (:mod:`~zicato.dashboard.endpoints`), SSE, the transcript
reconstructor, and the static asset bundle + its resolution
(:mod:`~zicato.dashboard.static_assets`). Workspace reads live in the
library query layer (:mod:`zicato.query`) — this package consumes the
library surface and never the other way around, except the declared
optional mount of the builder's API routes in ``server.py``.
"""
