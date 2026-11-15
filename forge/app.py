"""Wire the four surfaces into one HTTP server.

Routes are assembled from the surface modules. A module that is not
implemented yet simply contributes no routes (its endpoints answer the
global JSON 404), so the skeleton boots early and surfaces land one by one.
"""
from forge.http_server import create_server, route


def _load_routes():
    routes = []
    # Core: health + API root are implemented here so boot always works.
    from forge import core
    routes.extend(core.routes())
    for mod_name in ('rest', 'gitproto', 'graphql', 'mcp'):
        try:
            mod = __import__('forge.' + mod_name, fromlist=['routes'])
        except Exception:
            continue
        try:
            routes.extend(mod.routes())
        except Exception:
            continue
    return routes


def create_app_server(store, host='127.0.0.1', port=0):
    return create_server(store, _load_routes(), host, port)
