"""GraphQL v4 surface for the forge (Python stdlib only).

POST /api/graphql — the 18 documents the conformance suite sends
(kit/program/env/src/checks-graphql.ts). This is deliberately NOT a
general GraphQL parser: each document is recognised by normalised-string
patterns and answered from the store, per work/spec-graphql-mcp.md.

Deterministic: no clock, no randomness. Pagination cursors are
'cursor:<issue-number>'.

Cross-surface notes (the suite reads mutations back through REST):
- createIssue/closeIssue/addComment write the same store dicts REST
  reads, so a comment created here keeps its integer id from
  store.next_comment_id() and REST GET /issues/{n}/comments sees it.
- createPullRequest fills pull['files'] from a tree diff (head vs base),
  in the REST pullFileShape ({filename, status, additions, deletions,
  changes}); a pull created by REST keeps whatever files REST stored,
  and the files reader below accepts both {'path': ...} and
  {'filename': ...} entries, falling back to the tree diff.
- mergePullRequest is implemented here because forge/rest.py does not
  exist yet: a squash commit (tree = head tree, parent = base tip) is
  created with git_object_id(), the base ref is advanced to it, and the
  pull is marked merged / closed. REST can reuse merge_pull_request()
  from this module instead of duplicating the logic. merged_at is the
  fixed string '1970-01-01T00:00:00Z' (deterministic; the REST checks
  only require it to be a non-empty string).
"""
import difflib
import re

from forge.http_server import json_error, json_response, route
from forge.store import commit_tree, git_object_id

_MERGED_AT = '1970-01-01T00:00:00Z'

# ---------------------------------------------------------------------------
# Introspection tables (gh feature-detection probes).
# ---------------------------------------------------------------------------

_TYPE_FIELDS = {
    'Issue': ['id', 'number', 'title', 'state', 'body', 'createdAt',
              'updatedAt', 'url', 'author', 'labels', 'comments', 'closedAt'],
    'PullRequest': ['id', 'number', 'title', 'state', 'merged',
                    'headRefName', 'baseRefName', 'mergeable', 'files',
                    'body', 'createdAt', 'updatedAt', 'url', 'author'],
    'Repository': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner',
                   'defaultBranchRef', 'viewerPermission', 'hasIssuesEnabled',
                   'issue', 'issues', 'pullRequests', 'pullRequest',
                   'description', 'private', 'url'],
}

_SEARCH_TYPE_ENUM = ['ISSUE', 'USER', 'REPOSITORY']

# Every identifier token that may appear in the 18 suite documents
# (after string literals are stripped). Anything else is an unknown field.
_KNOWN_TOKENS = set('''
query mutation true false null on
ViewerLogin RepositoryFields Issue_fields PullRequestAndRepositoryFields
SearchType_enumValues OpenIssues IssueByNumber IssueCreate IssueClose
PullRequests PullRequestFiles PullRequestId PullRequestCreate
MergePullRequest AddComment ViewerInvalidField NodeRepository IssuesPage
viewer login repository owner name id databaseId nameWithOwner
defaultBranchRef viewerPermission hasIssuesEnabled fields includeDeprecated
enumValues issues nodes number title state body issue createIssue closeIssue
pullRequests headRefName baseRefName mergeable pullRequest files path
createPullRequest mergePullRequest merged addComment commentEdge node
first states orderBy field direction after input __type pageInfo hasNextPage
endCursor
String Int ID Boolean CreateIssueInput CloseIssueInput CreatePullRequestInput
MergePullRequestInput AddCommentInput
OPEN CLOSED CREATED_AT UPDATED_AT COMMENTS ASC DESC MERGE SQUASH REBASE
MERGEABLE CONFLICTING UNKNOWN ADMIN MAINTAIN WRITE TRIAGE READ
ISSUE USER REPOSITORY PULL_REQUEST
Issue PullRequest Repository SearchType
repositoryId issueId pullRequestId subjectId baseRefName headRefName
mergeMethod
'''.split())


def _norm(query):
    return re.sub(r'\s+', ' ', query).strip()


def _strip_strings(query):
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', query)


def _unknown_field(query):
    """First identifier token not in the known vocabulary, or None.
    The operation name after `query`/`mutation` is not a field."""
    stripped = re.sub(r'\b(?:query|mutation)\s+[A-Za-z_][A-Za-z0-9_]*',
                      'query', _strip_strings(query))
    for tok in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', stripped):
        if tok not in _KNOWN_TOKENS:
            return tok
    return None


def _err(message):
    return {'message': message}


# ---------------------------------------------------------------------------
# Argument resolution: $variable, "literal", 123, null.
# ---------------------------------------------------------------------------

def _resolve_arg(query, variables, name):
    m = re.search(r'\b' + re.escape(name) + r'\s*:\s*(?:\$(\w+)|"([^"]*)"|(-?\d+)|null)',
                  query)
    if not m:
        return None, False
    if m.group(1) is not None:
        return variables.get(m.group(1)), True
    if m.group(2) is not None:
        return m.group(2), True
    if m.group(3) is not None:
        return int(m.group(3)), True
    return None, True  # explicit null


def _int_arg(query, variables, name, default=None):
    value, found = _resolve_arg(query, variables, name)
    if not found or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_arg(query, variables, name, default=None):
    value, found = _resolve_arg(query, variables, name)
    if not found or value is None:
        return default
    return str(value)


# ---------------------------------------------------------------------------
# Store -> GraphQL shapes.
# ---------------------------------------------------------------------------

def _repo_obj(store, repo):
    return {
        'id': store.repo_global_id(repo),
        'databaseId': repo.database_id,
        'name': repo.name,
        'nameWithOwner': repo.full_name(),
        'owner': {'login': repo.owner},
        'defaultBranchRef': {'name': repo.default_branch},
        'viewerPermission': 'ADMIN',
        'hasIssuesEnabled': True,
    }


def _issue_state(issue):
    return 'CLOSED' if issue['state'] == 'closed' else 'OPEN'


def _issue_obj(store, repo, issue):
    return {
        'id': store.issue_global_id(repo, issue['number']),
        'number': issue['number'],
        'title': issue['title'],
        'state': _issue_state(issue),
        'body': issue['body'],
    }


def _pull_state(pull):
    if pull.get('merged'):
        return 'CLOSED'
    return 'CLOSED' if pull['state'] == 'closed' else 'OPEN'


def _mergeable(repo, pull):
    if pull.get('merged') or pull['state'] != 'open':
        return 'UNKNOWN'
    head_sha = (pull.get('head') or {}).get('sha')
    base_sha = (pull.get('base') or {}).get('sha')
    if not head_sha or not base_sha:
        return 'UNKNOWN'
    if head_sha == base_sha:
        return 'MERGEABLE'
    if repo.is_ancestor(base_sha, head_sha):
        return 'MERGEABLE'
    if repo.is_ancestor(head_sha, base_sha):
        return 'MERGEABLE'
    return 'UNKNOWN'


# ---------------------------------------------------------------------------
# Git tree diff (PR files). Trees are raw git objects in repo.objects.
# ---------------------------------------------------------------------------

def _flatten_tree(repo, tree_oid, prefix=''):
    entry = repo.objects.get(tree_oid)
    if entry is None or entry[0] != 'tree':
        return {}
    data = entry[1]
    out = {}
    i = 0
    while i < len(data):
        j = data.index(b'\x00', i)
        header = data[i:j].decode('utf-8', 'replace')
        mode, name = header.split(' ', 1)
        oid = data[j + 1:j + 21].hex()
        i = j + 21
        path = prefix + name
        if mode == '40000':
            out.update(_flatten_tree(repo, oid, path + '/'))
        else:
            out[path] = oid
    return out


def _tree_of_commit(repo, commit_oid):
    if not commit_oid:
        return {}
    entry = repo.objects.get(commit_oid)
    if entry is None or entry[0] != 'commit':
        return {}
    tree_oid = commit_tree(entry[1])
    return _flatten_tree(repo, tree_oid) if tree_oid else {}


def _blob_lines(repo, oid):
    entry = repo.objects.get(oid)
    if entry is None or entry[0] != 'blob':
        return []
    return entry[1].decode('utf-8', 'replace').splitlines()


def _diff_files(repo, base_sha, head_sha):
    """Changed paths head-vs-base as REST pullFileShape dicts."""
    base = _tree_of_commit(repo, base_sha)
    head = _tree_of_commit(repo, head_sha)
    out = []
    for path in sorted(set(base) | set(head)):
        if base.get(path) == head.get(path):
            continue
        if path not in base:
            status = 'added'
            additions = len(_blob_lines(repo, head[path]))
            deletions = 0
        elif path not in head:
            status = 'removed'
            additions = 0
            deletions = len(_blob_lines(repo, base[path]))
        else:
            status = 'modified'
            old = _blob_lines(repo, base[path])
            new = _blob_lines(repo, head[path])
            additions = 0
            deletions = 0
            for line in difflib.unified_diff(old, new, lineterm=''):
                if line.startswith('+++') or line.startswith('---'):
                    continue
                if line.startswith('+'):
                    additions += 1
                elif line.startswith('-'):
                    deletions += 1
        out.append({'filename': path, 'status': status,
                    'additions': additions, 'deletions': deletions,
                    'changes': additions + deletions})
    return out


def _pull_file_paths(store, repo, pull, first):
    entries = pull.get('files') or []
    paths = []
    for entry in entries:
        if isinstance(entry, str):
            paths.append(entry)
        elif isinstance(entry, dict):
            path = entry.get('path') or entry.get('filename')
            if path:
                paths.append(path)
    if not paths:
        head_sha = (pull.get('head') or {}).get('sha')
        base_sha = (pull.get('base') or {}).get('sha')
        paths = [f['filename'] for f in _diff_files(repo, base_sha, head_sha)]
    if first is not None:
        paths = paths[:first]
    return paths


def _pull_obj(store, repo, pull, query=None, variables=None):
    obj = {
        'id': store.pull_global_id(repo, pull['number']),
        'number': pull['number'],
        'title': pull['title'],
        'state': _pull_state(pull),
        'merged': bool(pull.get('merged')),
        'headRefName': (pull.get('head') or {}).get('ref'),
        'baseRefName': (pull.get('base') or {}).get('ref'),
        'mergeable': _mergeable(repo, pull),
        'body': pull['body'],
    }
    if query is not None and 'files(' in query:
        first = _int_arg(query, variables or {}, 'first', 10)
        obj['files'] = {'nodes': [{'path': p} for p in
                                   _pull_file_paths(store, repo, pull, first)]}
    return obj


def compute_pull_files(repo, pull):
    """Public helper for REST: (re)compute pull['files'] from the trees."""
    head_sha = (pull.get('head') or {}).get('sha')
    base_sha = (pull.get('base') or {}).get('sha')
    pull['files'] = _diff_files(repo, base_sha, head_sha)
    return pull['files']


def merge_pull_request(repo, pull):
    """Squash-merge a pull: new commit (tree = head tree, parent = base
    tip), base ref advanced to it, pull merged + closed. Returns the
    merge commit oid (or the head sha when no commit object exists)."""
    base_ref = (pull.get('base') or {}).get('ref') or repo.default_branch
    head_sha = (pull.get('head') or {}).get('sha')
    base_tip = repo.refs.get('refs/heads/' + base_ref) or \
        (pull.get('base') or {}).get('sha')
    new_oid = None
    head_entry = repo.objects.get(head_sha) if head_sha else None
    if head_entry is not None and head_entry[0] == 'commit':
        tree_oid = commit_tree(head_entry[1])
        if tree_oid:
            parents = 'parent %s\n' % base_tip if base_tip else ''
            message = 'Merge pull request #%d: %s\n' % (
                pull['number'], pull['title'])
            data = ('tree %s\n%sauthor Forge <forge@example.invalid> 0 +0000\n'
                    'committer Forge <forge@example.invalid> 0 +0000\n\n%s'
                    % (tree_oid, parents, message)).encode('utf-8')
            new_oid = git_object_id('commit', data)
            repo.objects[new_oid] = ('commit', data)
    if new_oid is None:
        new_oid = head_sha
    if new_oid:
        repo.refs['refs/heads/' + base_ref] = new_oid
    pull['merged'] = True
    pull['merged_at'] = _MERGED_AT
    pull['merge_commit_sha'] = new_oid
    pull['state'] = 'closed'
    return new_oid


# ---------------------------------------------------------------------------
# Issues connection (filter / order / cursor pagination).
# ---------------------------------------------------------------------------

def _issues_connection(store, repo, query, variables):
    first = _int_arg(query, variables, 'first', None)
    after = _str_arg(query, variables, 'after', None)
    states = None
    m = re.search(r'states\s*:\s*\[([^\]]*)\]', query)
    if m:
        states = [t.strip().upper() for t in m.group(1).split(',') if t.strip()]
    direction = 'DESC'
    m = re.search(r'direction\s*:\s*(ASC|DESC)', query)
    if m:
        direction = m.group(1)
    issues = list(repo.issues.values())
    if states:
        wanted = set()
        if 'OPEN' in states:
            wanted.add('open')
        if 'CLOSED' in states:
            wanted.add('closed')
        issues = [i for i in issues if i['state'] in wanted]
    numbers = sorted((i['number'] for i in issues),
                     reverse=(direction == 'DESC'))
    if after:
        anchor = None
        if after.startswith('cursor:'):
            try:
                anchor = int(after[len('cursor:'):])
            except ValueError:
                anchor = None
        elif after.isdigit():
            anchor = int(after)
        if anchor is not None:
            if direction == 'DESC':
                numbers = [n for n in numbers if n < anchor]
            else:
                numbers = [n for n in numbers if n > anchor]
    total = len(numbers)
    page = numbers if first is None else numbers[:first]
    nodes = [_issue_obj(store, repo, repo.issues[n]) for n in page]
    has_next = total > len(page)
    end_cursor = ('cursor:%d' % page[-1]) if page else None
    return {'nodes': nodes,
            'pageInfo': {'hasNextPage': has_next, 'endCursor': end_cursor}}


# ---------------------------------------------------------------------------
# Dispatch: one operation document -> (data, errors).
# ---------------------------------------------------------------------------

def _find_repo(store, query, variables):
    owner = _str_arg(query, variables, 'owner')
    name = _str_arg(query, variables, 'name')
    if owner is None or name is None:
        return None
    return store.get_repo(owner, name)


def _introspection(query):
    data = {}
    pattern = re.compile(
        r'(?:(\w+)\s*:\s*)?__type\s*\(\s*name\s*:\s*"([^"]+)"\s*\)')
    matches = list(pattern.finditer(query))
    for idx, match in enumerate(matches):
        alias, type_name = match.group(1), match.group(2)
        key = alias or '__type'
        segment = query[match.end():matches[idx + 1].start()
                        if idx + 1 < len(matches) else len(query)]
        if 'enumValues' in segment:
            values = _SEARCH_TYPE_ENUM if type_name == 'SearchType' else []
            data[key] = {'name': type_name,
                         'enumValues': [{'name': v} for v in values]}
        else:
            fields = _TYPE_FIELDS.get(type_name, [])
            data[key] = {'name': type_name,
                         'fields': [{'name': f} for f in fields]}
    return data, []


def _dispatch(store, query, variables):
    qn = _norm(query)
    bad = _unknown_field(qn)
    if bad is not None:
        return {}, [_err('Unknown field "%s" on type Query' % bad)]

    if '__type(' in qn:
        return _introspection(qn)

    if 'createIssue(' in qn:
        inp = variables.get('input') or {}
        if not isinstance(inp, dict):
            return {'createIssue': None}, [_err('createIssue requires an input object')]
        kind = store.global_id_kind(inp.get('repositoryId') or '')
        if not kind or kind[0] != 'repo':
            return {'createIssue': None}, [
                _err('Unknown repositoryId "%s"' % inp.get('repositoryId'))]
        repo = kind[1]
        issue = store.create_issue(repo, inp.get('title') or '',
                                   inp.get('body') or '', [])
        return {'createIssue': {'issue': {
            'number': issue['number'],
            'id': store.issue_global_id(repo, issue['number'])}}}, []

    if 'closeIssue(' in qn:
        inp = variables.get('input') or {}
        if not isinstance(inp, dict):
            return {'closeIssue': None}, [_err('closeIssue requires an input object')]
        kind = store.global_id_kind(inp.get('issueId') or '')
        if not kind or kind[0] != 'issue':
            return {'closeIssue': None}, [
                _err('Unknown issueId "%s"' % inp.get('issueId'))]
        _k, repo, number = kind
        issue = repo.issues[number]
        issue['state'] = 'closed'
        return {'closeIssue': {'issue': {
            'number': number,
            'id': store.issue_global_id(repo, number),
            'state': 'CLOSED'}}}, []

    if 'createPullRequest(' in qn:
        inp = variables.get('input') or {}
        if not isinstance(inp, dict):
            return {'createPullRequest': None}, [
                _err('createPullRequest requires an input object')]
        kind = store.global_id_kind(inp.get('repositoryId') or '')
        if not kind or kind[0] != 'repo':
            return {'createPullRequest': None}, [
                _err('Unknown repositoryId "%s"' % inp.get('repositoryId'))]
        repo = kind[1]
        head_ref = inp.get('headRefName') or ''
        base_ref = inp.get('baseRefName') or repo.default_branch
        head_sha = repo.refs.get('refs/heads/' + head_ref)
        if not head_sha:
            return {'createPullRequest': None}, [
                _err('Unknown headRefName "%s"' % head_ref)]
        base_sha = repo.refs.get('refs/heads/' + base_ref)
        if not base_sha:
            return {'createPullRequest': None}, [
                _err('Unknown baseRefName "%s"' % base_ref)]
        pull = store.create_pull(repo, inp.get('title') or '',
                                 inp.get('body') or '', head_ref, base_ref,
                                 head_sha, base_sha)
        compute_pull_files(repo, pull)
        return {'createPullRequest': {'pullRequest': {
            'number': pull['number'],
            'id': store.pull_global_id(repo, pull['number']),
            'state': 'OPEN',
            'headRefName': head_ref,
            'baseRefName': base_ref}}}, []

    if 'mergePullRequest(' in qn:
        inp = variables.get('input') or {}
        if not isinstance(inp, dict):
            return {'mergePullRequest': None}, [
                _err('mergePullRequest requires an input object')]
        kind = store.global_id_kind(inp.get('pullRequestId') or '')
        if not kind or kind[0] != 'pull':
            return {'mergePullRequest': None}, [
                _err('Unknown pullRequestId "%s"' % inp.get('pullRequestId'))]
        _k, repo, number = kind
        pull = repo.pulls[number]
        if not pull.get('merged'):
            merge_pull_request(repo, pull)
        return {'mergePullRequest': {'pullRequest': {
            'number': number,
            'id': store.pull_global_id(repo, number),
            'merged': True,
            'state': 'CLOSED'}}}, []

    if 'addComment(' in qn:
        inp = variables.get('input') or {}
        if not isinstance(inp, dict):
            return {'addComment': None}, [
                _err('addComment requires an input object')]
        kind = store.global_id_kind(inp.get('subjectId') or '')
        if not kind or kind[0] not in ('issue', 'pull'):
            return {'addComment': None}, [
                _err('Unknown subjectId "%s"' % inp.get('subjectId'))]
        body = inp.get('body') or ''
        comment_id = store.next_comment_id()
        if kind[0] == 'issue':
            _k, repo, number = kind
            repo.issues[number]['comments'].append(
                {'id': comment_id, 'body': body})
        else:
            _k, repo, number = kind
            repo.pulls[number].setdefault('comments', []).append(
                {'id': comment_id, 'body': body})
        return {'addComment': {'commentEdge': {'node': {
            'id': str(comment_id), 'body': body}}}}, []

    if re.search(r'\bnode\s*\(', qn):
        node_id = _str_arg(qn, variables, 'id')
        kind = store.global_id_kind(node_id or '')
        if not kind or kind[0] is None:
            return {'node': None}, []
        if kind[0] == 'repo':
            return {'node': _repo_obj(store, kind[1])}, []
        if kind[0] == 'issue':
            _k, repo, number = kind
            return {'node': _issue_obj(store, repo, repo.issues[number])}, []
        _k, repo, number = kind
        return {'node': _pull_obj(store, repo, repo.pulls[number])}, []

    if 'repository(' in qn:
        repo = _find_repo(store, qn, variables)
        if repo is None:
            return {'repository': None}, []
        obj = _repo_obj(store, repo)
        if 'issues(' in qn:
            obj['issues'] = _issues_connection(store, repo, qn, variables)
        elif re.search(r'\bissue\s*\(', qn):
            number = _int_arg(qn, variables, 'number')
            issue = repo.issues.get(number) if number is not None else None
            obj['issue'] = (_issue_obj(store, repo, issue)
                            if issue is not None else None)
        elif 'pullRequests(' in qn:
            first = _int_arg(qn, variables, 'first', None)
            pulls = sorted(repo.pulls.values(), key=lambda p: p['number'])
            if first is not None:
                pulls = pulls[:first]
            obj['pullRequests'] = {'nodes': [
                _pull_obj(store, repo, p) for p in pulls]}
        elif re.search(r'\bpullRequest\s*\(', qn):
            number = _int_arg(qn, variables, 'number')
            pull = repo.pulls.get(number) if number is not None else None
            obj['pullRequest'] = (_pull_obj(store, repo, pull, qn, variables)
                                  if pull is not None else None)
        return {'repository': obj}, []

    if re.search(r'\bviewer\b', qn):
        return {'viewer': {'login': store.viewer['login']}}, []

    # Unrecognised document: name the first requested field, if any.
    m = re.search(r'\{\s*([A-Za-z_][A-Za-z0-9_]*)', qn)
    if m:
        return {}, [_err('Unknown field "%s"' % m.group(1))]
    return {}, [_err('Unknown GraphQL document')]


def _execute_one(store, doc):
    if not isinstance(doc, dict):
        return {'data': {}, 'errors': [_err('Invalid operation document')]}
    query = doc.get('query')
    variables = doc.get('variables') or {}
    if not isinstance(query, str) or not query.strip():
        return {'data': {}, 'errors': [_err('Missing query')]}
    if not isinstance(variables, dict):
        variables = {}
    try:
        data, errors = _dispatch(store, query, variables)
    except Exception as exc:  # never fail the HTTP exchange on one doc
        return {'data': {}, 'errors': [_err('Internal error: %s' % exc)]}
    out = {'data': data}
    if errors:
        out['errors'] = errors
    return out


# ---------------------------------------------------------------------------
# HTTP handler + routes.
# ---------------------------------------------------------------------------

def _graphql(store, req):
    if not store.check_auth(req.headers):
        return json_error(401, 'Unauthorized')
    if req.json_error or req.json is None:
        return json_error(400, 'Invalid JSON body')
    body = req.json
    if isinstance(body, list):
        return json_response(200, [_execute_one(store, doc) for doc in body])
    if isinstance(body, dict):
        return json_response(200, _execute_one(store, body))
    return json_error(400, 'Invalid GraphQL body')


def routes():
    r = route('POST', '/api/graphql')
    return [(r[0], r[1], _graphql)]
