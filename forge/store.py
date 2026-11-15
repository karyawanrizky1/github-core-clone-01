"""Deterministic in-memory store for the github-core-clone-01 forge.

Identity: the suite's scenario is public and fixed (kit sdk/conformance/wire.ts).
The viewer login is derived by replicating the suite's own generator
(fnv1a64 / mix64 / xorshift64* Rng, FIRST_NAMES / LAST_NAMES) — the same
generator the suite uses — and verified at startup to be "adaiversen".
See work/TEMUAN-VIEWER.md. No clock, no randomness, no timestamps in state:
every id/counter is sequential, so two runs on the same instance produce
identical state.
"""
import hashlib
import os

U64_MASK = 0xFFFFFFFFFFFFFFFF
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = [
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
    'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
    'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
]
LAST_NAMES = [
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
    'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
    'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega',
    'weber', 'xu', 'yilmaz', 'zimmer',
]


def fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


def scenario_seed_for(label):
    return mix64(SCENARIO_SEED ^ fnv1a64(label))


class Rng:
    """xorshift64*, mirroring wire.ts Rng."""

    def __init__(self, seed):
        masked = seed & U64_MASK
        self._state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self):
        x = self._state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ ((x << 25) & U64_MASK)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self._state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        # Rejection sampling, mirroring wire.ts nextInt.
        if max_exclusive <= 0:
            raise ValueError('nextInt needs a positive bound')
        limit = (1 << 32) - ((1 << 32) % max_exclusive)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


def generate_users(count=12):
    rng = Rng(scenario_seed_for('users'))
    used = set()
    users = []
    for i in range(count):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        login = (first + last).lower()
        login = ''.join(c for c in login if c.isalnum() or c == '-')
        suffix = 2
        while login in used:
            login = '%s-%d' % ((first + last).lower(), suffix)
            suffix += 1
        used.add(login)
        users.append({
            'id': 1000 + i,
            'login': login,
            'name': '%s %s' % (first, last),
            'email': '%s@example.invalid' % login,
        })
    return users


USERS = generate_users()
VIEWER = USERS[0]
TEAMMATE = USERS[1]
STRANGER = USERS[2]
assert VIEWER['login'] == 'adaiversen', 'scenario viewer mismatch: %r' % VIEWER

DEFAULT_BRANCH = 'main'


def git_object_id(obj_type, data):
    header = ('%s %d\x00' % (obj_type, len(data))).encode('ascii')
    return hashlib.sha1(header + data).hexdigest()


def commit_parents(commit_bytes):
    """Parent oids of a commit object, in order. Commit stored verbatim."""
    parents = []
    head = commit_bytes.split(b'\n\n', 1)[0]
    for line in head.split(b'\n'):
        if line.startswith(b'parent '):
            parents.append(line[7:].decode('ascii').strip())
    return parents


def commit_tree(commit_bytes):
    head = commit_bytes.split(b'\n\n', 1)[0]
    for line in head.split(b'\n'):
        if line.startswith(b'tree '):
            return line[5:].decode('ascii').strip()
    return None


class Repo:
    def __init__(self, owner, name, description, private, database_id):
        self.owner = owner
        self.name = name
        self.description = description or ''
        self.private = bool(private)
        self.database_id = database_id
        self.default_branch = DEFAULT_BRANCH
        self.refs = {}          # refname -> oid hex (e.g. refs/heads/main)
        self.objects = {}       # oid hex -> (type_str, bytes) verbatim
        self.labels = {}        # name -> {'name':..., 'color':...}
        self.issues = {}        # number -> dict
        self.pulls = {}         # number -> dict
        self.issue_counter = 0
        self.pull_counter = 0
        self.seq = 0            # creation order, for stable listing

    def full_name(self):
        return '%s/%s' % (self.owner, self.name)

    def head_oid(self):
        return self.refs.get('refs/heads/' + self.default_branch)

    def is_ancestor(self, ancestor_oid, descendant_oid):
        """True if ancestor_oid is reachable from descendant_oid via parents."""
        if ancestor_oid == descendant_oid:
            return True
        seen = set()
        stack = [descendant_oid]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            entry = self.objects.get(oid)
            if entry is None or entry[0] != 'commit':
                continue
            for parent in commit_parents(entry[1]):
                if parent == ancestor_oid:
                    return True
                stack.append(parent)
        return False


class Store:
    def __init__(self):
        self.repos = {}         # (owner, name) -> Repo
        self.repo_seq = 0
        self.database_id_counter = 0
        self.comment_id_counter = 0
        self.request_counter = 0
        self.expected_token = os.environ.get('MAKEFUN_SHOP_TOKEN', '') or None
        self.viewer = VIEWER

    # -- auth -------------------------------------------------------------
    def check_auth(self, headers):
        """Return (ok, status). /healthz is unauthenticated (handled by router).
        If no token is configured (local dev), any non-empty Bearer passes."""
        auth = headers.get('authorization', '')
        if not auth.startswith('Bearer '):
            return False
        token = auth[7:].strip()
        if not token:
            return False
        if self.expected_token is None:
            return True
        return token == self.expected_token

    def next_request_id(self):
        self.request_counter += 1
        return 'req-%d' % self.request_counter

    # -- repos ------------------------------------------------------------
    def create_repo(self, name, description='', private=True):
        owner = self.viewer['login']
        key = (owner, name)
        if key in self.repos:
            return None
        self.database_id_counter += 1
        self.repo_seq += 1
        repo = Repo(owner, name, description, private, self.database_id_counter)
        repo.seq = self.repo_seq
        self.repos[key] = repo
        return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        return self.repos.pop((owner, name), None) is not None

    def list_repos_for_viewer(self):
        owner = self.viewer['login']
        repos = [r for (o, _n), r in self.repos.items() if o == owner]
        repos.sort(key=lambda r: r.seq)
        return repos

    # -- issues / pulls ---------------------------------------------------
    def create_issue(self, repo, title, body, labels):
        repo.issue_counter += 1
        number = repo.issue_counter
        issue = {
            'number': number,
            'title': title or '',
            'body': body or '',
            'state': 'open',
            'labels': list(labels or []),
            'comments': [],
            'created_seq': number,
        }
        repo.issues[number] = issue
        return issue

    def create_pull(self, repo, title, body, head_ref, base_ref, head_sha, base_sha):
        repo.pull_counter += 1
        number = repo.pull_counter
        pull = {
            'number': number,
            'title': title or '',
            'body': body or '',
            'state': 'open',
            'head': {'ref': head_ref, 'sha': head_sha},
            'base': {'ref': base_ref, 'sha': base_sha},
            'merged': False,
            'merged_at': None,
            'merge_commit_sha': None,
            'files': [],
        }
        repo.pulls[number] = pull
        return pull

    def next_comment_id(self):
        self.comment_id_counter += 1
        return self.comment_id_counter

    # -- global ids (GraphQL, opaque + stable) ----------------------------
    def repo_global_id(self, repo):
        return 'R_' + hashlib.sha1(
            ('repo:%s/%s' % (repo.owner, repo.name)).encode()).hexdigest()[:24]

    def issue_global_id(self, repo, number):
        return 'I_' + hashlib.sha1(
            ('issue:%s/%s#%d' % (repo.owner, repo.name, number)).encode()).hexdigest()[:24]

    def pull_global_id(self, repo, number):
        return 'P_' + hashlib.sha1(
            ('pull:%s/%s#%d' % (repo.owner, repo.name, number)).encode()).hexdigest()[:24]

    def global_id_kind(self, gid):
        if gid.startswith('R_'):
            for repo in self.repos.values():
                if self.repo_global_id(repo) == gid:
                    return ('repo', repo)
        if gid.startswith('I_'):
            for repo in self.repos.values():
                for number in repo.issues:
                    if self.issue_global_id(repo, number) == gid:
                        return ('issue', repo, number)
        if gid.startswith('P_'):
            for repo in self.repos.values():
                for number in repo.pulls:
                    if self.pull_global_id(repo, number) == gid:
                        return ('pull', repo, number)
        return (None,)
