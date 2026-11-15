"""Smart-HTTP git surface for the forge (Python stdlib only, deterministic).

Routes (registered by forge/app.py):
  GET  /{owner}/{repo}.git/info/refs
  POST /{owner}/{repo}.git/git-upload-pack
  POST /{owner}/{repo}.git/git-receive-pack

Protocol notes (kit sdk/conformance/git-client.ts is the authority):
  * upload-pack honours Git-Protocol: version=2; without it, v0.
    v2 answers start directly with `version 2` (no `# service=` preamble).
  * receive-pack is v0-only and always starts with the `# service=` preamble.
  * Auth: a Bearer token is required on every git request; a missing or
    wrong token is 401 (store.check_auth). A missing repo is 404.
  * Malformed pkt-line input never crashes the server: it is answered 400
    and the repo is left untouched.
"""
import re

from forge import gitobj
from forge.http_server import Response, json_error
from forge.store import git_object_id

ZERO_OID = '0' * 40
MAIN_REF = 'refs/heads/main'
HEAD_TARGET = MAIN_REF

_INFO_RE = re.compile(r'^/(?P<owner>[^/]+)/(?P<repo>[^/]+)\.git/info/refs$')
_UPLOAD_RE = re.compile(r'^/(?P<owner>[^/]+)/(?P<repo>[^/]+)\.git/git-upload-pack$')
_RECEIVE_RE = re.compile(r'^/(?P<owner>[^/]+)/(?P<repo>[^/]+)\.git/git-receive-pack$')

_UPLOAD_AD = 'application/x-git-upload-pack-advertisement'
_UPLOAD_RESULT = 'application/x-git-upload-pack-result'
_RECEIVE_AD = 'application/x-git-receive-pack-advertisement'
_RECEIVE_RESULT = 'application/x-git-receive-pack-result'

_UPLOAD_V0_CAPS = ('side-band-64k multi_ack_detailed thin-pack ofs-delta '
                   'agent=forge/1.0 shallow symref=HEAD:refs/heads/main')
_RECEIVE_V0_CAPS = ('report-status side-band-64k delete-refs report-status-v2 '
                    'ofs-delta agent=forge/1.0 symref=HEAD:refs/heads/main')


def routes():
    return [
        ('GET', _INFO_RE, _info_refs),
        ('POST', _UPLOAD_RE, _upload_pack),
        ('POST', _RECEIVE_RE, _receive_pack),
    ]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

def _authed(store, req):
    # store.check_auth returns a plain bool (despite its docstring).
    result = store.check_auth(req.headers)
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)


def _is_v2(req):
    return req.headers.get('git-protocol', '').strip() == 'version=2'


def _sorted_refs(repo):
    return sorted(repo.refs.items())


def _tag_peel(repo, ref_oid):
    """Peel an annotated tag ref to its commit oid, or None."""
    entry = repo.objects.get(ref_oid)
    if entry is None or entry[0] != 'tag':
        return None
    peeled, peeled_type = gitobj.peel_to_commit(repo.objects, ref_oid)
    if peeled_type == 'commit':
        return peeled
    return None


def _ref_lines(repo):
    """v0 advertisement ref lines (without preamble/flush).

    Order: HEAD first, then refs sorted by name; an annotated tag is
    followed by its `^{}` peeled line.
    """
    lines = []
    main_oid = repo.refs.get(MAIN_REF)
    if main_oid:
        lines.append('%s HEAD\n' % main_oid)
    for name, oid in _sorted_refs(repo):
        lines.append('%s %s\n' % (oid, name))
        peeled = _tag_peel(repo, oid)
        if peeled:
            lines.append('%s %s^{}\n' % (peeled, name))
    return lines


def _advertise_v0(repo, service):
    """v0 advertisement: `# service=...` preamble + flush + ref lines + flush.

    The capabilities ride after a NUL on the first ref line (or on the
    `capabilities^{}` pseudo-ref for an empty repo).
    """
    caps = _RECEIVE_V0_CAPS if service == 'git-receive-pack' else _UPLOAD_V0_CAPS
    preamble = '# service=%s\n' % service
    if not repo.refs:
        first = '%s capabilities^{}\x00%s\n' % (ZERO_OID, caps)
        body = [preamble, 'flush', first, 'flush']
    else:
        lines = _ref_lines(repo)
        first = lines[0].rstrip('\n') + '\x00' + caps + '\n'
        body = [preamble, 'flush', first] + lines[1:] + ['flush']
    return gitobj.encode_pkt_lines(body)


def _advertise_v2(repo):
    """v2 capability advertisement: `version 2` first, no preamble."""
    lines = [
        'version 2\n',
        'agent=forge/1.0\n',
        'ls-refs=unborn\n',
        'fetch=shallow thin-pack\n',
        'object-format=sha1\n',
        'flush',
    ]
    return gitobj.encode_pkt_lines(lines)


def _sideband_pack(pack):
    """Frame pack bytes as sideband band-1 pkt-lines (<= 65515 bytes each)."""
    out = bytearray()
    if not pack:
        return bytes(out)
    for start in range(0, len(pack), gitobj.SIDEBAND_MAX_CHUNK):
        chunk = pack[start:start + gitobj.SIDEBAND_MAX_CHUNK]
        out += gitobj.pkt_line(b'\x01' + chunk)
    return bytes(out)


# ---------------------------------------------------------------------------------------------
# info/refs
# ---------------------------------------------------------------------------------------------

def _info_refs(store, req, owner, repo):
    if not _authed(store, req):
        return json_error(401, 'unauthorized')
    found = store.get_repo(owner, repo)
    if found is None:
        return json_error(404, 'repository not found')
    service = req.query_one('service')
    if service == 'git-receive-pack':
        return Response(200, _advertise_v0(found, 'git-receive-pack'), _RECEIVE_AD)
    if service == 'git-upload-pack':
        if _is_v2(req):
            return Response(200, _advertise_v2(found), _UPLOAD_AD)
        return Response(200, _advertise_v0(found, 'git-upload-pack'), _UPLOAD_AD)
    return json_error(400, 'unknown service')


# ---------------------------------------------------------------------------------------------
# upload-pack (v2 ls-refs / fetch, v0 fetch fallback)
# ---------------------------------------------------------------------------------------------

def _ls_refs_lines(repo, args):
    """Build the v2 ls-refs response lines for the parsed arguments."""
    peel = 'peel' in args
    unborn = 'unborn' in args
    prefixes = [a[11:] for a in args if a.startswith('ref-prefix ')]
    main_oid = repo.refs.get(MAIN_REF)

    def keep(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    lines = []
    if main_oid:
        if keep('HEAD'):
            lines.append('%s HEAD symref-target:%s\n' % (main_oid, HEAD_TARGET))
    elif unborn and keep('HEAD'):
        lines.append('unborn HEAD symref-target:%s\n' % HEAD_TARGET)
    for name, oid in _sorted_refs(repo):
        if not keep(name):
            continue
        line = '%s %s' % (oid, name)
        if peel:
            peeled = _tag_peel(repo, oid)
            if peeled:
                line += ' peeled:%s' % peeled
        lines.append(line + '\n')
    return lines


def _parse_fetch_args(lines):
    """Parse the argument section of a v2 fetch / v0 fetch body.

    Returns (wants, haves, shallow, depth, done, deepen_relative).
    """
    wants, haves, shallow = [], [], []
    depth = None
    done = False
    deepen_relative = False
    for raw in lines:
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
        line = text.rstrip('\n')
        if line.startswith('want '):
            oid = line[5:].split(' ', 1)[0]
            if oid != ZERO_OID:
                wants.append(oid)
        elif line.startswith('have '):
            haves.append(line[5:].split(' ', 1)[0])
        elif line.startswith('shallow '):
            shallow.append(line[8:].split(' ', 1)[0])
        elif line.startswith('deepen '):
            try:
                depth = int(line[7:].split(' ', 1)[0])
            except ValueError:
                depth = None
        elif line == 'done':
            done = True
        elif line == 'deepen-relative':
            deepen_relative = True
        # thin-pack / no-progress / ofs-delta / filter / deepen-since /
        # deepen-not / agent / object-format: accepted, ignored.
    return wants, haves, shallow, depth, done, deepen_relative


def _effective_depth(repo, wants, client_shallow, depth, deepen_relative):
    """Resolve the depth limit for a fetch.

    Plain `deepen N` is an absolute depth (the kit's meaning). With
    `deepen-relative` (git CLI --deepen), N is added to the depth of the
    client's current shallow boundary: a boundary commit at distance d
    from a want is at depth d+1, so the new depth is max(d+1) + N.
    """
    if depth is None or not deepen_relative or not client_shallow:
        return depth
    _included, _boundary, _objects = gitobj.collect_depth_limited(
        repo.objects, wants, None)
    distances = gitobj.commit_distances(repo.objects, wants)
    current = 0
    for oid in client_shallow:
        if oid in distances:
            current = max(current, distances[oid] + 1)
    if current == 0:
        return depth
    return current + depth


def _compute_fetch(repo, wants, haves, client_shallow, depth):
    """Work out a fetch: (pack_objects, shallow_list, unshallow_list).

    pack_objects is {oid: (type, bytes)}: the depth-limited closure of the
    wants minus the closure of what the client already holds.
    """
    included, boundary, want_objects = gitobj.collect_depth_limited(
        repo.objects, wants, depth)
    have_objects = gitobj.collect_have_objects(
        repo.objects, haves, client_shallow)
    send_oids = want_objects - have_objects
    pack_objects = {oid: repo.objects[oid] for oid in send_oids
                    if oid in repo.objects}
    unshallow = sorted(oid for oid in client_shallow
                       if oid in included and oid not in boundary)
    return pack_objects, sorted(boundary), unshallow


def _common_haves(repo, wants, haves):
    """Haves reachable from the wants (any depth), in request order."""
    reachable, _boundary, _objects = gitobj.collect_depth_limited(
        repo.objects, wants, None)
    return [have for have in haves if have in reachable]


def _fetch_v2_response(repo, wants, haves, client_shallow, depth, done,
                       deepen_relative):
    """v2 fetch response, mirroring real git (verified against git http-backend).

    Sections are separated by delim (0001); only the packfile section is
    always present. The acknowledgments section is sent only when the
    client sent haves without `done` (git CLI negotiates that way; the
    kit client always sends `done`, and real git then sends no acks).
    shallow-info appears only when it names something.
    """
    depth = _effective_depth(repo, wants, client_shallow, depth,
                             deepen_relative)
    pack_objects, shallow, unshallow = _compute_fetch(
        repo, wants, haves, client_shallow, depth)
    pack = gitobj.write_pack(pack_objects)
    out = bytearray()
    if haves and not done:
        common = _common_haves(repo, wants, haves)
        out += gitobj.encode_pkt_lines(['acknowledgments\n'])
        if common:
            for oid in common:
                out += gitobj.pkt_line('ACK %s\n' % oid)
            out += gitobj.pkt_line('ready\n')
        else:
            out += gitobj.pkt_line('NAK\n')
        out += gitobj.pkt_delim()
    if shallow or unshallow:
        out += gitobj.encode_pkt_lines(['shallow-info\n'])
        for oid in shallow:
            out += gitobj.pkt_line('shallow %s\n' % oid)
        for oid in unshallow:
            out += gitobj.pkt_line('unshallow %s\n' % oid)
        out += gitobj.pkt_delim()
    out += gitobj.encode_pkt_lines(['packfile\n'])
    out += _sideband_pack(pack)
    out += gitobj.pkt_flush()
    return bytes(out)


def _fetch_v0_response(repo, wants, haves, client_shallow, depth):
    """v0 fetch response: plain NAK/ACK/shallow lines, then the sideband pack."""
    pack_objects, shallow, unshallow = _compute_fetch(
        repo, wants, haves, client_shallow, depth)
    pack = gitobj.write_pack(pack_objects)
    # Common haves: reachable from the wants at any depth (negotiation is
    # depth-independent). Real git answers `ACK <oid> common` + `ACK <oid>`.
    common = _common_haves(repo, wants, haves)
    out = bytearray()
    if common:
        out += gitobj.pkt_line('ACK %s common\n' % common[0])
        out += gitobj.pkt_line('ACK %s\n' % common[0])
    else:
        out += gitobj.pkt_line('NAK\n')
    for oid in shallow:
        out += gitobj.pkt_line('shallow %s\n' % oid)
    for oid in unshallow:
        out += gitobj.pkt_line('unshallow %s\n' % oid)
    out += _sideband_pack(pack)
    out += gitobj.pkt_flush()
    return bytes(out)


def _upload_pack(store, req, owner, repo):
    if not _authed(store, req):
        return json_error(401, 'unauthorized')
    found = store.get_repo(owner, repo)
    if found is None:
        return json_error(404, 'repository not found')
    try:
        reader = gitobj.PktReader(req.body)
        first_lines, terminator = reader.read_data_lines()
    except ValueError:
        return json_error(400, 'malformed pkt-line request')
    if not first_lines:
        return json_error(400, 'empty upload-pack request')
    try:
        first = first_lines[0].decode('utf-8')
    except UnicodeDecodeError:
        return json_error(400, 'malformed upload-pack request')
    if _is_v2(req):
        if first.startswith('command=ls-refs'):
            # Args run until the flush after the delim.
            rest, _term = reader.read_data_lines()
            args = set()
            for raw in rest:
                try:
                    args.add(raw.decode('utf-8').rstrip('\n'))
                except UnicodeDecodeError:
                    continue
            lines = _ls_refs_lines(found, args)
            body = gitobj.encode_pkt_lines(lines + ['flush'])
            return Response(200, body, _UPLOAD_RESULT)
        if first.startswith('command=fetch'):
            rest, _term = reader.read_data_lines()
            arg_lines = [raw for raw in rest]
            wants, haves, shallow, depth, done, deepen_rel = _parse_fetch_args(arg_lines)
            body = _fetch_v2_response(found, wants, haves, shallow, depth,
                                      done, deepen_rel)
            return Response(200, body, _UPLOAD_RESULT)
        return json_error(400, 'unknown upload-pack command')
    # v0 fetch: two sections (wants, then haves) separated by flushes.
    # read_data_lines already consumed section one; collect the rest too.
    lines = list(first_lines)
    try:
        rest, _term = reader.read_data_lines()
    except ValueError:
        return json_error(400, 'malformed pkt-line request')
    lines.extend(rest)
    wants, haves, shallow, depth, _done, _rel = _parse_fetch_args(lines)
    if not wants and not haves:
        return json_error(400, 'malformed upload-pack request')
    body = _fetch_v0_response(found, wants, haves, shallow, depth)
    return Response(200, body, _UPLOAD_RESULT)


# ---------------------------------------------------------------------------------------------
# receive-pack (v0 push)
# ---------------------------------------------------------------------------------------------

def _validate_ref_update(repo, old, new, ref):
    """Return None when the update may be applied, else the ng reason."""
    current = repo.refs.get(ref)
    if old == ZERO_OID and new == ZERO_OID:
        return 'non-fast-forward'
    if old == ZERO_OID:
        # create: the ref must not exist yet
        if current is not None:
            return 'non-fast-forward'
        if new not in repo.objects:
            return 'missing object'
        return None
    if new == ZERO_OID:
        # delete: old must be the current tip
        if current is None or current != old:
            return 'non-fast-forward'
        return None
    # update: old must be the current tip and new must descend from old
    if current is None or current != old:
        return 'non-fast-forward'
    if new not in repo.objects:
        return 'missing object'
    if not repo.is_ancestor(old, new):
        return 'non-fast-forward'
    return None


def _receive_pack(store, req, owner, repo):
    if not _authed(store, req):
        return json_error(401, 'unauthorized')
    found = store.get_repo(owner, repo)
    if found is None:
        return json_error(404, 'repository not found')
    try:
        reader = gitobj.PktReader(req.body)
        command_lines, _term = reader.read_data_lines()
    except ValueError:
        return json_error(400, 'malformed pkt-line request')
    if not command_lines:
        return json_error(400, 'empty receive-pack request')
    commands = []
    client_caps = ''
    try:
        for index, raw in enumerate(command_lines):
            if b'\x00' in raw and index == 0:
                line, client_caps = raw.split(b'\x00', 1)
                client_caps = client_caps.decode('utf-8', 'replace')
            else:
                line = raw
            text = line.decode('utf-8').rstrip('\n')
            parts = text.split(' ')
            if len(parts) != 3:
                raise ValueError('bad receive-pack command line')
            old, new, ref = parts
            if len(old) != 40 or len(new) != 40 or not ref.startswith('refs/'):
                raise ValueError('bad receive-pack command line')
            commands.append((old, new, ref))
    except (ValueError, UnicodeDecodeError):
        return json_error(400, 'malformed receive-pack command list')
    pack_bytes = reader.remainder()
    use_sideband = 'side-band-64k' in client_caps.split(' ')

    # Unpack first: a valid pack always reports `unpack ok`, even when a
    # ref update is refused afterwards.
    unpack_ok = True
    unpack_detail = 'ok'
    if pack_bytes:
        try:
            unpacked = gitobj.read_pack(pack_bytes, have_objects=found.objects)['objects']
        except ValueError as exc:
            unpack_ok = False
            unpack_detail = 'unpack failed: %s' % exc
            unpacked = {}
        else:
            for oid, (obj_type, data) in unpacked.items():
                found.objects[oid] = (obj_type, data)
    report = []
    if unpack_ok:
        report.append('unpack ok\n')
        for old, new, ref in commands:
            reason = _validate_ref_update(found, old, new, ref)
            if reason is None:
                if new == ZERO_OID:
                    found.refs.pop(ref, None)
                else:
                    found.refs[ref] = new
                report.append('ok %s\n' % ref)
            else:
                report.append('ng %s %s\n' % (ref, reason))
    else:
        report.append('unpack %s\n' % unpack_detail)
        for _old, _new, ref in commands:
            report.append('ng %s unpack failed\n' % ref)

    if use_sideband:
        # Real git sends the whole report as ONE sideband stream: the
        # report pkt-lines plus an inner flush, chunked into band-1
        # packets, then one outer flush. (Verified against git
        # http-backend: per-line outer packets without the inner flush
        # make git die with "the remote end hung up unexpectedly".)
        inner = gitobj.encode_pkt_lines(report + ['flush'])
        out = bytearray()
        for start in range(0, len(inner), gitobj.SIDEBAND_MAX_CHUNK):
            chunk = inner[start:start + gitobj.SIDEBAND_MAX_CHUNK]
            out += gitobj.pkt_line(b'\x01' + chunk)
        out += gitobj.pkt_flush()
    else:
        out = bytearray(gitobj.encode_pkt_lines(report + ['flush']))
    return Response(200, bytes(out), _RECEIVE_RESULT)
