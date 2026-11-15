"""MCP surface: POST /mcp (JSON-RPC 2.0 Streamable HTTP, plain JSON answers).

Python stdlib only. Deterministic: no clock, no randomness, no timestamps
in state. The tool list is a module-level constant, serialised with
sort_keys=True by json_response, so two tools/list answers are identical
byte-for-byte.

Shares the Store with REST/GraphQL: repos/issues/pulls created here are
the same objects REST reads, with the same ids.

Merge is implemented here (minimal squash) because forge/rest.py does not
exist yet: a squash commit is built from the head tree, parented on the
base tip, the base ref is moved to it, and the pull is marked merged /
closed. If a rest.py merge helper appears later, this can delegate to it.
"""
import json
import re

from forge.http_server import Response, json_error, json_response, route
from forge.store import git_object_id, commit_tree

SERVER_NAME = 'forge'
SERVER_VERSION = '1.0.0'
SUPPORTED_VERSIONS = ['2026-07-28', '2025-11-25']
LEGACY_VERSIONS = ['2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05']
MERGED_AT = '2025-01-01T00:00:00Z'

# --------------------------------------------------------------------------
# Tool catalogue (module-level constant; never mutated at runtime).
# required lists contain ONLY properties the suite sends.
# --------------------------------------------------------------------------
TOOLS = [
    {
        'name': 'create_repo',
        'description': 'Create a new repository owned by the viewer.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
                'description': {'type': 'string'},
                'private': {'type': 'boolean'},
            },
            'required': ['name'],
        },
    },
    {
        'name': 'get_repo',
        'description': 'Get a repository by owner and name.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_issue',
        'description': 'Create an issue in a repository.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'title': {'type': 'string'},
                'body': {'type': 'string'},
            },
            'required': ['owner', 'repo', 'title'],
        },
    },
    {
        'name': 'list_issues',
        'description': 'List issues in a repository, optionally filtered by state.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'state': {'type': 'string'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_pull_request',
        'description': 'Create a pull request from a head branch onto a base branch.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'title': {'type': 'string'},
                'head': {'type': 'string'},
                'base': {'type': 'string'},
                'body': {'type': 'string'},
            },
            'required': ['owner', 'repo', 'title', 'head', 'base'],
        },
    },
    {
        'name': 'merge_pull_request',
        'description': 'Merge a pull request with a squash merge.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string'},
                'repo': {'type': 'string'},
                'pull_number': {'type': 'integer'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
    },
]

# Canonical tool name -> aliases (normalised form, see _normalise).
ALIASES = {
    'createrepo': ['createrepository', 'repocreate', 'reposcreate',
                   'repositorycreate'],
    'getrepo': ['getrepository', 'repoget', 'reposget', 'repositoryget'],
    'createissue': ['issuecreate', 'issuescreate'],
    'listissues': ['issueslist', 'issuelist'],
    'createpullrequest': ['pullrequestcreate', 'pullscreate', 'createpr',
                          'prcreate'],
    'mergepullrequest': ['pullrequestmerge', 'pullsmerge', 'mergepr',
                         'prmerge'],
}

# Argument synonyms -> canonical property name.
ARG_SYNONYMS = {
    'owner': ['owner', 'org', 'organization', 'user', 'login', 'namespace'],
    'repo': ['repo', 'repository', 'project'],
    'name': ['name'],
    'title': ['title'],
    'body': ['body'],
    'description': ['description'],
    'private': ['private'],
    'state': ['state'],
    'head': ['head', 'head_ref', 'head_branch', 'source', 'from', 'branch'],
    'base': ['base', 'base_ref', 'base_branch', 'target', 'into'],
    'pull_number': ['pull_number', 'pr_number', 'number', 'index'],
}

# Canonical tool -> required canonical arguments, in reporting order.
TOOL_REQUIRED = {
    'create_repo': ['name'],
    'get_repo': ['owner', 'repo'],
    'create_issue': ['owner', 'repo', 'title'],
    'list_issues': ['owner', 'repo'],
    'create_pull_request': ['owner', 'repo', 'title', 'head', 'base'],
    'merge_pull_request': ['owner', 'repo', 'pull_number'],
}


def _normalise(name):
    """Lowercase, drop every non-alphanumeric character."""
    return re.sub(r'[^a-z0-9]', '', str(name).lower())


def _canonical_tool(name):
    """Map a requested tool name to its canonical name, or None.

    Matches when the normalised name equals the canonical normalised
    name, ends with it (prefix tolerated, e.g. github__create_issue),
    or is a known alias.
    """
    norm = _normalise(name)
    for canonical in TOOL_REQUIRED:
        canon_norm = _normalise(canonical)
        if norm == canon_norm or norm.endswith(canon_norm):
            return canonical
    for canonical, aliases in ALIASES.items():
        if norm in aliases:
            for canon in TOOL_REQUIRED:
                if _normalise(canon) == canonical:
                    return canon
    return None


def _normalise_args(arguments):
    """Map argument synonyms to canonical property names.

    The first synonym present wins; a canonical name already present is
    never overwritten. A bare full_name ("owner/repo") fills owner/repo
    when they are missing.
    """
    args = {}
    if not isinstance(arguments, dict):
        return args
    lowered = {}
    for key, value in arguments.items():
        if isinstance(key, str):
            lowered[key.lower()] = value
    for canonical, synonyms in ARG_SYNONYMS.items():
        for syn in synonyms:
            if syn in lowered and lowered[syn] is not None:
                args[canonical] = lowered[syn]
                break
    if ('owner' not in args or 'repo' not in args) and isinstance(
            lowered.get('full_name'), str):
        parts = lowered['full_name'].split('/', 1)
        if len(parts) == 2:
            args.setdefault('owner', parts[0])
            args.setdefault('repo', parts[1])
    return args


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes')
    return bool(value) if value is not None else default


def _coerce_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# JSON-RPC helpers (stateless per request; the id is echoed verbatim).
# --------------------------------------------------------------------------
def _rpc_result(rpc_id, result):
    return json_response(200, {'jsonrpc': '2.0', 'id': rpc_id,
                               'result': result})


def _rpc_error(rpc_id, code, message):
    return json_response(200, {'jsonrpc': '2.0', 'id': rpc_id,
                               'error': {'code': code, 'message': message}})


def _tool_text(payload):
    """One text content block carrying JSON, plus the same structured data."""
    text = json.dumps(payload, sort_keys=True)
    return {'content': [{'type': 'text', 'text': text}],
            'structuredContent': payload}


def _tool_error(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


def _extract_id(raw_body):
    """Best-effort id out of a raw (unparseable) body, else None."""
    try:
        text = raw_body.decode('utf-8', 'replace')
    except Exception:
        return None
    match = re.search(r'"id"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+|null)', text)
    if not match:
        return None
    token = match.group(1)
    if token == 'null':
        return None
    try:
        return json.loads(token)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Tool implementations (all share the Store with REST/GraphQL).
# --------------------------------------------------------------------------
def _tool_create_repo(store, args):
    name = args.get('name')
    repo = store.create_repo(name, args.get('description') or '',
                             _coerce_bool(args.get('private'), False))
    if repo is None:
        return _tool_error('repository already exists: %s/%s'
                           % (store.viewer['login'], name))
    payload = {'id': repo.database_id, 'name': repo.name,
               'full_name': repo.full_name(), 'owner': repo.owner,
               'private': repo.private}
    return _tool_text(payload)


def _tool_get_repo(store, args):
    repo = store.get_repo(args.get('owner'), args.get('repo'))
    if repo is None:
        return _tool_error('repository not found: %s/%s'
                           % (args.get('owner'), args.get('repo')))
    payload = {'id': repo.database_id, 'name': repo.name,
               'full_name': repo.full_name(), 'owner': repo.owner,
               'private': repo.private,
               'default_branch': repo.default_branch}
    return _tool_text(payload)


def _tool_create_issue(store, args):
    repo = store.get_repo(args.get('owner'), args.get('repo'))
    if repo is None:
        return _tool_error('repository not found: %s/%s'
                           % (args.get('owner'), args.get('repo')))
    issue = store.create_issue(repo, args.get('title'),
                               args.get('body') or '', [])
    payload = {'id': store.issue_global_id(repo, issue['number']),
               'number': issue['number'], 'title': issue['title'],
               'state': 'open'}
    return _tool_text(payload)


def _tool_list_issues(store, args):
    repo = store.get_repo(args.get('owner'), args.get('repo'))
    if repo is None:
        return _tool_error('repository not found: %s/%s'
                           % (args.get('owner'), args.get('repo')))
    state = args.get('state')
    wanted = state.lower() if isinstance(state, str) else None
    issues = []
    for number in sorted(repo.issues):
        issue = repo.issues[number]
        if wanted == 'open' and issue['state'] != 'open':
            continue
        if wanted == 'closed' and issue['state'] != 'closed':
            continue
        issues.append({'number': issue['number'], 'title': issue['title'],
                       'state': issue['state']})
    return {'content': [{'type': 'text',
                         'text': json.dumps(issues, sort_keys=True)}],
            'structuredContent': {'issues': issues}}


def _tool_create_pull_request(store, args):
    repo = store.get_repo(args.get('owner'), args.get('repo'))
    if repo is None:
        return _tool_error('repository not found: %s/%s'
                           % (args.get('owner'), args.get('repo')))
    head_ref = args.get('head')
    base_ref = args.get('base')
    head_sha = repo.refs.get('refs/heads/' + str(head_ref))
    if head_sha is None:
        return _tool_error('head branch not found: %s' % head_ref)
    base_sha = repo.refs.get('refs/heads/' + str(base_ref))
    if base_sha is None:
        return _tool_error('base branch not found: %s' % base_ref)
    pull = store.create_pull(repo, args.get('title'), args.get('body') or '',
                             head_ref, base_ref, head_sha, base_sha)
    payload = {'number': pull['number'], 'title': pull['title'],
               'state': 'open',
               'head': {'ref': pull['head']['ref'],
                        'sha': pull['head']['sha']},
               'base': {'ref': pull['base']['ref'],
                        'sha': pull['base']['sha']}}
    return _tool_text(payload)


def _squash_merge(store, repo, pull):
    """Minimal deterministic squash merge (no rest.py helper exists yet).

    New commit: tree = head tree, parent = base tip, fixed author /
    committer identity and epoch 0 timestamp, so the sha is stable for
    identical inputs. Base ref moves to the merge commit; the pull is
    marked merged/closed. Returns the merge commit sha.
    """
    head_sha = pull['head']['sha']
    base_ref = pull['base']['ref']
    base_tip = repo.refs.get('refs/heads/' + base_ref)
    tree = None
    entry = repo.objects.get(head_sha)
    if entry is not None:
        if entry[0] == 'commit':
            tree = commit_tree(entry[1])
        elif entry[0] == 'tree':
            tree = head_sha
    if tree is None:
        tree = git_object_id('tree', b'')
    lines = ['tree %s' % tree]
    if base_tip is not None:
        lines.append('parent %s' % base_tip)
    lines.append('author Forge <forge@example.invalid> 0 +0000')
    lines.append('committer Forge <forge@example.invalid> 0 +0000')
    message = 'Merge pull request #%d: %s\n' % (pull['number'],
                                               pull['title'])
    commit_bytes = ('\n'.join(lines) + '\n\n' + message).encode('utf-8')
    merge_sha = git_object_id('commit', commit_bytes)
    repo.objects[merge_sha] = ('commit', commit_bytes)
    repo.refs['refs/heads/' + base_ref] = merge_sha
    pull['merged'] = True
    pull['state'] = 'closed'
    pull['merged_at'] = MERGED_AT
    pull['merge_commit_sha'] = merge_sha
    pull['base']['sha'] = merge_sha
    return merge_sha


def _tool_merge_pull_request(store, args):
    repo = store.get_repo(args.get('owner'), args.get('repo'))
    if repo is None:
        return _tool_error('repository not found: %s/%s'
                           % (args.get('owner'), args.get('repo')))
    number = _coerce_int(args.get('pull_number'))
    pull = repo.pulls.get(number) if number is not None else None
    if pull is None:
        return _tool_error('pull request not found: %s' % args.get('pull_number'))
    if pull.get('merged') or pull.get('state') != 'open':
        return _tool_error('pull request already merged: #%d' % number)
    merge_sha = _squash_merge(store, repo, pull)
    payload = {'merged': True, 'sha': merge_sha, 'number': pull['number']}
    return _tool_text(payload)


_TOOL_HANDLERS = {
    'create_repo': _tool_create_repo,
    'get_repo': _tool_get_repo,
    'create_issue': _tool_create_issue,
    'list_issues': _tool_list_issues,
    'create_pull_request': _tool_create_pull_request,
    'merge_pull_request': _tool_merge_pull_request,
}


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------
def _handle_mcp(store, req):
    # Auth first, before the method/tool name is examined, for every
    # MCP method (including notifications and malformed bodies).
    # NOTE: store.check_auth returns a single bool despite its docstring
    # saying "(ok, status)".
    if not store.check_auth(req.headers):
        return json_error(401, 'Unauthorized')

    # Body must be JSON. A truncated / non-JSON body is HTTP 400 with
    # JSON-RPC -32700; the id is salvaged from the raw bytes when possible.
    if req.json_error or req.json is None:
        return json_response(
            400, {'jsonrpc': '2.0', 'id': _extract_id(req.body),
                  'error': {'code': -32700, 'message': 'Parse error'}})
    payload = req.json
    if not isinstance(payload, dict):
        return json_response(
            400, {'jsonrpc': '2.0', 'id': None,
                  'error': {'code': -32700, 'message': 'Parse error'}})

    # Notifications carry no id and need no answer: HTTP 202, empty body.
    if 'id' not in payload:
        return Response(202, b'', 'application/json')
    rpc_id = payload.get('id')
    method = payload.get('method')
    params = payload.get('params')
    if not isinstance(params, dict):
        params = {}

    if method == 'server/discover':
        # Modern era (2026-07-28): answer normally, whatever revision was
        # declared. serverInfo in both places: plain and under _meta.
        server_info = {'name': SERVER_NAME, 'version': SERVER_VERSION}
        return _rpc_result(rpc_id, {
            'supportedVersions': list(SUPPORTED_VERSIONS),
            'capabilities': {'tools': {}},
            'serverInfo': server_info,
            '_meta': {'io.modelcontextprotocol/serverInfo': server_info},
        })

    if method == 'initialize':
        requested = params.get('protocolVersion')
        version = (requested if requested in LEGACY_VERSIONS
                   else '2025-11-25')
        return _rpc_result(rpc_id, {
            'protocolVersion': version,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
        })

    if method == 'tools/list':
        # One page, no nextCursor; TOOLS is a module constant.
        return _rpc_result(rpc_id, {'tools': TOOLS})

    if method == 'tools/call':
        name = params.get('name')
        if not isinstance(name, str) or not name:
            return _rpc_error(rpc_id, -32602,
                              'Invalid params: missing tool name')
        canonical = _canonical_tool(name)
        if canonical is None:
            return _rpc_error(rpc_id, -32601,
                              'Unknown tool: %s' % name)
        args = _normalise_args(params.get('arguments'))
        for required in TOOL_REQUIRED[canonical]:
            value = args.get(required)
            if value is None or (isinstance(value, str) and value == ''):
                return _rpc_result(
                    rpc_id, _tool_error('missing required argument: %s'
                                        % required))
        if canonical == 'merge_pull_request':
            args['pull_number'] = _coerce_int(args.get('pull_number'))
            if args['pull_number'] is None:
                return _rpc_result(
                    rpc_id, _tool_error('missing required argument: '
                                        'pull_number'))
        result = _TOOL_HANDLERS[canonical](store, args)
        return _rpc_result(rpc_id, result)

    return _rpc_error(rpc_id, -32601, 'Method not found: %s' % method)


def routes():
    method, regex = route('POST', '/mcp')
    return [(method, regex, _handle_mcp)]
