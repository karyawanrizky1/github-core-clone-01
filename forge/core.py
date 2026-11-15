"""Core routes: /healthz and the API root. Always present so boot works
even before the other surfaces land."""
from forge.http_server import json_response, route


def _healthz(store, req):
    return json_response(200, {'ok': True})


def _api_root(store, req):
    return json_response(200, {
        'current_user_url': '/api/v3/user',
        'rate_limit_url': '/api/v3/rate_limit',
    }, {'x-oauth-scopes': 'repo, read:org'})


def routes():
    return [
        route('GET', '/healthz'),
        route('GET', '/api/v3'),
        route('GET', '/api/v3/'),
    ]


# Attach handlers by position (route() returns (method, regex); handlers
# are bound here to keep http_server generic).
_ROUTES = routes()
_ROUTES[0] = (_ROUTES[0][0], _ROUTES[0][1], _healthz)
_ROUTES[1] = (_ROUTES[1][0], _ROUTES[1][1], _api_root)
_ROUTES[2] = (_ROUTES[2][0], _ROUTES[2][1], _api_root)


def routes():
    return list(_ROUTES)
