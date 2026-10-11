# Scratchpad for Obsidian

A quick-capture scratchpad plugin for the [Omarchy](https://omarchy.org) shell
that sends notes straight into your Obsidian vault.

Press a keybinding (or click the bar icon), type a note, hit Enter — the note
lands as a timestamped bullet in a daily markdown file inside your Obsidian
vault. Obsidian picks it up instantly; no sync plugin or REST API needed.

![Scratchpad capture box](preview.png)

## Screenshots

| Setup wizard — vault detection | Setup wizard — subfolder | Setup wizard — confirmation |
|---|---|---|
| ![Vault list](screenshots/wizard-vaults.png) | ![Subfolder](screenshots/wizard-folder.png) | ![Confirm](screenshots/wizard-confirm.png) |

## Features

- **Fast capture** — SUPER+N opens a themed text box that matches your Omarchy
  theme; Enter saves, Esc cancels
- **Daily notes** — notes append as `- HH:MM — text` bullets to
  `<vault>/<folder>/YYYY-MM-DD.md`; the `# YYYY-MM-DD` heading is created
  automatically on the first note of the day
- **Bar widget** — pencil icon in the bar: left-click toggles the capture box,
  right-click reopens setup
- **Setup wizard** — first run walks you through choosing a vault and
  subfolder, with a confirmation screen showing the exact save path
- **Vault detection** — lists your registered Obsidian vaults automatically
  (from `~/.config/obsidian/obsidian.json`) plus a filesystem scan for
  `.obsidian` folders; native folder-picker dialog and manual path entry as
  fallbacks
- **Self-sizing UI** — the capture box grows with your text; the vault list
  grows with the number of vaults
- **Error recovery** — a failed write reopens the setup wizard instead of
  leaving you stuck

## Install

```
omarchy plugin add https://github.com/DuBearte/omarchy-scratchpad.git --enable
```

Then add the keybinding in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + N", "Scratchpad", "omarchy-shell shell toggle io.github.dubearte.scratchpad")
```

## Usage

| Action | How |
|---|---|
| Capture a note | SUPER+N, type, Enter |
| Cancel | Esc or click outside |
| Reconfigure (vault/folder) | Right-click the bar pencil icon |
| Capture from the bar | Left-click the bar pencil icon |

## Configuration

Settings live in `~/.config/omarchy/scratchpad-io.github.dubearte/settings.json`:

```json
{
  "vaultPath": "/home/you/Documents/Obsidian/MyVault",
  "folder": "Scratchpad"
}
```

- `vaultPath` — absolute path to the vault (or any target folder)
- `folder` — subfolder inside the vault where daily notes are written

Delete the file and use the bar icon's right-click (or SUPER+N) to re-run the
setup wizard.

## Requirements

- Omarchy Quattro (the shell's plugin support)
- An Obsidian vault anywhere on disk (the plugin only writes markdown files —
  Obsidian itself doesn't need to be running)

External commands and runtimes the plugin invokes (all ship with a stock
Omarchy install, so there is nothing extra to install):

| Command | Provided by | Used for |
|---|---|---|
| `hyprctl` | Hyprland | floating/centering the folder-picker window during setup |
| `qml6` | Qt6 (a Quickshell runtime dependency) | the optional native folder-picker dialog |
| `jq` | jq (ships with Omarchy) | finding the folder-picker window in `hyprctl clients` output |
| `python3` | python (a hard dependency of Omarchy's `uwsm` session manager) | `scripts/note-writer.py`, the writer that saves notes and settings, and the bounded vault scanner |

## Notes

- Notes are plain filesystem appends; if you use Syncthing or another sync
  tool on your vault, make sure its index is healthy — a corrupted sync index
  can interfere with externally-created files
- Settings live outside the plugin folder so saving them doesn't trigger a
  plugin hot-reload
- All file writes go through a descriptor-based helper
  (`scripts/note-writer.py`). The vault you choose is the trust boundary:
  directories above it may be symlinks (e.g. `/home` → `/var/home`, or a
  vault on another disk), but the vault itself and everything inside it is
  resolved without following symlinks, the target file is opened with
  `O_NOFOLLOW`, verified to be a regular file, and written through that
  descriptor. If the vault, its notes subfolder, or the daily note is (or
  becomes) a symlink, the write fails closed with a notification instead of
  following it. The same applies to the settings directory and file
  (`~/.config/omarchy` above them may be a symlink, e.g. from stow).
- The vault folder must already exist: a mistyped path fails and reopens
  setup instead of silently creating a new folder. The notes subfolder is
  created on the first save.
- Settings are only saved when you confirm the setup wizard, and are
  replaced atomically (temp file + rename).
- First-run vault discovery is bounded and self-contained in the same helper
  (`scan-vaults`): the Obsidian registry read is size-capped before parsing,
  the `.obsidian` scan never follows symlinks and is capped by visited
  directories, entries per directory, depth and result count, and the whole
  scan runs under a hard deadline with a small validated JSON result. Setup
  stays interactive even against a huge or hostile home directory.

## Development

Check your work the same way the shell does:

```
python3 tests/test_scan_vaults.py
omarchy plugin validate ~/.config/omarchy/plugins/io.github.dubearte.scratchpad
```

## Updating

```
omarchy plugin update io.github.dubearte.scratchpad
```

Updates are diff-previewed and require confirmation; local modifications are
never overwritten silently.

## Uninstalling

```
omarchy plugin remove io.github.dubearte.scratchpad
```

This deletes the plugin folder, unloads it from the shell, and removes its
entries from `shell.json`.

Two things are left behind (both harmless, both your call):

- **Keybinding** — the SUPER+N binding lives in
  `~/.config/hypr/bindings.lua`, which the plugin system never touches. To
  remove it, delete this line:

  ```lua
  o.bind("SUPER + N", "Scratchpad", "omarchy-shell shell toggle io.github.dubearte.scratchpad")
  ```

- **Saved settings** —
  `~/.config/omarchy/scratchpad-io.github.dubearte/settings.json`
  (your vault and folder choice). Delete the file (or the
  `scratchpad-io.github.dubearte` directory) if you don't want it
  remembered on a future reinstall.

Notes already written into your vault are regular markdown files — removing
the plugin never touches them.