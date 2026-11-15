"""Git objects and packfiles for the forge (Python stdlib only, deterministic).

No clock, no randomness: pack bytes depend only on the objects, and object
order is fixed (commit, tree, blob, tag, then oid), so two runs over the same
store produce identical packs.

Covers kit sdk/conformance/git-client.ts: pkt-line codec, object parsing
(commit/tree/tag, stored verbatim), pack read (plain + OFS/REF deltas, thin
packs, trailer verification, per-object SHA-1 recheck) and pack write
(plain, version 2), plus reachability / depth-limited closure helpers.
"""
import hashlib
import struct
import zlib

from forge.store import commit_parents, commit_tree, git_object_id

ZERO_OID = '0' * 40

TYPE_CODES = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}
CODE_TYPES = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}

# Max total pkt-line length (4 header bytes + payload), mirroring the kit.
PKT_LINE_MAX = 65520
PKT_LINE_MAX_PAYLOAD = PKT_LINE_MAX - 4
# Max pack bytes carried in one sideband packet: 1 band byte + payload.
SIDEBAND_MAX_CHUNK = PKT_LINE_MAX_PAYLOAD - 1

_HEX = set('0123456789abcdefABCDEF')


def _is_oid(text):
    return isinstance(text, str) and len(text) == 40 and all(c in _HEX for c in text)


# ---------------------------------------------------------------------------------------------
# pkt-line
# ---------------------------------------------------------------------------------------------

def pkt_line(payload):
    """Encode one data pkt-line: 4 hex length bytes (payload + 4) + payload."""
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    if len(payload) > PKT_LINE_MAX_PAYLOAD:
        raise ValueError('pkt-line payload too long: %d' % len(payload))
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


def pkt_flush():
    return b'0000'


def pkt_delim():
    return b'0001'


def encode_pkt_lines(items):
    """Encode a mixed sequence; items are bytes/str or 'flush'/'delim'."""
    out = bytearray()
    for item in items:
        if item == 'flush':
            out += pkt_flush()
        elif item == 'delim':
            out += pkt_delim()
        else:
            out += pkt_line(item)
    return bytes(out)


class PktReader(object):
    """Incremental pkt-line reader over a bytes buffer.

    next() returns (kind, payload) with kind in 'data', 'flush', 'delim',
    'response-end', or None at end of input. Malformed input raises
    ValueError (never crashes the caller). ``pos`` is the offset just past
    the last packet consumed, so a handler can take the raw remainder
    (e.g. the pack appended after a receive-pack command list).
    """

    def __init__(self, data):
        self.data = bytes(data)
        self.pos = 0

    def next(self):
        data = self.data
        if self.pos >= len(data):
            return None
        if self.pos + 4 > len(data):
            raise ValueError('truncated pkt-line header')
        header = data[self.pos:self.pos + 4]
        try:
            text = header.decode('ascii')
        except UnicodeDecodeError:
            raise ValueError('pkt-line header is not ascii')
        if any(c not in _HEX for c in text):
            raise ValueError('invalid pkt-line length %r' % text)
        length = int(text, 16)
        if length == 0:
            self.pos += 4
            return ('flush', None)
        if length == 1:
            self.pos += 4
            return ('delim', None)
        if length == 2:
            self.pos += 4
            return ('response-end', None)
        if length < 4 or length > PKT_LINE_MAX:
            raise ValueError('invalid pkt-line length %d' % length)
        if self.pos + length > len(data):
            raise ValueError('truncated pkt-line payload')
        payload = data[self.pos + 4:self.pos + length]
        self.pos += length
        return ('data', payload)

    def read_data_lines(self):
        """Read data packets until flush/delim/end. Returns (lines, terminator).

        terminator is 'flush', 'delim', 'response-end' or None (end of input).
        """
        lines = []
        while True:
            packet = self.next()
            if packet is None:
                return lines, None
            kind, payload = packet
            if kind == 'data':
                lines.append(payload)
            else:
                return lines, kind

    def remainder(self):
        return self.data[self.pos:]


def parse_pkt_lines(data):
    """Decode a whole buffer into a list of (kind, payload) packets."""
    reader = PktReader(data)
    packets = []
    while True:
        packet = reader.next()
        if packet is None:
            return packets
        packets.append(packet)


# ---------------------------------------------------------------------------------------------
# object parsing (objects are stored verbatim; these only read structure)
# ---------------------------------------------------------------------------------------------

def parse_commit(data):
    """Parse a commit object. Raises ValueError on a bad tree/parent oid."""
    head, sep, message = data.partition(b'\n\n')
    if not sep:
        head, message = data, b''
    result = {'tree': None, 'parents': [], 'author': None, 'committer': None,
              'message': message, 'headers': []}
    for line in head.split(b'\n'):
        if not line:
            continue
        if b' ' in line:
            key, value = line.split(b' ', 1)
        else:
            key, value = line, b''
        key = key.decode('ascii', 'replace')
        result['headers'].append((key, value))
        if key == 'tree':
            oid = value.decode('ascii', 'replace').strip()
            if not _is_oid(oid):
                raise ValueError('commit tree is not an object id: %r' % oid)
            result['tree'] = oid
        elif key == 'parent':
            oid = value.decode('ascii', 'replace').strip()
            if not _is_oid(oid):
                raise ValueError('commit parent is not an object id: %r' % oid)
            result['parents'].append(oid)
        elif key == 'author':
            result['author'] = value.decode('utf-8', 'replace')
        elif key == 'committer':
            result['committer'] = value.decode('utf-8', 'replace')
    if result['tree'] is None:
        raise ValueError('commit has no tree')
    return result


def parse_tree(data):
    """Parse a tree object into [{mode, name (bytes), oid (hex)}]."""
    entries = []
    pos = 0
    size = len(data)
    while pos < size:
        space = data.find(b' ', pos)
        if space < 0:
            raise ValueError('tree entry has no mode')
        mode = data[pos:space].decode('ascii', 'replace')
        if not mode or any(c not in '01234567' for c in mode):
            raise ValueError('tree entry has a bad mode: %r' % mode)
        nul = data.find(b'\x00', space + 1)
        if nul < 0:
            raise ValueError('tree entry has no name terminator')
        name = data[space + 1:nul]
        if nul + 21 > size:
            raise ValueError('tree entry is truncated')
        oid = data[nul + 1:nul + 21].hex()
        entries.append({'mode': mode, 'name': name, 'oid': oid})
        pos = nul + 21
    return entries


def parse_tag(data):
    """Parse an annotated tag object. Raises ValueError on bad object/type."""
    head, sep, message = data.partition(b'\n\n')
    if not sep:
        head, message = data, b''
    result = {'object': None, 'type': None, 'tag': None, 'tagger': None,
              'message': message, 'headers': []}
    for line in head.split(b'\n'):
        if not line:
            continue
        if b' ' in line:
            key, value = line.split(b' ', 1)
        else:
            key, value = line, b''
        key = key.decode('ascii', 'replace')
        result['headers'].append((key, value))
        if key == 'object':
            oid = value.decode('ascii', 'replace').strip()
            if not _is_oid(oid):
                raise ValueError('tag object is not an object id: %r' % oid)
            result['object'] = oid
        elif key == 'type':
            result['type'] = value.decode('ascii', 'replace').strip()
        elif key == 'tag':
            result['tag'] = value.decode('utf-8', 'replace')
        elif key == 'tagger':
            result['tagger'] = value.decode('utf-8', 'replace')
    if result['object'] is None:
        raise ValueError('tag has no object')
    if result['type'] not in ('commit', 'tree', 'blob', 'tag'):
        raise ValueError('tag has a bad type: %r' % result['type'])
    return result


# ---------------------------------------------------------------------------------------------
# packfile
# ---------------------------------------------------------------------------------------------

def _read_size_varint(data, pos):
    """Pack entry size/type varint. Returns (size, type_code, next_pos)."""
    if pos >= len(data):
        raise ValueError('pack entry has no size header')
    byte = data[pos]
    size = byte & 0x0F
    type_code = (byte >> 4) & 0x07
    shift = 4
    pos += 1
    while byte & 0x80:
        if pos >= len(data):
            raise ValueError('pack entry size varint is truncated')
        if shift > 63:
            raise ValueError('pack entry size varint is too long')
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return size, type_code, pos


def _read_ofs_varint(data, pos):
    """OFS_DELTA base-offset varint. Returns (base_offset_distance, next_pos)."""
    if pos >= len(data):
        raise ValueError('ofs-delta offset is truncated')
    byte = data[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        if pos >= len(data):
            raise ValueError('ofs-delta offset is truncated')
        byte = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


def _read_delta_varint(data, pos):
    """Generic 7-bit little-endian varint used in delta headers."""
    value = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError('delta varint is truncated')
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if shift > 63:
            raise ValueError('delta varint is too long')
        if not (byte & 0x80):
            return value, pos


def apply_delta(base, delta):
    """Apply a git delta to base bytes. Raises ValueError on any bad opcode/bound."""
    src_size, pos = _read_delta_varint(delta, 0)
    if src_size != len(base):
        raise ValueError('delta source size %d does not match base %d'
                         % (src_size, len(base)))
    tgt_size, pos = _read_delta_varint(delta, pos)
    out = bytearray()
    while pos < len(delta):
        opcode = delta[pos]
        pos += 1
        if opcode & 0x80:
            # copy from base: offset/size bytes selected by opcode bits
            offset = 0
            size = 0
            for i in range(4):
                if opcode & (1 << i):
                    if pos >= len(delta):
                        raise ValueError('delta copy opcode is truncated')
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if opcode & (0x10 << i):
                    if pos >= len(delta):
                        raise ValueError('delta copy opcode is truncated')
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            if offset + size > len(base):
                raise ValueError('delta copy runs past the base')
            out += base[offset:offset + size]
        elif opcode:
            # literal insert
            if pos + opcode > len(delta):
                raise ValueError('delta literal runs past the end')
            out += delta[pos:pos + opcode]
            pos += opcode
        else:
            raise ValueError('delta opcode 0 is invalid')
    if len(out) != tgt_size:
        raise ValueError('delta result size %d does not match target %d'
                         % (len(out), tgt_size))
    return bytes(out)


def read_pack(data, have_objects=None):
    """Parse and verify a packfile.

    have_objects maps oid -> (type, bytes) for thin-pack bases the pack
    omits (the receiver already holds them). Returns a dict with version,
    declared_count, objects {oid: (type, bytes)}, entries and delta_count.
    Raises ValueError on any corruption: bad header, bad trailer, truncated
    entry, inflate size mismatch, delta error/cycle, missing base, or a
    resolved object whose SHA-1 does not match its claimed base oid.
    """
    have_objects = have_objects or {}
    if len(data) < 12 + 20:
        raise ValueError('pack is too short')
    if data[0:4] != b'PACK':
        raise ValueError('pack has a bad magic')
    version, count = struct.unpack('>II', data[4:12])
    if version not in (2, 3):
        raise ValueError('pack version %d is not supported' % version)
    trailer = data[-20:]
    if hashlib.sha1(data[:-20]).digest() != trailer:
        raise ValueError('pack trailer does not match its bytes')
    end = len(data) - 20
    pos = 12
    entries = []
    by_offset = {}
    for _ in range(count):
        if pos >= end:
            raise ValueError('pack is truncated')
        entry_offset = pos
        size, type_code, pos = _read_size_varint(data, pos)
        entry = {'offset': entry_offset, 'size': size, 'type_code': type_code,
                 'base_offset': None, 'base_oid': None, 'data': None}
        if type_code == 6:  # OFS_DELTA
            distance, pos = _read_ofs_varint(data, pos)
            base_offset = entry_offset - distance
            if base_offset < 0 or base_offset >= entry_offset:
                raise ValueError('ofs-delta base offset is out of range')
            entry['base_offset'] = base_offset
        elif type_code == 7:  # REF_DELTA
            if pos + 20 > end:
                raise ValueError('ref-delta base oid is truncated')
            entry['base_oid'] = data[pos:pos + 20].hex()
            pos += 20
        elif type_code not in CODE_TYPES:
            raise ValueError('pack entry has an unknown type %d' % type_code)
        # Inflate exactly one zlib stream starting at pos.
        decomp = zlib.decompressobj()
        try:
            inflated = decomp.decompress(data[pos:end])
        except zlib.error as exc:
            raise ValueError('pack entry does not inflate: %s' % exc)
        if not decomp.eof:
            raise ValueError('pack entry zlib stream is truncated')
        consumed = (end - pos) - len(decomp.unused_data)
        if consumed <= 0:
            raise ValueError('pack entry has no zlib data')
        if len(inflated) != size:
            raise ValueError('pack entry inflates to %d bytes, declared %d'
                             % (len(inflated), size))
        entry['data'] = inflated
        pos += consumed
        entries.append(entry)
        by_offset[entry_offset] = entry
    if pos != end:
        raise ValueError('pack has trailing bytes before the trailer')

    objects = {}
    resolved = {}  # id(entry) -> (oid, type, bytes)
    delta_count = sum(1 for entry in entries if entry['type_code'] in (6, 7))
    # Resolve iteratively so a delta may follow its base in any order.
    pending = list(entries)
    while pending:
        progressed = False
        remaining = []
        for entry in pending:
            code = entry['type_code']
            if code in CODE_TYPES:
                obj_type = CODE_TYPES[code]
                oid = git_object_id(obj_type, entry['data'])
                _store(objects, oid, obj_type, entry['data'])
                resolved[id(entry)] = (oid, obj_type, entry['data'])
                progressed = True
                continue
            base = None
            if code == 6:
                base_entry = by_offset.get(entry['base_offset'])
                if base_entry is not None and id(base_entry) in resolved:
                    base = resolved[id(base_entry)]
            else:  # REF_DELTA
                wanted = entry['base_oid']
                for (oid, obj_type, obj_data) in resolved.values():
                    if oid == wanted:
                        base = (oid, obj_type, obj_data)
                        break
                if base is None and wanted in have_objects:
                    obj_type, obj_data = have_objects[wanted]
                    if git_object_id(obj_type, obj_data) != wanted:
                        raise ValueError('thin-pack base %s does not hash to itself'
                                         % wanted)
                    base = (wanted, obj_type, obj_data)
            if base is None:
                remaining.append(entry)
                continue
            _base_oid, base_type, base_data = base
            result = apply_delta(base_data, entry['data'])
            oid = git_object_id(base_type, result)
            _store(objects, oid, base_type, result)
            resolved[id(entry)] = (oid, base_type, result)
            progressed = True
        if not progressed:
            raise ValueError('pack delta base is missing or cyclic')
        pending = remaining
    if len(objects) != count:
        raise ValueError('pack resolved to %d objects, declared %d'
                         % (len(objects), count))
    return {'version': version, 'declared_count': count, 'objects': objects,
            'entries': entries, 'delta_count': delta_count,
            'trailer': trailer.hex(), 'checksum_ok': True}


def _store(objects, oid, obj_type, data):
    existing = objects.get(oid)
    if existing is not None and existing != (obj_type, data):
        raise ValueError('pack contains conflicting objects for %s' % oid)
    objects[oid] = (obj_type, data)


def _encode_object_header(obj_type, size):
    """Pack entry header varint: low 4 size bits + type in the first byte."""
    type_code = TYPE_CODES[obj_type]
    first = (type_code << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    if size:
        out.append(first | 0x80)
        while size:
            byte = size & 0x7F
            size >>= 7
            if size:
                out.append(byte | 0x80)
            else:
                out.append(byte)
    else:
        out.append(first)
    return bytes(out)


_TYPE_RANK = {'commit': 0, 'tree': 1, 'blob': 2, 'tag': 3}


def write_pack(objects):
    """Write a plain (delta-free) version-2 packfile.

    objects is a dict {oid: (type, bytes)} or an iterable of
    (oid, type, bytes). Order is deterministic: commit, tree, blob, tag,
    then oid ascending. Trailer is the SHA-1 of all preceding bytes.
    """
    if isinstance(objects, dict):
        items = [(oid, typ, data) for oid, (typ, data) in objects.items()]
    else:
        items = list(objects)
    items.sort(key=lambda item: (_TYPE_RANK.get(item[1], 9), item[0]))
    out = bytearray()
    out += b'PACK'
    out += struct.pack('>II', 2, len(items))
    for _oid, obj_type, data in items:
        out += _encode_object_header(obj_type, len(data))
        out += zlib.compress(data)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


# ---------------------------------------------------------------------------------------------
# reachability / closure
# ---------------------------------------------------------------------------------------------

def _tree_closure(objects, tree_oid, out):
    """Add a tree and everything under it (blobs, subtrees) to out.

    ``seen`` is separate from ``out``: callers often add the root tree to
    ``out`` before calling, and it must still be walked.
    """
    seen = set()
    stack = [tree_oid]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        entry = objects.get(oid)
        if entry is None or entry[0] != 'tree':
            continue
        out.add(oid)
        try:
            parsed = parse_tree(entry[1])
        except ValueError:
            continue
        for item in parsed:
            if item['mode'] == '160000':
                continue  # gitlink: a commit this repo does not hold
            if item['mode'] == '40000':
                stack.append(item['oid'])
            else:
                out.add(item['oid'])


def collect_reachable(objects, roots):
    """All objects reachable from roots (commits, tags, trees, blobs).

    Missing objects are skipped, never an error. Gitlinks (mode 160000)
    are not followed.
    """
    seen = set()
    stack = [oid for oid in roots if _is_oid(oid)]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        entry = objects.get(oid)
        if entry is None:
            continue
        seen.add(oid)
        obj_type, data = entry
        if obj_type == 'commit':
            try:
                parsed = parse_commit(data)
            except ValueError:
                continue
            if parsed['tree']:
                seen.add(parsed['tree'])
                _tree_closure(objects, parsed['tree'], seen)
            stack.extend(parsed['parents'])
        elif obj_type == 'tag':
            try:
                parsed = parse_tag(data)
            except ValueError:
                continue
            stack.append(parsed['object'])
        elif obj_type == 'tree':
            _tree_closure(objects, oid, seen)
    return seen


def commit_distances(objects, roots):
    """Distance (in parent hops) of every commit reachable from roots.

    Roots are at distance 0; a commit reached by several paths keeps the
    shortest distance. Missing or non-commit roots are ignored.
    """
    distances = {}
    queue = [(oid, 0) for oid in roots if _is_oid(oid)]
    head = 0
    while head < len(queue):
        oid, distance = queue[head]
        head += 1
        if oid in distances and distances[oid] <= distance:
            continue
        entry = objects.get(oid)
        if entry is None or entry[0] != 'commit':
            continue
        distances[oid] = distance
        try:
            parents = parse_commit(entry[1])['parents']
        except ValueError:
            continue
        for parent in parents:
            queue.append((parent, distance + 1))
    return distances


def collect_depth_limited(objects, want_oids, depth):
    """Depth-limited closure of the wanted objects.

    Returns (included_commits, boundary_commits, object_oids). With
    depth N, commits at distance < N from a want are included (distance 0
    is the want itself); the boundary is the included commits at distance
    N-1 -- the commits whose parents were cut off, i.e. the shallow
    boundary the client records (for depth 1 that is the tip itself).
    With depth None the full closure is returned and the boundary is empty.
    Tag wants are included and followed to their commit; non-commit,
    non-tag wants contribute just themselves.
    """
    included = {}
    queue = []  # (commit oid, distance)
    object_oids = set()
    for want in want_oids:
        if not _is_oid(want):
            continue
        entry = objects.get(want)
        if entry is None:
            continue
        obj_type, data = entry
        if obj_type == 'tag':
            object_oids.add(want)
            try:
                target = parse_tag(data)['object']
            except ValueError:
                continue
            target_entry = objects.get(target)
            if target_entry is not None and target_entry[0] == 'commit':
                queue.append((target, 0))
            elif target_entry is not None:
                object_oids.add(target)
        elif obj_type == 'commit':
            queue.append((want, 0))
        else:
            object_oids.add(want)
    head = 0
    while head < len(queue):
        oid, distance = queue[head]
        head += 1
        if oid in included and included[oid] <= distance:
            continue
        included[oid] = distance
        entry = objects.get(oid)
        if entry is None or entry[0] != 'commit':
            continue
        try:
            parsed = parse_commit(entry[1])
        except ValueError:
            continue
        object_oids.add(oid)
        if parsed['tree']:
            object_oids.add(parsed['tree'])
            _tree_closure(objects, parsed['tree'], object_oids)
        if depth is None or distance + 1 < depth:
            for parent in parsed['parents']:
                queue.append((parent, distance + 1))
    if depth is None:
        boundary = set()
    else:
        boundary = {oid for oid, dist in included.items() if dist == depth - 1}
    return set(included), boundary, object_oids


def collect_have_objects(objects, have_oids, shallow_oids):
    """Closure of the objects a fetching client already holds.

    Descent stops past the client's shallow boundary commits (those
    commits are included, their parents are not), matching what a shallow
    clone actually holds.
    """
    shallow_oids = set(shallow_oids or [])
    out = set()
    stack = list(have_oids)
    seen_commits = set()
    while stack:
        oid = stack.pop()
        if not _is_oid(oid):
            continue
        entry = objects.get(oid)
        if entry is None:
            continue
        obj_type, data = entry
        if obj_type == 'tag':
            if oid in out:
                continue
            out.add(oid)
            try:
                stack.append(parse_tag(data)['object'])
            except ValueError:
                continue
        elif obj_type == 'commit':
            if oid in seen_commits:
                continue
            seen_commits.add(oid)
            try:
                parsed = parse_commit(data)
            except ValueError:
                continue
            out.add(oid)
            if parsed['tree']:
                out.add(parsed['tree'])
                _tree_closure(objects, parsed['tree'], out)
            if oid not in shallow_oids:
                stack.extend(parsed['parents'])
        elif obj_type == 'tree':
            _tree_closure(objects, oid, out)
        else:
            out.add(oid)
    return out


def peel_to_commit(objects, oid):
    """Follow annotated tags to the object they point at; returns (oid, type)."""
    seen = set()
    current = oid
    while True:
        if current in seen:
            return current, None
        seen.add(current)
        entry = objects.get(current)
        if entry is None or entry[0] != 'tag':
            return current, (entry[0] if entry else None)
        try:
            current = parse_tag(entry[1])['object']
        except ValueError:
            return current, 'tag'
