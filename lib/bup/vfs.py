"""Virtual File System representing bup's repository contents.

The vfs.py library makes it possible to expose contents from bup's repository
and abstracts internal name mangling and storage from the exposition layer.
"""

import os, re, stat, time

from bup import git, metadata
from helpers import debug1, debug2
from bup.git import BUP_NORMAL, BUP_CHUNKED, cp
from bup.hashsplit import GIT_MODE_TREE, GIT_MODE_FILE

EMPTY_SHA='\0'*20


class NodeError(Exception):
    """VFS base exception."""
    pass

class NoSuchFile(NodeError):
    """Request of a file that does not exist."""
    pass

class NotDir(NodeError):
    """Attempt to do a directory action on a file that is not one."""
    pass

class NotFile(NodeError):
    """Access to a node that does not represent a file."""
    pass

class TooManySymlinks(NodeError):
    """Symlink dereferencing level is too deep."""
    pass


def _treeget(hash, repo_dir=None):
    it = cp(repo_dir).get(hash.encode('hex'))
    _, type, _ = next(it)
    assert(type == 'tree')
    return git.tree_decode(''.join(it))


def _tree_decode(hash, repo_dir=None):
    tree = [(int(name,16),stat.S_ISDIR(mode),sha)
            for (mode,name,sha)
            in _treeget(hash, repo_dir)]
    assert(tree == list(sorted(tree)))
    return tree


def _chunk_len(hash, repo_dir=None):
    return sum(len(b) for b in cp(repo_dir).join(hash.encode('hex')))


def _last_chunk_info(hash, repo_dir=None):
    tree = _tree_decode(hash, repo_dir)
    assert(tree)
    (ofs,isdir,sha) = tree[-1]
    if isdir:
        (subofs, sublen) = _last_chunk_info(sha, repo_dir)
        return (ofs+subofs, sublen)
    else:
        return (ofs, _chunk_len(sha))


def _total_size(hash, repo_dir=None):
    (lastofs, lastsize) = _last_chunk_info(hash, repo_dir)
    return lastofs + lastsize


def _chunkiter(hash, startofs, repo_dir=None):
    assert(startofs >= 0)
    tree = _tree_decode(hash, repo_dir)

    # skip elements before startofs
    for i in xrange(len(tree)):
        if i+1 >= len(tree) or tree[i+1][0] > startofs:
            break
    first = i

    # iterate through what's left
    for i in xrange(first, len(tree)):
        (ofs,isdir,sha) = tree[i]
        skipmore = startofs-ofs
        if skipmore < 0:
            skipmore = 0
        if isdir:
            for b in _chunkiter(sha, skipmore, repo_dir):
                yield b
        else:
            yield ''.join(cp(repo_dir).join(sha.encode('hex')))[skipmore:]


class _ChunkReader:
    def __init__(self, hash, isdir, startofs, repo_dir=None):
        if isdir:
            self.it = _chunkiter(hash, startofs, repo_dir)
            self.blob = None
        else:
            self.it = None
            self.blob = ''.join(cp(repo_dir).join(hash.encode('hex')))[startofs:]
        self.ofs = startofs

    def next(self, size):
        out = ''
        while len(out) < size:
            if self.it and not self.blob:
                try:
                    self.blob = next(self.it)
                except StopIteration:
                    self.it = None
            if self.blob:
                want = size - len(out)
                out += self.blob[:want]
                self.blob = self.blob[want:]
            if not self.it:
                break
        debug2('next(%d) returned %d\n' % (size, len(out)))
        self.ofs += len(out)
        return out


class _FileReader(object):
    def __init__(self, hash, size, isdir, repo_dir=None):
        self.hash = hash
        self.ofs = 0
        self.size = size
        self.isdir = isdir
        self.reader = None
        self._repo_dir = repo_dir

    def seek(self, ofs):
        if ofs > self.size:
            self.ofs = self.size
        elif ofs < 0:
            self.ofs = 0
        else:
            self.ofs = ofs

    def tell(self):
        return self.ofs

    def read(self, count = -1):
        if count < 0:
            count = self.size - self.ofs
        if not self.reader or self.reader.ofs != self.ofs:
            self.reader = _ChunkReader(self.hash, self.isdir, self.ofs,
                                       self._repo_dir)
        try:
            buf = self.reader.next(count)
        except:
            self.reader = None
            raise  # our offsets will be all screwed up otherwise
        self.ofs += len(buf)
        return buf

    def close(self):
        pass


class Node(object):
    """Base class for file representation."""
    def __init__(self, parent, name, mode, hash, repo_dir=None):
        self.parent = parent
        self.name = name
        self.mode = mode
        self.hash = hash
        self.ctime = self.mtime = self.atime = 0
        self._repo_dir = repo_dir
        self._subs = None
        self._metadata = None

    def __repr__(self):
        return "<%s object at %s - name:%r hash:%s parent:%r>" \
            % (self.__class__, hex(id(self)),
               self.name, self.hash.encode('hex'),
               self.parent.name if self.parent else None)

    def __cmp__(a, b):
        if a is b:
            return 0
        return (cmp(a and a.parent, b and b.parent) or
                cmp(a and a.name, b and b.name))

    def __iter__(self):
        return iter(self.subs())

    def fullname(self, stop_at=None):
        """Get this file's full path."""
        assert(self != stop_at)  # would be the empty string; too weird
        if self.parent and self.parent != stop_at:
            return os.path.join(self.parent.fullname(stop_at=stop_at),
                                self.name)
        else:
            return self.name

    def _mksubs(self):
        self._subs = {}

    def subs(self):
        """Get a list of nodes that are contained in this node."""
        if self._subs == None:
            self._mksubs()
        return sorted(self._subs.values())

    def sub(self, name):
        """Get node named 'name' that is contained in this node."""
        if self._subs == None:
            self._mksubs()
        ret = self._subs.get(name)
        if not ret:
            raise NoSuchFile("no file %r in %r" % (name, self.name))
        return ret

    def top(self):
        """Return the very top node of the tree."""
        if self.parent:
            return self.parent.top()
        else:
            return self

    def fs_top(self):
        """Return the top node of the particular backup set.

        If this node isn't inside a backup set, return the root level.
        """
        if self.parent and not isinstance(self.parent, CommitList):
            return self.parent.fs_top()
        else:
            return self

    def _lresolve(self, parts):
        #debug2('_lresolve %r in %r\n' % (parts, self.name))
        if not parts:
            return self
        (first, rest) = (parts[0], parts[1:])
        if first == '.':
            return self._lresolve(rest)
        elif first == '..':
            if not self.parent:
                raise NoSuchFile("no parent dir for %r" % self.name)
            return self.parent._lresolve(rest)
        elif rest:
            return self.sub(first)._lresolve(rest)
        else:
            return self.sub(first)

    def lresolve(self, path, stay_inside_fs=False):
        """Walk into a given sub-path of this node.

        If the last element is a symlink, leave it as a symlink, don't resolve
        it.  (like lstat())
        """
        start = self
        if not path:
            return start
        if path.startswith('/'):
            if stay_inside_fs:
                start = self.fs_top()
            else:
                start = self.top()
            path = path[1:]
        parts = re.split(r'/+', path or '.')
        if not parts[-1]:
            parts[-1] = '.'
        #debug2('parts: %r %r\n' % (path, parts))
        return start._lresolve(parts)

    def resolve(self, path = ''):
        """Like lresolve(), and dereference it if it was a symlink."""
        return self.lresolve(path).lresolve('.')

    def try_resolve(self, path = ''):
        """Like resolve(), but don't worry if a symlink uses an invalid path.

        Returns an error if any intermediate nodes were invalid.
        """
        n = self.lresolve(path)
        try:
            n = n.lresolve('.')
        except NoSuchFile:
            pass
        return n

    def nlinks(self):
        """Get the number of hard links to the current node."""
        return 1

    def size(self):
        """Get the size of the current node."""
        return 0

    def open(self):
        """Open the current node. It is an error to open a non-file node."""
        raise NotFile('%s is not a regular file' % self.name)

    def _populate_metadata(self, force=False):
        # Only Dirs contain .bupm files, so by default, do nothing.
        pass

    def metadata(self):
        """Return this Node's Metadata() object, if any."""
        if not self._metadata and self.parent:
            self.parent._populate_metadata(force=True)
        return self._metadata

    def release(self):
        """Release resources that can be automatically restored (at a cost)."""
        self._metadata = None
        self._subs = None


class File(Node):
    """A normal file from bup's repository."""
    def __init__(self, parent, name, mode, hash, bupmode, repo_dir=None):
        Node.__init__(self, parent, name, mode, hash, repo_dir)
        self.bupmode = bupmode
        self._cached_size = None
        self._filereader = None

    def open(self):
        """Open the file."""
        # You'd think FUSE might call this only once each time a file is
        # opened, but no; it's really more of a refcount, and it's called
        # once per read().  Thus, it's important to cache the filereader
        # object here so we're not constantly re-seeking.
        if not self._filereader:
            self._filereader = _FileReader(self.hash, self.size(),
                                           self.bupmode == git.BUP_CHUNKED,
                                           repo_dir = self._repo_dir)
        self._filereader.seek(0)
        return self._filereader

    def size(self):
        """Get this file's size."""
        if self._cached_size == None:
            debug1('<<<<File.size() is calculating (for %r)...\n' % self.name)
            if self.bupmode == git.BUP_CHUNKED:
                self._cached_size = _total_size(self.hash,
                                                repo_dir = self._repo_dir)
            else:
                self._cached_size = _chunk_len(self.hash,
                                               repo_dir = self._repo_dir)
            debug1('<<<<File.size() done.\n')
        return self._cached_size


_symrefs = 0
class Symlink(File):
    """A symbolic link from bup's repository."""
    def __init__(self, parent, name, hash, bupmode, repo_dir=None):
        File.__init__(self, parent, name, 0o120000, hash, bupmode,
                      repo_dir = repo_dir)

    def size(self):
        """Get the file size of the file at which this link points."""
        return len(self.readlink())

    def readlink(self):
        """Get the path that this link points at."""
        return ''.join(cp(self._repo_dir).join(self.hash.encode('hex')))

    def dereference(self):
        """Get the node that this link points at.

        If the path is invalid, raise a NoSuchFile exception. If the level of
        indirection of symlinks is 100 levels deep, raise a TooManySymlinks
        exception.
        """
        global _symrefs
        if _symrefs > 100:
            raise TooManySymlinks('too many levels of symlinks: %r'
                                  % self.fullname())
        _symrefs += 1
        try:
            try:
                return self.parent.lresolve(self.readlink(),
                                            stay_inside_fs=True)
            except NoSuchFile:
                raise NoSuchFile("%s: broken symlink to %r"
                                 % (self.fullname(), self.readlink()))
        finally:
            _symrefs -= 1

    def _lresolve(self, parts):
        return self.dereference()._lresolve(parts)


class FakeSymlink(Symlink):
    """A symlink that is not stored in the bup repository."""
    def __init__(self, parent, name, toname, repo_dir=None):
        Symlink.__init__(self, parent, name, EMPTY_SHA, git.BUP_NORMAL,
                         repo_dir = repo_dir)
        self.toname = toname

    def readlink(self):
        """Get the path that this link points at."""
        return self.toname


class Dir(Node):
    """A directory stored inside of bup's repository."""

    def __init__(self, *args, **kwargs):
        Node.__init__(self, *args, **kwargs)
        self._bupm = None

    def _populate_metadata(self, force=False):
        if self._metadata and not force:
            return
        if not self._subs:
            self._mksubs()
        if not self._bupm:
            return
        meta_stream = self._bupm.open()
        dir_meta = metadata.Metadata.read(meta_stream)
        for sub in self:
            if not stat.S_ISDIR(sub.mode):
                sub._metadata = metadata.Metadata.read(meta_stream)
        self._metadata = dir_meta

    def _mksubs(self):
        self._subs = {}
        it = cp(self._repo_dir).get(self.hash.encode('hex'))
        _, type, _ = next(it)
        if type == 'commit':
            del it
            it = cp(self._repo_dir).get(self.hash.encode('hex') + ':')
            _, type, _ = next(it)
        assert(type == 'tree')
        for (mode,mangled_name,sha) in git.tree_decode(''.join(it)):
            if mangled_name == '.bupm':
                bupmode = stat.S_ISDIR(mode) and BUP_CHUNKED or BUP_NORMAL
                self._bupm = File(self, mangled_name, GIT_MODE_FILE, sha,
                                  bupmode)
                continue
            name, bupmode = git.demangle_name(mangled_name, mode)
            if bupmode == git.BUP_CHUNKED:
                mode = GIT_MODE_FILE
            if stat.S_ISDIR(mode):
                self._subs[name] = Dir(self, name, mode, sha, self._repo_dir)
            elif stat.S_ISLNK(mode):
                self._subs[name] = Symlink(self, name, sha, bupmode,
                                           self._repo_dir)
            else:
                self._subs[name] = File(self, name, mode, sha, bupmode,
                                        self._repo_dir)

    def metadata(self):
        """Return this Dir's Metadata() object, if any."""
        self._populate_metadata()
        return self._metadata

    def metadata_file(self):
        """Return this Dir's .bupm File, if any."""
        if not self._subs:
            self._mksubs()
        return self._bupm

    def release(self):
        """Release restorable resources held by this node."""
        self._bupm = None
        super(Dir, self).release()


class CommitDir(Node):
    """A directory that contains all commits that are reachable by a ref.

    Contains a set of subdirectories named after the commits' first byte in
    hexadecimal. Each of those directories contain all commits with hashes that
    start the same as the directory name. The name used for those
    subdirectories is the hash of the commit without the first byte. This
    separation helps us avoid having too much directories on the same level as
    the number of commits grows big.
    """
    def __init__(self, parent, name, repo_dir=None):
        Node.__init__(self, parent, name, GIT_MODE_TREE, EMPTY_SHA, repo_dir)

    def _mksubs(self):
        self._subs = {}
        refs = git.list_refs(repo_dir = self._repo_dir)
        for ref in refs:
            #debug2('ref name: %s\n' % ref[0])
            revs = git.rev_list(ref[1].encode('hex'),
                                format='%at',
                                parse=lambda f: int(f.readline().strip()),
                                repo_dir=self._repo_dir)
            for commithex, date in revs:
                #debug2('commit: %s  date: %s\n' % (commit.encode('hex'), date))
                commit = commithex.decode('hex')
                containername = commithex[:2]
                dirname = commithex[2:]
                n1 = self._subs.get(containername)
                if not n1:
                    n1 = CommitList(self, containername, self._repo_dir)
                    self._subs[containername] = n1

                if n1.commits.get(dirname):
                    # Stop work for this ref, the rest should already be present
                    break

                n1.commits[dirname] = (commit, date)


class CommitList(Node):
    """A list of commits with hashes that start with the current node's name."""
    def __init__(self, parent, name, repo_dir=None):
        Node.__init__(self, parent, name, GIT_MODE_TREE, EMPTY_SHA, repo_dir)
        self.commits = {}

    def _mksubs(self):
        self._subs = {}
        for (name, (hash, date)) in self.commits.items():
            n1 = Dir(self, name, GIT_MODE_TREE, hash, self._repo_dir)
            n1.ctime = n1.mtime = date
            self._subs[name] = n1


class TagDir(Node):
    """A directory that contains all tags in the repository."""
    def __init__(self, parent, name, repo_dir = None):
        Node.__init__(self, parent, name, GIT_MODE_TREE, EMPTY_SHA, repo_dir)

    def _mksubs(self):
        self._subs = {}
        for (name, sha) in git.list_refs(repo_dir = self._repo_dir):
            if name.startswith('refs/tags/'):
                name = name[10:]
                date = git.get_commit_dates([sha.encode('hex')],
                                            repo_dir=self._repo_dir)[0]
                commithex = sha.encode('hex')
                target = '../.commit/%s/%s' % (commithex[:2], commithex[2:])
                tag1 = FakeSymlink(self, name, target, self._repo_dir)
                tag1.ctime = tag1.mtime = date
                self._subs[name] = tag1


class BranchList(Node):
    """A list of links to commits reachable by a branch in bup's repository.

    Represents each commit as a symlink that points to the commit directory in
    /.commit/??/ . The symlink is named after the commit date.
    """
    def __init__(self, parent, name, hash, repo_dir=None):
        Node.__init__(self, parent, name, GIT_MODE_TREE, hash, repo_dir)

    def _mksubs(self):

        self._subs = {}

        revs = list(git.rev_list(self.hash.encode('hex'),
                                 format='%at',
                                 parse=lambda f: int(f.readline().strip()),
                                 repo_dir=self._repo_dir))
        latest = revs[0]
        for commithex, date in revs:
            l = time.localtime(date)
            ls = time.strftime('%Y-%m-%d-%H%M%S', l)
            target = '../.commit/%s/%s' % (commithex[:2], commithex[2:])
            n1 = FakeSymlink(self, ls, target, self._repo_dir)
            n1.ctime = n1.mtime = date
            self._subs[ls] = n1

        commithex, date = latest
        target = '../.commit/%s/%s' % (commithex[:2], commithex[2:])
        n1 = FakeSymlink(self, 'latest', target, self._repo_dir)
        n1.ctime = n1.mtime = date
        self._subs['latest'] = n1


class RefList(Node):
    """A list of branches in bup's repository.

    The sub-nodes of the ref list are a series of CommitList for each commit
    hash pointed to by a branch.

    Also, a special sub-node named '.commit' contains all commit directories
    that are reachable via a ref (e.g. a branch).  See CommitDir for details.
    """
    def __init__(self, parent, repo_dir=None):
        Node.__init__(self, parent, '/', GIT_MODE_TREE, EMPTY_SHA, repo_dir)

    def _mksubs(self):
        self._subs = {}

        commit_dir = CommitDir(self, '.commit', self._repo_dir)
        self._subs['.commit'] = commit_dir

        tag_dir = TagDir(self, '.tag', self._repo_dir)
        self._subs['.tag'] = tag_dir

        refs_info = [(name[11:], sha) for (name,sha)
                     in git.list_refs(limit_to_heads=True,
                                      repo_dir=self._repo_dir)]
        dates = git.get_commit_dates([sha.encode('hex')
                                      for (name, sha) in refs_info],
                                     repo_dir=self._repo_dir)
        for (name, sha), date in zip(refs_info, dates):
            n1 = BranchList(self, name, sha, self._repo_dir)
            n1.ctime = n1.mtime = date
            self._subs[name] = n1
