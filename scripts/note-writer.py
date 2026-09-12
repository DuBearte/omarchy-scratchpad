#!/usr/bin/env python3
"""Descriptor-based, symlink-safe writer for Scratchpad for Obsidian.

Two modes:

  note <vault-dir> <YYYY-MM-DD.md> <heading>
      Appends one note line (stdin/env SCRATCHPAD_NOTE) to the daily file
      inside <vault-dir>, creating the file and missing directories.
      Prepends the heading line when the file does not already contain it.

  replace-settings <settings.json>
      Replaces the plugin settings file with the JSON payload from the
      SCRATCHPAD_PAYLOAD environment variable (create or truncate).

Security model (requested in the marketplace review of this plugin):
  - The target directory is resolved component-wise with O_NOFOLLOW, so a
    symlink at ANY path component fails closed instead of being followed.
  - The daily note / settings file is opened relative to that verified
    directory descriptor with O_NOFOLLOW, so a symlink planted at the file
    path is never followed. The descriptor is then verified to be a regular
    file before anything is written.
  - Opening the parent directory returns a pinned descriptor: the kernel
    resolves subsequent openat() against that inode, so later path
    replacement cannot redirect the write.
  - The note text and settings payload arrive via the environment, never on
    the command line, so they do not show up in `ps` output.
  - Symlinked vaults and symlinked settings files are refused (exit 42).
"""

import errno
import os
import stat
import sys
from typing import NoReturn

EXIT_ERROR = 1
EXIT_SYMLINK = 42

MAX_HEADING_SCAN = 4 * 1024 * 1024  # bytes of an existing daily file to scan

NOTE_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-."
)


def fail(message: str, code: int = EXIT_ERROR) -> NoReturn:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def open_dir_component(parent_fd, name):
    """Open one path component without following symlinks, creating it when
    missing. Returns a directory descriptor pinned to the real inode."""
    try:
        return os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd
        )
    except OSError as e:
        # On Linux, O_NOFOLLOW|O_DIRECTORY on a symlink raises ELOOP when
        # O_DIRECTORY is checked first and ENOTDIR when O_NOFOLLOW is —
        # either way the component is a symlink (or not a real directory),
        # and both fail closed.
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            fail("symlink-detected: " + name, EXIT_SYMLINK)
        if e.errno != errno.ENOENT:
            fail("cannot open directory component %s: %s" % (name, e.strerror))
        # Missing component: create it, then re-open through the descriptor.
        try:
            os.mkdir(name, 0o777, dir_fd=parent_fd)
        except OSError as e2:
            if e2.errno == errno.EEXIST:
                # Created (or replaced) between our attempts: re-open and let
                # O_NOFOLLOW decide. A symlink here still fails closed.
                pass
            elif e2.errno == errno.ENOTDIR:
                fail("symlink-detected: " + name, EXIT_SYMLINK)
            else:
                fail("cannot create directory %s: %s" % (name, e2.strerror))
        try:
            return os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd
            )
        except OSError as e3:
            if e3.errno in (errno.ELOOP, errno.ENOTDIR):
                fail("symlink-detected: " + name, EXIT_SYMLINK)
            fail("cannot open directory component %s: %s" % (name, e3.strerror))


def resolve_dir_nofollow(path):
    """Walk <path> component-wise from the root, never following symlinks,
    and return a descriptor pinned to the final directory."""
    # Refuse raw '..' components before any normalization can collapse them.
    if any(component == ".." for component in path.split("/")):
        fail("path must not contain '..': " + path)
    path = os.path.abspath(path)
    if path == "/":
        return os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.split("/"):
            if component in ("", "."):
                continue
            fd, prev = open_dir_component(fd, component), fd
            os.close(prev)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_file_nofollow(dir_fd, name, mode):
    """Open <name> relative to <dir_fd> without following symlinks and verify
    it is a regular file. mode: 'append' or 'truncate'. O_RDWR so the
    heading check can pread() the existing content through the same pinned
    descriptor."""
    flags = os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT
    flags |= os.O_APPEND if mode == "append" else os.O_TRUNC
    try:
        fd = os.open(name, flags, 0o644, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            fail("symlink-detected: " + name, EXIT_SYMLINK)
        if e.errno == errno.EISDIR:
            fail("path is a directory, not a file: " + name)
        fail("cannot open %s: %s" % (name, e.strerror))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            fail("not a regular file: " + name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def do_note(vault_dir, filename, heading):
    if not vault_dir or not vault_dir.startswith("/"):
        fail("vault directory must be an absolute path")
    if (
        not filename
        or any(c not in NOTE_NAME_CHARS for c in filename)
        or filename.startswith(".")
    ):
        fail("invalid daily note file name: " + filename)
    note = os.environ.get("SCRATCHPAD_NOTE", "")
    if not note:
        fail("no note text provided")

    dir_fd = resolve_dir_nofollow(vault_dir)
    try:
        fd = open_file_nofollow(dir_fd, filename, "append")
        try:
            payload = b""
            # O_APPEND makes every write atomic-append at the file's current
            # end, so the heading check races at worst to a duplicated
            # heading on an existing file, never to a lost or overwritten
            # note.
            st = os.fstat(fd)
            if heading not in head_text(fd, st.st_size):
                payload += heading.encode("utf-8") + b"\n"
            payload += note.encode("utf-8", "surrogateescape") + b"\n"
            write_all(fd, payload)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def head_text(fd, size):
    """The first MAX_HEADING_SCAN bytes of fd, as text."""
    data = os.pread(fd, min(size, MAX_HEADING_SCAN), 0) if size > 0 else b""
    return data.decode("utf-8", "replace")


def do_replace_settings(path):
    if not path or not path.startswith("/"):
        fail("settings path must be absolute")
    payload = os.environ.get("SCRATCHPAD_PAYLOAD", "")
    if not payload:
        fail("no settings payload provided")

    directory, filename = path.rsplit("/", 1)
    dir_fd = resolve_dir_nofollow(directory)
    try:
        fd = open_file_nofollow(dir_fd, filename, "truncate")
        try:
            write_all(fd, payload.encode("utf-8", "surrogateescape"))
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def main(argv):
    if len(argv) < 2:
        fail("usage: note-writer.py note <dir> <file> <heading> | "
             "replace-settings <file>")
    if argv[1] == "note":
        if len(argv) != 5:
            fail("usage: note-writer.py note <dir> <file> <heading>")
        do_note(argv[2], argv[3], argv[4])
    elif argv[1] == "replace-settings":
        if len(argv) != 3:
            fail("usage: note-writer.py replace-settings <file>")
        do_replace_settings(argv[2])
    else:
        fail("unknown mode: " + argv[1])


if __name__ == "__main__":
    try:
        main(sys.argv)
    except SystemExit:
        raise
    except Exception as e:  # unexpected: log and fail closed
        sys.stderr.write("note-writer: %s\n" % e)
        sys.exit(EXIT_ERROR)