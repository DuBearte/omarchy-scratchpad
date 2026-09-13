#!/usr/bin/env python3
"""Descriptor-based, symlink-safe writer and bounded vault discovery for
Scratchpad for Obsidian.

Three modes:

  note <vault-dir> <YYYY-MM-DD.md> <heading>
      Appends one note line (env SCRATCHPAD_NOTE) to the daily file
      inside <vault-dir>, creating the file and missing directories.
      Prepends the heading line when the file does not already contain it.

  replace-settings <settings.json>
      Replaces the plugin settings file with the JSON payload from the
      SCRATCHPAD_PAYLOAD environment variable (create or truncate).

  scan-vaults <home>
      Bounded vault discovery: reads the Obsidian registry (size-capped)
      and scans <home> for `.obsidian` directories without following
      symlinks, all under hard caps and a deadline; prints a small
      validated JSON result ({"vaults": [...], "registryCount": N,
      "truncated": bool}).

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
  - Discovery is bounded in every phase: registry read (bytes), traversal
    (visited dirs, entries per dir, depth), result (count, path bytes) and
    the serialized output itself — under a hard self-deadline, with no
    subprocess tree to clean up because there is none.
"""

import errno
import json
import os
import stat
import sys
import time
from typing import NoReturn

EXIT_ERROR = 1
EXIT_SYMLINK = 42

MAX_HEADING_SCAN = 4 * 1024 * 1024  # bytes of an existing daily file to scan

# ---- bounded vault discovery (scan-vaults) --------------------------------
# Every phase is capped so a large or adversarial home directory or vault
# registry can never stall or exhaust the caller: the helper reads a bounded
# amount of the registry, visits a bounded number of real directories, and
# returns a bounded, validated result within a hard deadline.

MAX_REGISTRY_BYTES = 256 * 1024      # bytes of obsidian.json read before parsing
MAX_REGISTRY_ENTRIES = 200           # vault paths parsed from the registry
MAX_VISITED_DIRS = 4000              # real directories entered during the scan
MAX_ENTRIES_PER_DIR = 400            # entries read from any single directory
MAX_SCAN_DEPTH = 5                   # matches the previous find -maxdepth 5
MAX_SCAN_RESULTS = 50                # vault paths returned
MAX_OUTPUT_BYTES = 16 * 1024         # serialized result ceiling
SCAN_DEADLINE_SECONDS = 10.0         # hard self-deadline for the whole scan

# Directories that are never worth traversing during vault discovery. Dot
# directories are skipped by rule; this set adds the known heavyweights so a
# huge .cache/node_modules tree costs nothing even if named oddly.
SCAN_SKIP_DIRS = frozenset({
    ".cache", ".npm", ".cargo", ".rustup", ".nvm", ".local", ".thumbnails",
    ".venv", "node_modules", "__pycache__", "target", "dist", "build",
})

VAULT_MARKER = ".obsidian"
MAX_PATH_BYTES = 4096

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


# ---------------------------------------------------------------- bounded
# vault discovery: no bash/jq/find subprocess tree — the helper does the
# whole scan in-process, every phase capped, under a hard deadline, and
# returns a small validated result set (see constants above).


def read_registry(path):
    """Read the Obsidian vault registry capped at MAX_REGISTRY_BYTES and
    return its vault paths (deduplicated, capped at MAX_REGISTRY_ENTRIES,
    absolute, path-shaped, '..'-free). Anything malformed is skipped, not an
    error — discovery is best-effort by design."""
    candidates = []
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_REGISTRY_BYTES + 1)
    except OSError:
        return candidates
    if len(data) > MAX_REGISTRY_BYTES:
        data = data[:MAX_REGISTRY_BYTES]
    try:
        parsed = json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return candidates
    if not isinstance(parsed, dict):
        return candidates
    vaults = parsed.get("vaults")
    if not isinstance(vaults, list):
        return candidates
    for entry in vaults[:MAX_REGISTRY_ENTRIES]:
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("path")
        add_registry_candidate(candidate, candidates)
    return candidates


def add_registry_candidate(candidate, candidates):
    if not isinstance(candidate, str):
        return
    candidate = candidate.strip()
    if not candidate or not candidate.startswith("/"):
        return
    if len(candidate.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
        return
    if any(part == ".." for part in candidate.split("/")):
        return
    if candidate not in candidates:
        candidates.append(candidate)


def scan_home_for_vaults(home):
    """Bounded, symlink-safe depth-first scan for <VAULT_MARKER> directories.

    Never follows symlinks (walks with dir_fd and O_NOFOLLOW directory
    descriptors), caps visited directories, per-directory entries, depth,
    results and output size, and stops by the hard deadline. Returns
    (vault_paths, truncated_flag)."""
    try:
        root_fd = os.open(
            home, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        )
    except OSError:
        return [], False  # no home to scan (or HOME itself a symlink)
    deadline = time.monotonic() + SCAN_DEADLINE_SECONDS
    results = []
    truncated = False
    # Stack of open directory descriptors with their depth. Every fd is
    # closed exactly once: popped fds right after their entry loop, and
    # never-popped ones in the finally block.
    stack = [(root_fd, 0)]
    visited = 0
    try:
        while stack:
            if (
                len(results) >= MAX_SCAN_RESULTS
                or time.monotonic() >= deadline
                or visited >= MAX_VISITED_DIRS
            ):
                truncated = True
                break
            dir_fd, depth = stack.pop()
            visited += 1
            try:
                entries = []
                with os.scandir(dir_fd) as listing:
                    for index, entry in enumerate(listing):
                        if index >= MAX_ENTRIES_PER_DIR:
                            truncated = True
                            break
                        if entry.name in (".", ".."):
                            continue
                        entries.append(entry)
                found_here = False
                for entry in entries:
                    if (
                        not found_here
                        and entry.name == VAULT_MARKER
                        and entry.is_dir(follow_symlinks=False)
                    ):
                        # A .obsidian directory is a marker, never traversed.
                        vault_path = path_under(
                            dir_fd, os.path.dirname(entry.name) or "."
                        )
                        if vault_path and vault_path not in results:
                            results.append(vault_path)
                        found_here = True
                        continue
                    if entry.name.startswith("."):
                        continue  # dot directories are never traversed
                    if entry.name in SCAN_SKIP_DIRS:
                        continue
                    if depth >= MAX_SCAN_DEPTH:
                        # Deepest level is still checked as a marker (above)
                        # but never descended into.
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue  # symlinks and files are never entered
                    try:
                        child_fd = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
                            dir_fd=dir_fd,
                        )
                    except OSError:
                        continue  # vanished mid-scan, or a symlink: skip
                    stack.append((child_fd, depth + 1))
            except OSError:
                continue  # unreadable directory: skip, keep scanning
            finally:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
    finally:
        for fd, _depth in stack:
            try:
                os.close(fd)
            except OSError:
                pass
    return results, truncated


def path_under(dir_fd, name):
    """Absolute path of <name> relative to the directory descriptor dir_fd,
    or None when it cannot be determined. Uses /proc/self/fd, which on Linux
    resolves a descriptor to its real (symlink-free) location."""
    try:
        target = os.path.join("/proc/self/fd/%d" % dir_fd, name)
        real = os.path.realpath(target, strict=True)
        if not real.startswith("/") or len(real.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
            return None
        return real
    except (OSError, ValueError):
        return None


def is_real_dir(path):
    """True when <path> exists and is a real directory whose final component
    is not a symlink. Parents are allowed to contain symlinks (matching the
    old bash `[[ -d $p ]]` filter, and the writer only fails on symlinks at
    or inside the vault itself)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    parent, name = os.path.split(os.path.abspath(path))
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return False
    try:
        os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd)
        return True
    except OSError:
        # ELOOP here means the final component is a symlink; every other
        # error simply means it is not a usable real directory right now.
        return False
    finally:
        os.close(parent_fd)


def do_scan_vaults(home):
    if not home or not home.startswith("/"):
        fail("home must be an absolute path")
    registry_path = os.path.join(home, ".config/obsidian/obsidian.json")
    ordered = read_registry(registry_path)
    registry_count = len(ordered)
    seen = set(ordered)
    fs_found, truncated = scan_home_for_vaults(home)
    for found in fs_found:
        if found not in seen:
            seen.add(found)
            ordered.append(found)
    # Registry candidates were validated for shape only; drop entries that
    # no longer exist and entries whose final component is a symlink (the
    # writer refuses symlinked vaults, so listing them would only set the
    # user up for a refused write). Filesystem-scan results are real by
    # construction.
    ordered = [path for path in ordered if is_real_dir(path)]
    if len(ordered) > MAX_SCAN_RESULTS:
        ordered = ordered[:MAX_SCAN_RESULTS]
    for candidate in ordered:
        if not candidate.startswith("/") or ".." in candidate.split("/"):
            fail("invalid scan result: " + candidate)  # defense in depth
    payload = {
        "vaults": ordered,
        "registryCount": registry_count,
        "truncated": truncated,
    }
    encoded = json.dumps(payload, ensure_ascii=True)
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        # Hard-cap the serialized result: progressively shrink the result
        # set until it fits; the empty-vaults form always fits.
        for keep in (4, 2, 1, 0):
            encoded = json.dumps({
                "vaults": ordered[:keep],
                "registryCount": registry_count,
                "truncated": True,
            }, ensure_ascii=True)
            if len(encoded.encode("utf-8")) <= MAX_OUTPUT_BYTES:
                break
    sys.stdout.write(encoded + "\n")


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
             "replace-settings <file> | scan-vaults <home>")
    if argv[1] == "note":
        if len(argv) != 5:
            fail("usage: note-writer.py note <dir> <file> <heading>")
        do_note(argv[2], argv[3], argv[4])
    elif argv[1] == "replace-settings":
        if len(argv) != 3:
            fail("usage: note-writer.py replace-settings <file>")
        do_replace_settings(argv[2])
    elif argv[1] == "scan-vaults":
        if len(argv) != 3:
            fail("usage: note-writer.py scan-vaults <home>")
        do_scan_vaults(argv[2])
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