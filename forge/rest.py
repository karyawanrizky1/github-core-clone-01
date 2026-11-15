"""REST surface (/api/v3) for the github-core-clone-01 forge.

Implements the 55 REST checks from work/spec-rest.md (repos / issues /
pulls / protocol). Python stdlib only, no clock / randomness in responses
or state: timestamps are the constant '2025-01-01T00:00:00Z', commit
author/committer use the fixed epoch 1735689600 (2025-01-01), ids and
issue numbers are sequential counters from the store.

Every REST response carries 'x-github-request-id'. Auth: every endpoint
here requires a valid Bearer token (store.check_auth); without one the
answer is 401. /healthz and GET /api/v3[/] live in forge.core and are
exempt. Unknown routes are answered by the global handler in
forge.http_server (JSON 404), not here.
"""
import base64
import difflib
import re

from forge.http_server import Response, json_response, route
from forge.store import commit_parents, git_object_id

CONST_TS = '2025-01-01T00:00:00Z'
AUTHOR_LINE = b'ada iversen <adaiversen@example.invalid> 1735689600 +0000'


# ---------------------------------------------------------------- helpers

def _rid(store):
    return {'x-github-request-id': store.next_request_id()}


def _ok(store, status, obj, extra=None):
    headers = _rid(store)
    if extra:
        headers.update(extra)
    return json_response(status, obj, headers)


def _err(store, status, message):
    return json_response(status, {'message': message}, _rid(store))


def _auth(store, req):
    """None when authorised, else the 401 response to return."""
    if not store.check_auth(req.headers):
        return _err(store, 401, 'Unauthorized')
    return None


def _body(store, req, required=True):
    """Parse a JSON object body.

    Returns (data, error_response). Malformed JSON -> 400. When
    required=False an absent body yields {} instead of an error.
    """
    if req.json_error:
        return None, _err(store, 400, 'Malformed JSON body')
    if req.json is None:
        if required:
            return None, _err(store, 400, 'Missing JSON body')
        return {}, None
    if not isinstance(req.json, dict):
        return None, _err(store, 400, 'JSON body must be an object')
    return req.json, None


def _repo_or_404(store, owner, name):
    repo = store.get_repo(owner, name)
    if repo is None:
        return None, _err(store, 404, 'Repository not found')
    return repo, None


def _page_params(req):
    try:
        per_page = int(req.query_one('per_page', '30'))
    except ValueError:
        per_page = 30
    per_page = max(1, min(per_page, 100))
    try:
        page = int(req.query_one('page', '1'))
    except ValueError:
        page = 1
    page = max(1, page)
    return per_page, page


def _paginate(items, req):
    per_page, page = _page_params(req)
    start = (page - 1) * per_page
    return items[start:start + per_page], per_page, page, len(items)


def _repo_json(store, repo):
    return {
        'id': repo.database_id,
        'name': repo.name,
        'full_name': repo.full_name(),
        'private': repo.private,
        'description': repo.description,
        'default_branch': repo.default_branch,
        'owner': {'login': repo.owner, 'id': store.viewer['id']},
    }


def _issue_json(store, repo, issue):
    return {
        'id': store.issue_global_id(repo, issue['number']),
        'number': issue['number'],
        'title': issue['title'],
        'body': issue['body'],
        'state': issue['state'],
        'labels': [{'name': label['name']} for label in issue['labels']],
        'created_at': CONST_TS,
        'updated_at': CONST_TS,
    }


def _pull_json(store, repo, pull):
    return {
        'id': store.pull_global_id(repo, pull['number']),
        'number': pull['number'],
        'title': pull['title'],
        'body': pull['body'],
        'state': pull['state'],
        'head': dict(pull['head']),
        'base': dict(pull['base']),
        'merged': pull['merged'],
        'merged_at': pull['merged_at'],
        'merge_commit_sha': pull['merge_commit_sha'],
        'created_at': CONST_TS,
        'updated_at': CONST_TS,
    }


# ------------------------------------------------------- git object helpers

def _parse_tree(data):
    entries = []
    i = 0
    while i < len(data):
        sp = data.index(b' ', i)
        mode = data[i:sp].decode('ascii')
        nul = data.index(b'\x00', sp)
        name = data[sp + 1:nul].decode('utf-8')
        sha = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        i = nul + 21
    return entries


def _flatten(repo, tree_sha, prefix=''):
    """path -> (blob_sha, bytes) for every file under a tree."""
    out = {}
    if tree_sha is None:
        return out
    entry = repo.objects.get(tree_sha)
    if entry is None or entry[0] != 'tree':
        return out
    for mode, name, sha in _parse_tree(entry[1]):
        path = prefix + name
        if mode == '40000':
            out.update(_flatten(repo, sha, path + '/'))
        else:
            blob = repo.objects.get(sha)
            if blob is not None:
                out[path] = (sha, blob[1])
    return out


def _write_tree(repo, files):
    """Build blob+tree objects for {path: bytes}; return the root tree sha."""
    root = {}
    for path, data in files.items():
        parts = path.split('/')
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data

    def build(node):
        entries = []
        for name in sorted(node.keys()):
            value = node[name]
            if isinstance(value, dict):
                entries.append(('40000', name, build(value)))
            else:
                sha = git_object_id('blob', value)
                repo.objects[sha] = ('blob', value)
                entries.append(('100644', name, sha))
        raw = b''
        for mode, name, sha in entries:
            raw += ('%s %s' % (mode, name)).encode('utf-8')
            raw += b'\x00' + bytes.fromhex(sha)
        sha = git_object_id('tree', raw)
        repo.objects[sha] = ('tree', raw)
        return sha

    return build(root)


def _make_commit(repo, tree_sha, parent_sha, message):
    lines = [b'tree ' + tree_sha.encode('ascii')]
    if parent_sha:
        lines.append(b'parent ' + parent_sha.encode('ascii'))
    lines.append(b'author ' + AUTHOR_LINE)
    lines.append(b'committer ' + AUTHOR_LINE)
    raw = b'\n'.join(lines) + b'\n\n' + message.encode('utf-8')
    sha = git_object_id('commit', raw)
    repo.objects[sha] = ('commit', raw)
    return sha


def _commit_message(raw):
    parts = raw.split(b'\n\n', 1)
    return parts[1].decode('utf-8', 'replace') if len(parts) > 1 else ''


def _commit_list(repo, tip):
    result = []
    seen = set()
    stack = [tip] if tip else []
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != 'commit':
            continue
        result.append({'sha': oid,
                       'commit': {'message': _commit_message(entry[1])}})
        for parent in commit_parents(entry[1]):
            stack.append(parent)
    return result


def _resolve(repo, tree_sha, parts):
    """Resolve path parts in a tree -> ('blob', sha) | ('tree', sha) | None."""
    cur_sha = tree_sha
    for index, part in enumerate(parts):
        entry = repo.objects.get(cur_sha)
        if entry is None or entry[0] != 'tree':
            return None
        found = None
        for mode, name, sha in _parse_tree(entry[1]):
            if name == part:
                found = (mode, sha)
                break
        if found is None:
            return None
        mode, sha = found
        if mode == '40000':
            cur_sha = sha
        else:
            if index == len(parts) - 1:
                return ('blob', sha)
            return None
    return ('tree', cur_sha)


def _diff_files(repo, base_tree_sha, head_tree_sha):
    """Diff two trees -> list of pull-file dicts."""
    base = _flatten(repo, base_tree_sha)
    head = _flatten(repo, head_tree_sha)
    files = []
    for path in sorted(set(base) | set(head)):
        if path not in base:
            new = head[path][1]
            adds = len(new.splitlines())
            files.append({'filename': path, 'status': 'added',
                          'additions': adds, 'deletions': 0, 'changes': adds})
        elif path not in head:
            old = base[path][1]
            dels = len(old.splitlines())
            files.append({'filename': path, 'status': 'removed',
                          'additions': 0, 'deletions': dels, 'changes': dels})
        elif base[path][0] != head[path][0]:
            old_lines = base[path][1].decode('utf-8', 'replace').splitlines()
            new_lines = head[path][1].decode('utf-8', 'replace').splitlines()
            adds = dels = 0
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                    a=old_lines, b=new_lines).get_opcodes():
                if tag in ('insert', 'replace'):
                    adds += j2 - j1
                if tag in ('delete', 'replace'):
                    dels += i2 - i1
            if adds == 0 and dels == 0:
                adds = 1
            files.append({'filename': path, 'status': 'modified',
                          'additions': adds, 'deletions': dels,
                          'changes': adds + dels})
    return files


# ------------------------------------------------------------------ /user

def _get_user(store, req):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    viewer = store.viewer
    return _ok(store, 200, {
        'login': viewer['login'],
        'id': viewer['id'],
        'name': viewer['name'],
        'email': viewer['email'],
        'type': 'User',
    })


def _get_meta(store, req):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    return _ok(store, 200, {'installed_version': '1.0.0'})


def _list_user_repos(store, req):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    repos = store.list_repos_for_viewer()
    page_items, per_page, page, total = _paginate(repos, req)
    extra = {}
    if (page * per_page) < total:
        host = req.headers.get('host', '127.0.0.1')
        url = 'http://%s/api/v3/user/repos?per_page=%d&page=%d' % (
            host, per_page, page + 1)
        extra['Link'] = '<%s>; rel="next"' % url
    return _ok(store, 200, [_repo_json(store, r) for r in page_items], extra)


def _create_user_repo(store, req):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    data, err = _body(store, req)
    if err is not None:
        return err
    name = data.get('name')
    if not name or not isinstance(name, str):
        return _err(store, 400, 'Repository name is required')
    repo = store.create_repo(name, data.get('description', ''),
                             data.get('private', True))
    if repo is None:
        return _err(store, 422, 'Repository already exists')
    return _ok(store, 201, _repo_json(store, repo))


# ------------------------------------------------------------------ repos

def _get_repo(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    return _ok(store, 200, _repo_json(store, found))


def _patch_repo(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    if 'description' in data:
        found.description = data['description'] or ''
    if 'private' in data:
        found.private = bool(data['private'])
    return _ok(store, 200, _repo_json(store, found))


def _delete_repo(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    store.delete_repo(owner, repo)
    return Response(204, b'', headers=_rid(store))


def _list_branches(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    branches = []
    for refname, oid in found.refs.items():
        if refname.startswith('refs/heads/'):
            branches.append({'name': refname[len('refs/heads/'):],
                             'commit': {'sha': oid}})
    return _ok(store, 200, branches)


def _list_commits(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    sha_param = req.query_one('sha')
    if sha_param:
        tip = found.refs.get('refs/heads/' + sha_param)
        if tip is None:
            if sha_param in found.objects:
                tip = sha_param
            else:
                return _err(store, 404, 'No such branch or commit')
    else:
        tip = found.head_oid()
    if tip is None:
        return _ok(store, 200, [])
    return _ok(store, 200, _commit_list(found, tip))


def _create_ref(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    ref = data.get('ref')
    sha = data.get('sha')
    if not ref or not sha:
        return _err(store, 400, 'ref and sha are required')
    found.refs[ref] = sha
    return _ok(store, 201, {'ref': ref, 'object': {'sha': sha}})


# --------------------------------------------------------------- contents

def _tip_tree(store, repo, req):
    """(tree_sha, tip_oid) for ?ref=<branch> or the default branch."""
    ref = req.query_one('ref')
    branch = ref or repo.default_branch
    tip = repo.refs.get('refs/heads/' + branch)
    if tip is None:
        return None, None
    entry = repo.objects.get(tip)
    if entry is None or entry[0] != 'commit':
        return None, None
    from forge.store import commit_tree
    return commit_tree(entry[1]), tip


def _get_contents(store, req, owner, repo, path):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    tree_sha, _tip = _tip_tree(store, found, req)
    if tree_sha is None:
        return _err(store, 404, 'No such file or directory')
    resolved = _resolve(found, tree_sha, path.split('/'))
    if resolved is None:
        return _err(store, 404, 'No such file or directory')
    kind, sha = resolved
    if kind == 'blob':
        blob = found.objects.get(sha)
        data = blob[1] if blob else b''
        return _ok(store, 200, {
            'type': 'file',
            'encoding': 'base64',
            'name': path.split('/')[-1],
            'path': path,
            'content': base64.b64encode(data).decode('ascii'),
            'sha': sha,
            'size': len(data),
        })
    entry = found.objects.get(sha)
    items = []
    if entry is not None:
        for mode, name, child_sha in _parse_tree(entry[1]):
            items.append({
                'type': 'dir' if mode == '40000' else 'file',
                'name': name,
                'path': path + '/' + name,
                'sha': child_sha,
            })
    return _ok(store, 200, items)


def _put_contents(store, req, owner, repo, path):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    content_b64 = data.get('content')
    if not isinstance(content_b64, str):
        return _err(store, 400, 'content is required')
    try:
        raw = base64.b64decode(''.join(content_b64.split()), validate=True)
    except Exception:
        return _err(store, 400, 'content is not valid base64')
    message = data.get('message') or ''
    branch = data.get('branch') or found.default_branch
    refname = 'refs/heads/' + branch
    tip = found.refs.get(refname)
    files = {}
    if tip is not None:
        entry = found.objects.get(tip)
        if entry is not None and entry[0] == 'commit':
            from forge.store import commit_tree
            files = _flatten(found, commit_tree(entry[1]))
    if data.get('sha'):
        current = files.get(path)
        if current is None or current[0].lower() != str(data['sha']).lower():
            return _err(store, 409, 'sha does not match the current file')
    is_update = path in files
    files[path] = (None, raw)
    plain = {p: content for p, (_s, content) in files.items()}
    tree_sha = _write_tree(found, plain)
    commit_sha = _make_commit(found, tree_sha, tip, message)
    found.refs[refname] = commit_sha
    blob_sha = git_object_id('blob', raw)
    status = 200 if is_update else 201
    return _ok(store, status, {
        'content': {
            'type': 'file',
            'encoding': 'base64',
            'name': path.split('/')[-1],
            'path': path,
            'content': content_b64,
            'sha': blob_sha,
            'size': len(raw),
        },
        'commit': {'sha': commit_sha},
    })


def _delete_contents(store, req, owner, repo, path):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    branch = data.get('branch') or found.default_branch
    refname = 'refs/heads/' + branch
    tip = found.refs.get(refname)
    files = {}
    if tip is not None:
        entry = found.objects.get(tip)
        if entry is not None and entry[0] == 'commit':
            from forge.store import commit_tree
            files = _flatten(found, commit_tree(entry[1]))
    current = files.get(path)
    if current is None:
        return _err(store, 404, 'No such file')
    if data.get('sha') and current[0].lower() != str(data['sha']).lower():
        return _err(store, 409, 'sha does not match the current file')
    del files[path]
    plain = {p: content for p, (_s, content) in files.items()}
    tree_sha = _write_tree(found, plain)
    commit_sha = _make_commit(found, tree_sha, tip, data.get('message') or '')
    found.refs[refname] = commit_sha
    return _ok(store, 200, {'commit': {'sha': commit_sha}})


# ----------------------------------------------------------------- labels

def _create_label(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    name = data.get('name')
    if not name:
        return _err(store, 400, 'Label name is required')
    found.labels[name] = {'name': name, 'color': data.get('color', '')}
    return _ok(store, 201, {'name': name, 'color': data.get('color', '')})


# ----------------------------------------------------------------- issues

def _label_names(raw_labels):
    names = []
    for item in raw_labels or []:
        if isinstance(item, dict):
            names.append(item.get('name'))
        else:
            names.append(item)
    return [n for n in names if n]


def _create_issue(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    names = _label_names(data.get('labels'))
    for name in names:
        if name not in found.labels:
            return _err(store, 422, 'Unknown label: %s' % name)
    issue = store.create_issue(found, data.get('title', ''),
                               data.get('body', ''),
                               [{'name': n} for n in names])
    return _ok(store, 201, _issue_json(store, found, issue))


def _list_issues(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    state = req.query_one('state', 'open')
    issues = list(found.issues.values())
    if state != 'all':
        issues = [i for i in issues if i['state'] == state]
    direction = req.query_one('direction', 'asc')
    reverse = direction == 'desc'
    issues.sort(key=lambda i: i['created_seq'], reverse=reverse)
    page_items, _pp, _pg, _total = _paginate(issues, req)
    return _ok(store, 200, [_issue_json(store, found, i) for i in page_items])


def _get_issue(store, req, owner, repo, number):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    try:
        num = int(number)
    except ValueError:
        return _err(store, 404, 'Issue not found')
    issue = found.issues.get(num)
    if issue is None:
        return _err(store, 404, 'Issue not found')
    return _ok(store, 200, _issue_json(store, found, issue))


def _patch_issue(store, req, owner, repo, number):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    try:
        num = int(number)
    except ValueError:
        return _err(store, 404, 'Issue not found')
    issue = found.issues.get(num)
    if issue is None:
        return _err(store, 404, 'Issue not found')
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    if 'state' in data:
        issue['state'] = data['state']
    if 'title' in data:
        issue['title'] = data['title']
    if 'body' in data:
        issue['body'] = data['body']
    return _ok(store, 200, _issue_json(store, found, issue))


def _create_comment(store, req, owner, repo, number):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    try:
        num = int(number)
    except ValueError:
        return _err(store, 404, 'Issue not found')
    issue = found.issues.get(num)
    if issue is None:
        return _err(store, 404, 'Issue not found')
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    comment = {'id': store.next_comment_id(), 'body': data.get('body', '')}
    issue['comments'].append(comment)
    return _ok(store, 201, dict(comment))


def _list_comments(store, req, owner, repo, number):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    try:
        num = int(number)
    except ValueError:
        return _err(store, 404, 'Issue not found')
    issue = found.issues.get(num)
    if issue is None:
        return _err(store, 404, 'Issue not found')
    return _ok(store, 200, [dict(c) for c in issue['comments']])


# ------------------------------------------------------------------ pulls

def _create_pull(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    data, berr = _body(store, req)
    if berr is not None:
        return berr
    head_ref = data.get('head')
    base_ref = data.get('base')
    head_tip = found.refs.get('refs/heads/' + str(head_ref)) if head_ref else None
    base_tip = found.refs.get('refs/heads/' + str(base_ref)) if base_ref else None
    if head_tip is None or base_tip is None:
        return _err(store, 404, 'Head or base branch not found')
    from forge.store import commit_tree
    head_tree = commit_tree(found.objects[head_tip][1])
    base_tree = commit_tree(found.objects[base_tip][1])
    pull = store.create_pull(found, data.get('title', ''), data.get('body', ''),
                             head_ref, base_ref, head_tip, base_tip)
    pull['files'] = _diff_files(found, base_tree, head_tree)
    pull['_base_tree'] = base_tree
    pull['_head_tree'] = head_tree
    return _ok(store, 201, _pull_json(store, found, pull))


def _list_pulls(store, req, owner, repo):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    state = req.query_one('state', 'open')
    pulls = list(found.pulls.values())
    if state != 'all':
        pulls = [p for p in pulls if p['state'] == state]
    pulls.sort(key=lambda p: p['number'])
    page_items, _pp, _pg, _total = _paginate(pulls, req)
    return _ok(store, 200, [_pull_json(store, found, p) for p in page_items])


def _get_pull(store, req, owner, repo, number):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    try:
        num = int(number)
    except ValueError:
        return _err(store, 404, 'Pull request not found')
    pull = found.pulls.get(num)
    if pull is None:
        return _err(store, 404, 'Pull request not found')
    return _ok(store, 200, _pull_json(store, found, pull))


def _list_pull_files(store, req, owner, repo, number):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    try:
        num = int(number)
    except ValueError:
        return _err(store, 404, 'Pull request not found')
    pull = found.pulls.get(num)
    if pull is None:
        return _err(store, 404, 'Pull request not found')
    return _ok(store, 200, pull.get('files', []))


def _merge_pull(store, req, owner, repo, number):
    unauth = _auth(store, req)
    if unauth is not None:
        return unauth
    found, err = _repo_or_404(store, owner, repo)
    if err is not None:
        return err
    try:
        num = int(number)
    except ValueError:
        return _err(store, 404, 'Pull request not found')
    pull = found.pulls.get(num)
    if pull is None:
        return _err(store, 404, 'Pull request not found')
    data, berr = _body(store, req, required=False)
    if berr is not None:
        return berr
    if pull['merged']:
        return _ok(store, 200, {
            'merged': True,
            'sha': pull['merge_commit_sha'],
            'message': 'Pull request already merged',
        })
    from forge.store import commit_tree
    head_ref = pull['head']['ref']
    base_ref = pull['base']['ref']
    head_tip = found.refs.get('refs/heads/' + head_ref)
    base_tip = found.refs.get('refs/heads/' + base_ref)
    if head_tip is None or base_tip is None:
        return _err(store, 404, 'Head or base branch not found')
    base_tree_then = pull.get('_base_tree')
    base_files_then = _flatten(found, base_tree_then)
    base_files_now = _flatten(found, commit_tree(found.objects[base_tip][1]))
    head_files_now = _flatten(found, commit_tree(found.objects[head_tip][1]))
    # Conflict: a path changed on BOTH sides since the PR was opened,
    # to different contents.
    changed_paths = set(base_files_then) | set(base_files_now) | set(head_files_now)
    for path in changed_paths:
        base_then = base_files_then.get(path)
        base_now = base_files_now.get(path)
        head_now = head_files_now.get(path)
        base_changed = (base_now is None) != (base_then is None) or (
            base_now is not None and base_then is not None
            and base_now[1] != base_then[1])
        head_changed = (head_now is None) != (base_then is None) or (
            head_now is not None and base_then is not None
            and head_now[1] != base_then[1])
        if base_changed and head_changed:
            base_bytes = base_now[1] if base_now else None
            head_bytes = head_now[1] if head_now else None
            if base_bytes != head_bytes:
                return _err(store, 405, 'Merge conflict on %s' % path)
    # Squash: one commit, tree = current base files + head's changes.
    merged = {p: content for p, (_s, content) in base_files_now.items()}
    for path, (_s, content) in head_files_now.items():
        merged[path] = content
    for path in list(merged):
        if path not in head_files_now and path in base_files_then:
            # file deleted on head side relative to PR base: drop it only
            # if head actually removed it (not present in head tree).
            if path in base_files_now:
                del merged[path]
    tree_sha = _write_tree(found, merged)
    message = data.get('commit_message') or data.get('message') or (
        'Merge pull request #%d' % num)
    commit_sha = _make_commit(found, tree_sha, base_tip, message)
    found.refs['refs/heads/' + base_ref] = commit_sha
    pull['state'] = 'closed'
    pull['merged'] = True
    pull['merged_at'] = CONST_TS
    pull['merge_commit_sha'] = commit_sha
    return _ok(store, 200, {
        'merged': True,
        'sha': commit_sha,
        'message': 'Pull request successfully merged',
    })


# ------------------------------------------------------------------ routes

_CONTENTS_RE = re.compile(
    r'^/api/v3/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/contents/(?P<path>.+)$')


def routes():
    out = []

    def add(method, pattern, handler):
        m, rx = route(method, pattern)
        out.append((m, rx, handler))

    add('GET', '/api/v3/user', _get_user)
    add('GET', '/api/v3/meta', _get_meta)
    add('GET', '/api/v3/user/repos', _list_user_repos)
    add('POST', '/api/v3/user/repos', _create_user_repo)
    add('GET', '/api/v3/repos/{owner}/{repo}', _get_repo)
    add('PATCH', '/api/v3/repos/{owner}/{repo}', _patch_repo)
    add('DELETE', '/api/v3/repos/{owner}/{repo}', _delete_repo)
    add('GET', '/api/v3/repos/{owner}/{repo}/branches', _list_branches)
    add('GET', '/api/v3/repos/{owner}/{repo}/commits', _list_commits)
    add('POST', '/api/v3/repos/{owner}/{repo}/git/refs', _create_ref)
    add('POST', '/api/v3/repos/{owner}/{repo}/labels', _create_label)
    add('POST', '/api/v3/repos/{owner}/{repo}/issues', _create_issue)
    add('GET', '/api/v3/repos/{owner}/{repo}/issues', _list_issues)
    add('GET', '/api/v3/repos/{owner}/{repo}/issues/{number}', _get_issue)
    add('PATCH', '/api/v3/repos/{owner}/{repo}/issues/{number}', _patch_issue)
    add('GET', '/api/v3/repos/{owner}/{repo}/issues/{number}/comments',
        _list_comments)
    add('POST', '/api/v3/repos/{owner}/{repo}/issues/{number}/comments',
        _create_comment)
    add('POST', '/api/v3/repos/{owner}/{repo}/pulls', _create_pull)
    add('GET', '/api/v3/repos/{owner}/{repo}/pulls', _list_pulls)
    add('GET', '/api/v3/repos/{owner}/{repo}/pulls/{number}', _get_pull)
    add('GET', '/api/v3/repos/{owner}/{repo}/pulls/{number}/files',
        _list_pull_files)
    add('PUT', '/api/v3/repos/{owner}/{repo}/pulls/{number}/merge',
        _merge_pull)
    # Contents paths contain '/' so the {path} helper (single segment)
    # cannot express them; a hand-built regex is used instead.
    out.append(('GET', _CONTENTS_RE, _get_contents))
    out.append(('PUT', _CONTENTS_RE, _put_contents))
    out.append(('DELETE', _CONTENTS_RE, _delete_contents))
    return out
