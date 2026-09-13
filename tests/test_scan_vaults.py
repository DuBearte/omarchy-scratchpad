#!/usr/bin/env python3
"""Test suite for the bounded vault discovery (scan-vaults) in note-writer.py,
plus regression checks for the existing note / replace-settings modes.

Run:  python3 tests/test_scan_vaults.py
"""

import contextlib
import importlib.util
import io
import json
import os
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "scripts", "note-writer.py")

spec = importlib.util.spec_from_file_location("note_writer", HELPER)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load note-writer.py from " + HELPER)
nw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nw)

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append(name + (" — " + detail if detail else ""))


def run_helper(*args, env_extra=None):
    env = dict(os.environ)
    env.pop("SCRATCHPAD_NOTE", None)
    env.pop("SCRATCHPAD_PAYLOAD", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, HELPER] + list(args),
        capture_output=True, text=True, env=env, timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def make_registry(home, entries):
    cfg = os.path.join(home, ".config", "obsidian")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "obsidian.json"), "w") as fh:
        json.dump({"vaults": entries}, fh)


# ---------------------------------------------------------------- registry

def test_registry_basic():
    with tempfile.TemporaryDirectory() as home:
        vault = os.path.join(home, "Vault")
        os.makedirs(vault)
        make_registry(home, [{"ts": 1, "path": vault, "open": True}])
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("registry: finds registered vault", vault in payload["vaults"], out)
        check("registry: registryCount=1", payload["registryCount"] == 1, out)


def test_registry_garbage_and_oversize():
    with tempfile.TemporaryDirectory() as home:
        cfg = os.path.join(home, ".config", "obsidian")
        os.makedirs(cfg)
        # Oversize registry (2 MB of junk JSON) must not break or blow up.
        with open(os.path.join(cfg, "obsidian.json"), "w") as fh:
            fh.write('{"vaults": ["' + "A" * (2 * 1024 * 1024) + '"]}')
        code, out, _ = run_helper("scan-vaults", home)
        check("registry: oversize garbage does not crash", code == 0, out)
        payload = json.loads(out)
        check("registry: oversize garbage yields no vaults", payload["vaults"] == [], out)
        # Oversize but valid JSON: 300 tiny entries, all nonexistent paths.
        make_registry(home, [{"path": "/nonexistent/vault/%d" % i} for i in range(300)])
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("registry: 300 entries parsed but capped count reported",
              payload["registryCount"] == 200, out)
        check("registry: nonexistent entries dropped",
              payload["vaults"] == [], out)
        # Missing / malformed registry files
        os.remove(os.path.join(cfg, "obsidian.json"))
        code, out, _ = run_helper("scan-vaults", home)
        check("registry: missing registry is fine", code == 0 and json.loads(out)["registryCount"] == 0, out)
        with open(os.path.join(cfg, "obsidian.json"), "w") as fh:
            fh.write("{not json")
        code, out, _ = run_helper("scan-vaults", home)
        check("registry: malformed JSON is fine", code == 0, out)


def test_registry_filters():
    with tempfile.TemporaryDirectory() as home:
        vault = os.path.join(home, "Good")
        os.makedirs(vault)
        link = os.path.join(home, "Linked")
        os.makedirs(os.path.join(home, "RealTarget"))
        os.symlink(os.path.join(home, "RealTarget"), link)
        make_registry(home, [
            {"path": vault},
            {"path": link},                    # symlinked vault: dropped
            {"path": "/does/not/exist"},       # stale: dropped
            {"path": home + "/../etc"},        # '..': rejected at parse
            {"path": "relative/path"},         # not absolute: rejected
            {"path": 12345},                   # wrong type: rejected
            {},                                # no path: rejected
            {"path": vault},                   # duplicate: deduped
        ])
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("registry: only the real vault survives filters",
              payload["vaults"] == [vault], out)


# --------------------------------------------------------------- traversal

def test_scan_find_and_dedup():
    with tempfile.TemporaryDirectory() as home:
        v1 = os.path.join(home, "Docs", "VaultOne")
        v2 = os.path.join(home, "Work", "deep", "er", "VaultTwo")
        for path in (v1, v2):
            os.makedirs(os.path.join(path, ".obsidian"))
        # Depth-6 vault must NOT be found (MAX_SCAN_DEPTH == 5)
        v6 = os.path.join(home, "a", "b", "c", "d", "e", "f", "TooDeep")
        os.makedirs(os.path.join(v6, ".obsidian"))
        # Registry also lists VaultOne → dedup keeps one copy.
        make_registry(home, [{"path": v1}])
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("scan: finds shallow vault", v1 in payload["vaults"], out)
        check("scan: finds 4-level vault", v2 in payload["vaults"], out)
        check("scan: depth-6 vault excluded", v6 not in payload["vaults"], out)
        check("scan: dedup between registry and filesystem", payload["vaults"].count(v1) == 1, out)
        check("scan: no truncation on small tree", payload["truncated"] is False, out)


def test_symlinks_never_followed():
    with tempfile.TemporaryDirectory() as home:
        outside = tempfile.mkdtemp(prefix="outside-")
        target = os.path.join(outside, "TargetVault")
        os.makedirs(os.path.join(target, ".obsidian"))
        os.makedirs(os.path.join(home, "links"))
        os.symlink(target, os.path.join(home, "links", "vault-link"))
        # Symlink named as the marker itself
        os.makedirs(os.path.join(home, "markerlink-parent"))
        os.symlink(target, os.path.join(home, "markerlink-parent", ".obsidian"))
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("scan: symlinked vault dir not followed", all(
            p.startswith(home) for p in payload["vaults"]), out)
        check("scan: symlinked .obsidian marker not followed",
              target not in payload["vaults"], out)
        shutil.rmtree(outside)


def test_scan_skips_hidden_and_heavy():
    with tempfile.TemporaryDirectory() as home:
        # A vault under a dot directory is NOT found (dot dirs never traversed)
        hidden_vault = os.path.join(home, ".config", "vaultish", "HiddenVault")
        os.makedirs(os.path.join(hidden_vault, ".obsidian"))
        # A vault under node_modules is skipped by the skip-set
        nm_vault = os.path.join(home, "proj", "node_modules", "NMVault")
        os.makedirs(os.path.join(nm_vault, ".obsidian"))
        # A vault at depth 5 exactly IS found
        v5 = os.path.join(home, "1", "2", "3", "4", "VaultFive")
        os.makedirs(os.path.join(v5, ".obsidian"))
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("scan: vault under dot dir not found", hidden_vault not in payload["vaults"], out)
        check("scan: vault under node_modules not found", nm_vault not in payload["vaults"], out)
        check("scan: depth-5 vault found", v5 in payload["vaults"], out)


def test_caps_results_and_visited():
    with tempfile.TemporaryDirectory() as home:
        # 60 vaults: more than MAX_SCAN_RESULTS (50)
        for i in range(60):
            os.makedirs(os.path.join(home, "v%02d" % i, ".obsidian"))
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("caps: result count capped at 50", len(payload["vaults"]) <= 50, out)
        check("caps: truncation flagged", payload["truncated"] is True, out)

    # Visited-dir cap mechanism, exercised in-process with a lowered cap
    # (the production cap of 4000 is unreachable below depth 5 in a cheap
    # test tree).
    with tempfile.TemporaryDirectory() as home:
        for i in range(50):
            os.makedirs(os.path.join(home, "c%02d" % i, "d", "e", "f"))
        original = getattr(nw, "MAX_VISITED_DIRS")
        setattr(nw, "MAX_VISITED_DIRS", 10)
        try:
            found, truncated = nw.scan_home_for_vaults(home)
        finally:
            setattr(nw, "MAX_VISITED_DIRS", original)
        check("caps: visited-dir cap flags truncated", truncated is True, str(truncated))
        check("caps: visited-dir cap returns partial results", isinstance(found, list))

    # End-to-end with production caps: 800 chains of 5 nested levels ≈
    # 4800 real directories → the 4000 visited-dir cap fires.
    with tempfile.TemporaryDirectory() as home:
        for i in range(800):
            os.makedirs(os.path.join(home, "c%03d" % i, *(["d"] * 5)))
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("caps: production visited cap triggers end-to-end", payload["truncated"] is True, out)
        check("caps: scan still completes", code == 0, out)


def test_cap_entries_per_dir():
    with tempfile.TemporaryDirectory() as home:
        # One directory with 1000 subdirs, each containing a vault → only the
        # first MAX_ENTRIES_PER_DIR (400) are considered.
        for i in range(1000):
            os.makedirs(os.path.join(home, "dir%03d" % i, ".obsidian"))
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("caps: per-dir entries capped at 400", len(payload["vaults"]) <= 400, out)
        check("caps: per-dir truncation flagged", payload["truncated"] is True, out)


def test_deadline():
    # Deadline mechanism, in-process: 2000 chains ≈ 10000 real directories,
    # which cannot finish inside a 0.02s deadline.
    with tempfile.TemporaryDirectory() as home:
        for i in range(2000):
            os.makedirs(os.path.join(home, "w%04d" % i, "d", "e", "f", "g"))
        original = getattr(nw, "SCAN_DEADLINE_SECONDS")
        setattr(nw, "SCAN_DEADLINE_SECONDS", 0.02)
        try:
            started = time.monotonic()
            found, truncated = nw.scan_home_for_vaults(home)
            elapsed = time.monotonic() - started
        finally:
            setattr(nw, "SCAN_DEADLINE_SECONDS", original)
        check("deadline: short deadline flags truncated", truncated is True, str(truncated))
        check("deadline: short deadline stops quickly", elapsed < 2.0, "%.2fs" % elapsed)
        check("deadline: partial results still returned", isinstance(found, list))

    # End-to-end sanity: a wide, deep, vault-less tree completes fast.
    with tempfile.TemporaryDirectory() as home:
        for i in range(60):
            os.makedirs(os.path.join(home, "b%02d" % i, *(["x"] * 30)))
        started = time.monotonic()
        code, out, _ = run_helper("scan-vaults", home)
        elapsed = time.monotonic() - started
        check("deadline: scan completes", code == 0, out)
        check("deadline: well under process+scan budget", elapsed < 12.0, "%.1fs" % elapsed)


def test_home_edge_cases():
    code, out, _ = run_helper("scan-vaults", "/nonexistent-home-xyz")
    check("home: nonexistent home → empty result", code == 0 and json.loads(out)["vaults"] == [], out)
    link = tempfile.mkdtemp(prefix="homelink-") + "/home-link"
    os.symlink(tempfile.gettempdir(), link)
    code, out, _ = run_helper("scan-vaults", link)
    check("home: symlink home refused quietly (empty, not crash)",
          code == 0 and json.loads(out)["vaults"] == [], out)
    os.unlink(link)
    os.rmdir(os.path.dirname(link))


def test_output_shape_and_cap():
    with tempfile.TemporaryDirectory() as home:
        code, out, _ = run_helper("scan-vaults", home)
        payload = json.loads(out)
        check("shape: result keys exact",
              set(payload.keys()) == {"vaults", "registryCount", "truncated"}, out)
        check("shape: truncated is bool", isinstance(payload["truncated"], bool), out)
        check("shape: stdout is single line", out.strip().count("\n") == 0, repr(out))

    # Output cap end-to-end: 120 vaults × 100-byte names ≈ 13 KB of raw
    # paths plus JSON overhead → over the 16 KB cap, so it must shrink.
    with tempfile.TemporaryDirectory() as home:
        name_core = "N" * 95
        for i in range(120):
            os.makedirs(os.path.join(home, name_core + ("%03d" % i), ".obsidian"))
        code, out, _ = run_helper("scan-vaults", home)
        check("output: total stdout under 16 KB cap",
              len(out.encode()) <= nw.MAX_OUTPUT_BYTES + 1, str(len(out)))
        payload = json.loads(out)
        check("output: capped output flagged truncated", payload["truncated"] is True, out)

    # Guaranteed-cap mechanism: in-process with MAX_OUTPUT_BYTES lowered
    # below a two-vault result (stdout captured so the test stays silent).
    with tempfile.TemporaryDirectory() as home:
        v1 = os.path.join(home, "Alpha" + "x" * 40)
        v2 = os.path.join(home, "Beta" + "y" * 40)
        for vault in (v1, v2):
            os.makedirs(os.path.join(vault, ".obsidian"))
        original = getattr(nw, "MAX_OUTPUT_BYTES")
        setattr(nw, "MAX_OUTPUT_BYTES", 150)  # fits one vault, not two
        try:
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                nw.do_scan_vaults(home)
        finally:
            setattr(nw, "MAX_OUTPUT_BYTES", original)
        shrunk = json.loads(captured.getvalue())
        check("output: tiny cap shrinks result", shrunk["vaults"] == [v1], captured.getvalue())
        check("output: tiny cap flagged truncated", shrunk["truncated"] is True, captured.getvalue())
        check("output: tiny cap output actually under cap",
              len(captured.getvalue().encode()) <= 150, str(len(captured.getvalue())))


# ------------------------------------------------- writer regression checks

def test_note_write_still_works():
    with tempfile.TemporaryDirectory() as tmp:
        vault_dir = os.path.join(tmp, "vault", "Scratch")
        code, out, err = run_helper("note", vault_dir, "2026-09-13.md", "# 2026-09-13",
                                    env_extra={"SCRATCHPAD_NOTE": "- 10:00 — hello"})
        check("writer: note appends", code == 0, err)
        with open(os.path.join(vault_dir, "2026-09-13.md")) as fh:
            content = fh.read()
        check("writer: heading + note present", content == "# 2026-09-13\n- 10:00 — hello\n", repr(content))
        code, _, _ = run_helper("note", vault_dir, "2026-09-13.md", "# 2026-09-13",
                                env_extra={"SCRATCHPAD_NOTE": "- 10:01 — second"})
        with open(os.path.join(vault_dir, "2026-09-13.md")) as fh:
            content = fh.read()
        check("writer: second note, no dup heading", content.count("# 2026-09-13") == 1, content)


def test_settings_and_symlink_refusal():
    with tempfile.TemporaryDirectory() as tmp:
        settings = os.path.join(tmp, "settings.json")
        code, _, _ = run_helper("replace-settings", settings, env_extra={"SCRATCHPAD_PAYLOAD": '{"a": 1}'})
        with open(settings) as fh:
            check("writer: settings roundtrip", json.load(fh) == {"a": 1})
        os.remove(settings)
        # A symlink planted at the settings path must be refused and its
        # target left untouched (decoy lives inside the sandbox).
        decoy = os.path.join(tmp, "settings-decoy.json")
        with open(decoy, "w") as fh:
            fh.write("decoy-original")
        os.symlink(decoy, settings)
        code, _, err = run_helper("replace-settings", settings, env_extra={"SCRATCHPAD_PAYLOAD": '{"a": 2}'})
        check("writer: settings symlink refused exit 42", code == 42, str(code) + " " + err)
        with open(decoy) as fh:
            check("writer: symlink decoy untouched", fh.read() == "decoy-original")
        # Note into a symlinked vault dir is refused
        real = os.path.join(tmp, "real")
        os.makedirs(real)
        link = os.path.join(tmp, "link")
        os.symlink(real, link)
        code, _, _ = run_helper("note", link, "2026-09-13.md", "# d",
                                env_extra={"SCRATCHPAD_NOTE": "x"})
        check("writer: symlinked vault refused exit 42", code == 42, str(code))


def test_fd_limit_under_traversal():
    """The scan must not leak descriptors: run a wide scan with a hard
    RLIMIT_NOFILE of 64 — impossible if fds accumulate per directory."""
    with tempfile.TemporaryDirectory() as home:
        for i in range(300):
            os.makedirs(os.path.join(home, "f%03d" % i, "d", "e", "f", "g"))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
        try:
            found, truncated = nw.scan_home_for_vaults(home)
        except OSError as e:
            check("fd-limit: no descriptor exhaustion", False, str(e))
            return
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        check("fd-limit: wide scan completes under 64 fds", truncated is False, str(truncated))


def test_race_symlink_swap_during_scan():
    """Abuse test: a swapper repeatedly replaces a real directory with a
    symlink to a decoy vault tree while scans run back to back. Discovery
    must never return the decoy path (O_NOFOLLOW traversal)."""
    decoy_root = tempfile.mkdtemp(prefix="race-decoy-")
    decoy_vault = os.path.join(decoy_root, "DecoyVault")
    os.makedirs(os.path.join(decoy_vault, ".obsidian"))
    stop = {}

    def swapper():
        import threading
        link_path = os.path.join(home, "flipper")
        real_dir = os.path.join(home, "flipper-real")
        os.makedirs(real_dir, exist_ok=True)
        while not stop.get("done"):
            try:
                if os.path.islink(link_path):
                    os.unlink(link_path)
                    os.rename(real_dir, link_path)
                else:
                    os.rename(link_path, real_dir)
                    os.symlink(decoy_vault, link_path)
            except OSError:
                pass

    import threading
    with tempfile.TemporaryDirectory() as home:
        for i in range(50):
            os.makedirs(os.path.join(home, "v%02d" % i, ".obsidian"))
        thread = threading.Thread(target=swapper, daemon=True)
        thread.start()
        try:
            decoy_seen = False
            for _round in range(30):
                found, _truncated = nw.scan_home_for_vaults(home)
                if any(path == decoy_vault for path in found):
                    decoy_seen = True
        finally:
            stop["done"] = True
            thread.join(timeout=5)
        check("race: decoy vault never followed mid-scan", not decoy_seen,
              "decoy path appeared in results")
    shutil.rmtree(decoy_root, ignore_errors=True)


# -------------------------------------------------------------------- main

def main():
    tests = [
        test_registry_filters,
        test_registry_garbage_and_oversize,
        test_registry_filters,
        test_scan_find_and_dedup,
        test_symlinks_never_followed,
        test_scan_skips_hidden_and_heavy,
        test_caps_results_and_visited,
        test_cap_entries_per_dir,
        test_deadline,
        test_home_edge_cases,
        test_output_shape_and_cap,
        test_note_write_still_works,
        test_settings_and_symlink_refusal,
        test_fd_limit_under_traversal,
        test_race_symlink_swap_during_scan,
    ]
    for test in tests:
        test()
    print("PASS: %d" % len(PASS))
    for name in PASS:
        print("  ok: " + name)
    if FAIL:
        print("FAIL: %d" % len(FAIL))
        for name in FAIL:
            print("  FAIL: " + name)
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()