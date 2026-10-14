from fastapi.routing import APIRoute
from starlette.routing import Mount

from app.main import create_app

# Everything else must require a login.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
    ("GET", "/manifest.webmanifest"),
    ("GET", "/sw.js"),
    ("GET", "/favicon.ico"),
}


def iter_routes(routes):
    for route in routes:
        if isinstance(route, Mount):
            continue  # /static
        if isinstance(route, APIRoute):
            for method in route.methods:
                yield method, route.path
        elif hasattr(route, "original_router"):
            yield from iter_routes(route.original_router.routes)


def test_every_non_public_route_requires_login(settings, client):
    routes = set(iter_routes(create_app(settings).routes))
    assert ("GET", "/") in routes, "route discovery found nothing - FastAPI internals changed?"
    for method, path in sorted(routes - PUBLIC):
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303, f"{method} {path} is reachable without login"
        assert response.headers["location"].startswith("/login")
