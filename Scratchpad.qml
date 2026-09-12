import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import qs.Commons
import qs.Ui

Item {
  id: root

  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  property var shell: null
  property var manifest: null

  property bool opened: false

  // Setup flow: "capture" | "vault" | "folder" | "confirm"
  property string step: "capture"

  // Configured location. vaultPath is absolute; folder is a single subfolder name.
  property string vaultPath: ""
  property string folder: "Scratchpad"
  readonly property bool configured: vaultPath !== "" && folder !== ""
  readonly property string vaultDir: vaultPath + "/" + folder

  property string fontFamily: Style.font.menuFamily
  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color border: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", border, Math.max(1, Style.space(2)))
  property color scrim: Color.menu.scrim
  readonly property int cornerRadius: Style.cornerRadius
  property int contentMargin: Style.spacing.panelPadding
  // Capture card grows with the note text; other steps grow with their content.
  readonly property int inputHeight: Math.max(
    Style.space(80),
    Math.ceil(input.paintedHeight || Style.font.heading * 1.45)) + Style.space(4)
  readonly property string placeholderText: "Scratchpad note..."

  function log(msg) {
    console.log("scratchpad:", msg)
  }

  // ------------------------------------------------------------------ state

  // Plugin settings location. Lives outside the plugin folder so saving it
  // doesn't trigger a plugin hot-reload.
  readonly property string settingsPath:
    Quickshell.env("HOME") + "/.config/omarchy/scratchpad-io.github.dubearte/settings.json"

  FileView {
    id: settingsFile
    path: root.settingsPath
    watchChanges: false
    printErrors: true
    onLoaded: root.applySettings()
    onLoadFailed: function(error) {
      root.log("no settings.json (" + error + ") — setup wizard will run")
    }
  }

  function applySettings() {
    try {
      var text = settingsFile.text() || ""
      if (text.trim()) {
        var parsed = JSON.parse(text)
        if (parsed && typeof parsed.vaultPath === "string" && parsed.vaultPath.trim())
          root.vaultPath = parsed.vaultPath.trim()
        if (parsed && typeof parsed.folder === "string" && parsed.folder.trim())
          root.folder = parsed.folder.trim()
      }
      root.log("settings loaded: vault=" + root.vaultPath + " folder=" + root.folder)
    } catch (e) {
      root.log("failed to parse settings: " + e)
    }
  }

  function saveSettings() {
    var payload = JSON.stringify({ vaultPath: root.vaultPath, folder: root.folder }, null, 2) + "\n"
    // Fail closed if the settings path (or any of its parents) became a
    // symlink since it was loaded: refuse to write through it.
    settingsGuard.command = [helperPath, "replace-settings", root.settingsPath]
    settingsGuard.environment = { "SCRATCHPAD_PAYLOAD": payload }
    settingsGuard.stdinEnabled = false
    settingsGuard.running = true
    root.log("settings saved: vault=" + root.vaultPath + " folder=" + root.folder)
  }

  function expandPath(p) {
    p = String(p || "").trim()
    if (p === "~") return home
    if (p.indexOf("~/") === 0) return home + p.slice(1)
    return p
  }

  // If the picked path points at a note file (ends in .md), treat its parent
  // as the vault. Always strips trailing slashes.
  function normalizeVaultPath(p) {
    p = String(p || "").replace(/\/+$/, "")
    if (p.toLowerCase().endsWith(".md")) {
      var idx = p.lastIndexOf("/")
      if (idx > 0) p = p.slice(0, idx)
    }
    return p
  }

  // ------------------------------------------------------------------- flow

  function open(payloadJson) {
    var payload = {}
    try { payload = JSON.parse(payloadJson || "{}") } catch (e) { payload = ({}) }
    root.opened = true
    root.vaultEntryMode = false
    if (payload && payload.reconfigure === true) {
      root.log("reconfigure requested — forcing setup wizard")
      root.step = "vault"
    } else {
      root.step = root.configured ? "capture" : "vault"
    }
    if (root.step === "vault") {
      root.startVaultScan()
      Qt.callLater(function() { vaultList.forceActiveFocus() })
    } else {
      Qt.callLater(function() {
        input.clear()
        input.forceActiveFocus()
      })
    }
    root.log("open: step=" + root.step)
  }

  function close() {
    root.opened = false
  }

  function dismiss() {
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "io.github.dubearte.scratchpad")
  }

  function toggle() {
    if (root.opened) root.dismiss()
    else root.open("{}")
  }

  function back() {
    if (root.step === "folder") {
      root.step = "vault"
      Qt.callLater(function() { vaultList.forceActiveFocus() })
    } else if (root.step === "confirm") {
      root.step = "folder"
      Qt.callLater(function() {
        setupInput.text = root.folder
        setupInput.forceActiveFocus()
      })
    }
  }

  // ------------------------------------------------------------ vault list

  property var vaults: []
  property int vaultIndex: 0
  property bool vaultEntryMode: false

  function vaultName(path) {
    var parts = String(path || "").split("/").filter(function(p) { return p !== "" })
    return parts.length ? parts[parts.length - 1] : path
  }

  function startVaultScan() {
    vaultScanner.command = ["bash", "-c",
      "reg=$1; [[ -f $reg ]] && jq -r '.vaults[].path' \"$reg\" 2>/dev/null | while IFS= read -r p; do [[ -d \"$p\" ]] && printf '%s\\n' \"$p\"; done; find \"${2:-$HOME}\" -maxdepth 5 -type d -name .obsidian 2>/dev/null | sed 's|/.obsidian$||'",
      "bash", Quickshell.env("HOME") + "/.config/obsidian/obsidian.json"]
    vaultScanner.running = true
    root.log("scanning for vaults")
  }

  Process {
    id: vaultScanner
    running: false

    stdout: StdioCollector {
      id: vaultScanStdout
      waitForEnd: true
    }

    onExited: function(exitCode) {
      var seen = {}
      var out = []
      var lines = String(vaultScanStdout.text || "").split("\n")
      for (var i = 0; i < lines.length; i++) {
        var p = lines[i].trim()
        if (!p || seen[p]) continue
        seen[p] = true
        out.push(p)
      }
      root.vaults = out
      root.vaultIndex = 0
      root.log("vault scan: " + out.length + " vault(s) found")
    }
  }

  function pickVault(path) {
    root.vaultPath = normalizeVaultPath(path)
    root.log("vault selected: " + root.vaultPath)
    saveSettings()
    root.vaultEntryMode = false
    root.step = "folder"
    Qt.callLater(function() { setupInput.clear(); setupInput.forceActiveFocus() })
  }

  function submit() {
    if (root.step === "vault") {
      var expanded = expandPath(setupInput.text)
      if (!expanded || expanded === home || expanded.indexOf("/") !== 0) {
        Quickshell.execDetached([root.omarchyPath + "/bin/omarchy-notification-send", "Scratchpad", "Enter an absolute vault path (or leave blank to cancel)"])
        return
      }
      root.vaultPath = normalizeVaultPath(expanded)
      root.log("vault path set: " + root.vaultPath)
      saveSettings()
      root.vaultEntryMode = false
      root.step = "folder"
      Qt.callLater(function() { setupInput.clear(); setupInput.forceActiveFocus() })
      return
    }

    if (root.step === "folder") {
      var name = setupInput.text.trim().replace(/\//g, "-").replace(/\.md$/i, "").replace(/\.+$/, "").trim()
      if (!name) {
        root.dismiss()
        return
      }
      root.folder = name
      root.log("folder set: " + root.folder + " — showing confirm")
      root.step = "confirm"
      Qt.callLater(function() { confirmField.forceActiveFocus() })
      return
    }

    if (root.step === "confirm") {
      saveSettings()
      root.log("setup complete — switching to capture")
      root.step = "capture"
      Qt.callLater(function() { input.clear(); input.forceActiveFocus() })
      return
    }

    capture()
  }

  // Descriptor-based, symlink-safe writer helper. Resolves the vault and
  // settings paths component-wise without following symlinks, opens the
  // target file relative to the verified directory (O_NOFOLLOW), verifies it
  // is a regular file, and writes through that descriptor. Fail closed.
  readonly property string helperPath: pluginDir + "/scripts/note-writer.py"

  function capture() {
    var text = String(input.text || "").trim()
    if (!text) {
      root.dismiss()
      return
    }

    var now = new Date()
    var pad = function(n) { return (n < 10 ? "0" : "") + n }
    var datestamp = now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate())
    var timestamp = pad(now.getHours()) + ":" + pad(now.getMinutes())
    var heading = "# " + datestamp

    root.log("saving note to " + root.vaultDir + "/" + datestamp + ".md")
    noteWriter.command = [helperPath, "note", root.vaultDir, datestamp + ".md", heading]
    noteWriter.environment = { "SCRATCHPAD_NOTE": "- " + timestamp + " — " + text.replace(/\n/g, " ") }
    noteWriter.stdinEnabled = false
    noteWriter.running = true
    root.pendingDismiss = true
  }

  property bool pendingDismiss: false

  Process {
    id: settingsGuard
    running: false

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") root.log("settings writer stderr: " + text.trim())
    }

    onExited: function(exitCode) {
      if (exitCode === 42) {
        root.log("settings path is a symlink — refusing to save")
        Quickshell.execDetached([root.omarchyPath + "/bin/omarchy-notification-send", "Scratchpad", "Settings path looks unsafe (symlink) — not saved"])
      } else if (exitCode !== 0) {
        root.log("settings write failed (exit " + exitCode + ")")
      }
    }
  }

  Process {
    id: noteWriter
    running: false

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") root.log("note writer stderr: " + text.trim())
    }

    onExited: function(exitCode) {
      root.log("note write exited code=" + exitCode)
      if (exitCode === 42) {
        // Symlink (or path replacement) detected at the target — refuse the
        // write and drop the note rather than follow it.
        root.pendingDismiss = false
        root.log("symlink detected at note path — refusing to write")
        Quickshell.execDetached([root.omarchyPath + "/bin/omarchy-notification-send", "Scratchpad", "Note not saved — target path looks unsafe (symlink)"])
        return
      }
      if (exitCode !== 0) {
        root.pendingDismiss = false
        root.log("note write failed (exit " + exitCode + ") — reopening setup")
        Quickshell.execDetached([root.omarchyPath + "/bin/omarchy-notification-send", "Scratchpad", "Failed to save note \u2014 setup wizard will reopen to reconfigure"])
        root.vaultPath = ""
        root.folder = "Scratchpad"
        root.step = "vault"
        Qt.callLater(function() {
          setupInput.clear()
          setupInput.forceActiveFocus()
        })
        return
      }
      root.dismiss()
    }
  }

  readonly property string pluginDir: {
    var url = Qt.resolvedUrl("Scratchpad.qml")
    return url.toString().replace(/^file:\/\//, "").replace(/\/[^/]*$/, "")
  }

  property string pickerOutput: ""

  function browseForVault() {
    if (folderPickerProcess.running) return
    pickerOutput = ""
    folderPickerProcess.command = ["bash", "-lc",
      'exec "$@"', "bash",
      pluginDir + "/scripts/folder-picker.sh"]
    folderPickerProcess.running = true
    root.log("browse: launching folder picker")
  }

  function uriToPath(uri) {
    var path = String(uri || "").replace(/^file:\/\//, "")
    try { path = decodeURIComponent(path) } catch (e) {}
    return path
  }

  Process {
    id: folderPickerProcess
    running: false

    stdout: StdioCollector {
      id: pickerStdout
      waitForEnd: true
      onStreamFinished: root.pickerOutput = text
    }

    onExited: function(exitCode) {
      var selected = String(root.pickerOutput || pickerStdout.text || "").trim()
      root.log("picker exited code=" + exitCode + " output=" + selected)
      if (exitCode === 0 && selected) {
        root.vaultPath = normalizeVaultPath(uriToPath(selected))
        root.log("vault path picked: " + root.vaultPath)
        saveSettings()
        root.step = "folder"
        Qt.callLater(function() { setupInput.clear(); setupInput.forceActiveFocus() })
      } else if (exitCode !== 0) {
        Quickshell.execDetached([root.omarchyPath + "/bin/omarchy-notification-send", "Scratchpad", "Folder chooser failed \u2014 enter the path manually"])
      }
    }
  }

  // --------------------------------------------------------------------- ui

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-scratchpad"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    Item {
      id: windowArea
      anchors.fill: parent

      readonly property int contentHeight: contentColumn.implicitHeight

      BorderSurface {
        id: card
        width: Math.min(Style.space(720), Math.max(Style.space(300), windowArea.width - Style.gapsOut * 2))
        height: Math.min(
          Math.max(Style.space(220), windowArea.contentHeight + root.contentMargin * 2 + Style.space(8)),
          Math.max(Style.space(220), windowArea.height - Style.gapsOut * 2))
        radius: root.cornerRadius
        anchors.centerIn: parent
        color: root.background
        borderSpec: root.borderSpec
        padding: root.contentMargin

      MouseArea {
        anchors.fill: parent
        onClicked: {
          var field = root.step === "capture" ? input : setupInput
          if (root.step === "vault" || root.step === "folder")
            field.forceActiveFocus()
        }
      }

      Item {
        anchors.fill: parent

        Column {
          id: contentColumn
          anchors.horizontalCenter: parent.horizontalCenter
          anchors.verticalCenter: parent.verticalCenter
          width: card.width - root.contentMargin * 2
          spacing: Style.space(10)

          Text {
            textFormat: Text.PlainText
            text: root.step === "capture" ? "Scratchpad" : "Scratchpad setup"
            color: root.foreground
            opacity: 0.7
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.letterSpacing: 1
          }

          Text {
            visible: root.step !== "capture"
            width: parent.width
            textFormat: Text.PlainText
            text: root.step === "vault" && !root.vaultEntryMode
              ? "Choose your Obsidian vault"
              : root.step === "vault"
              ? "Path to your Obsidian vault folder (e.g. ~/Documents/Obsidian/Home)"
              : root.step === "folder"
              ? "Subfolder for scratchpad notes (daily notes land here)"
              : "Notes will be saved to this file:"
            color: root.foreground
            opacity: 0.85
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.Wrap
          }

          // Resolved path shown on the confirm step
          Text {
            visible: root.step === "confirm"
            width: parent.width
            textFormat: Text.PlainText
            text: root.vaultDir + "/" + new Date().toISOString().slice(0, 10) + ".md"
            color: Color.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WrapAnywhere
          }

          // Vault picker list: click or arrow-key + Enter
          Column {
            id: vaultList
            visible: root.step === "vault" && !root.vaultEntryMode
            width: parent.width
            spacing: Style.space(4)
            focus: true
            Keys.priority: Keys.BeforeItem
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_Up) {
                if (root.vaultIndex > 0) root.vaultIndex--
                event.accepted = true
              } else if (event.key === Qt.Key_Down) {
                if (root.vaultIndex < root.vaults.length - 1) root.vaultIndex++
                event.accepted = true
              } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                if (root.vaults.length) root.pickVault(root.vaults[root.vaultIndex])
                event.accepted = true
              } else if (event.key === Qt.Key_Backspace || event.key === Qt.Key_Tab) {
                root.vaultEntryMode = true
                Qt.callLater(function() { setupInput.forceActiveFocus() })
                event.accepted = true
              }
            }

            Repeater {
              model: root.vaults

              delegate: Item {
                required property var modelData
                required property int index
                width: vaultList.width
                height: Math.max(vaultRow.implicitHeight, Style.font.body * 1.6) + Style.space(14)

                Rectangle {
                  anchors.fill: parent
                  radius: root.cornerRadius
                  color: root.vaultIndex === index ? Style.selectedFill : "transparent"
                  visible: root.vaultIndex === index
                }

                MouseArea {
                  id: vaultRowMouse
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  hoverEnabled: true
                  onClicked: root.pickVault(modelData)
                  onEntered: root.vaultIndex = index
                }

                Row {
                  id: vaultRow
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.leftMargin: Style.space(8)
                  anchors.rightMargin: Style.space(8)
                  spacing: Style.space(8)

                  Text {
                    textFormat: Text.PlainText
                    text: "\uf02d"
                    color: root.vaultIndex === index ? Color.accent : root.foreground
                    opacity: root.vaultIndex === index ? 1 : 0.7
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    anchors.verticalCenter: parent.verticalCenter
                  }

                  Text {
                    id: vaultName
                    textFormat: Text.PlainText
                    text: root.vaultName(modelData)
                    color: root.vaultIndex === index ? Color.accent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    anchors.verticalCenter: parent.verticalCenter
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: modelData
                    color: root.foreground
                    opacity: 0.4
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideMiddle
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - vaultName.width - Style.space(30)
                  }
                }
              }
            }

            Text {
              width: parent.width
              visible: root.vaults.length === 0 && !vaultScanner.running
              textFormat: Text.PlainText
              text: "No vaults found \u2014 press Backspace to type a path, or Browse"
              color: root.foreground
              opacity: 0.5
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Text {
              width: parent.width
              visible: vaultScanner.running
              textFormat: Text.PlainText
              text: "Searching for vaults..."
              color: root.foreground
              opacity: 0.5
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Row {
              spacing: Style.space(12)

              Text {
                textFormat: Text.PlainText
                text: "\u2191\u2193 navigate \u00B7 \u21B5 choose \u00B7 Backspace to type a path \u00B7 Esc to cancel"
                color: root.foreground
                opacity: 0.4
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              Text {
                textFormat: Text.PlainText
                text: folderPickerProcess.running ? "\uf6ff Browsing..." : ""
                color: root.foreground
                opacity: 0.6
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          TextInput {
            id: setupInput
            visible: root.step === "folder" || (root.step === "vault" && root.vaultEntryMode)
            width: parent.width
            focus: true
            activeFocusOnTab: true
            color: root.foreground
            selectionColor: Color.accent
            selectedTextColor: root.background
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            clip: true
            Keys.priority: Keys.BeforeItem
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                root.submit()
                event.accepted = true
              }
            }

            Text {
              visible: setupInput.text === "" && root.step === "folder"
              text: "Scratchpad"
              color: root.foreground
              opacity: 0.4
              font: setupInput.font
              anchors.left: parent.left
              anchors.leftMargin: 2
            }

            Text {
              visible: setupInput.text === "" && root.step === "vault"
              text: "~/Documents/Obsidian/MyVault"
              color: root.foreground
              opacity: 0.4
              font: setupInput.font
              anchors.left: parent.left
              anchors.leftMargin: 2
            }
          }

          // Confirm step: Enter to accept, Backspace to go back
          Item {
            id: confirmField
            visible: root.step === "confirm"
            width: parent.width
            height: confirmRow.height
            focus: true
            Keys.priority: Keys.BeforeItem
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                root.submit()
                event.accepted = true
              } else if (event.key === Qt.Key_Backspace || event.key === Qt.Key_Left) {
                root.back()
                event.accepted = true
              }
            }

            Row {
              id: confirmRow
              anchors.left: parent.left
              spacing: Style.space(14)

              Text {
                textFormat: Text.PlainText
                text: "\u21B5 Enter to confirm"
                color: root.foreground
                opacity: 0.7
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                textFormat: Text.PlainText
                text: "\u232B Backspace to change"
                color: root.foreground
                opacity: 0.7
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          Item {
            visible: root.step === "vault"
            width: parent.width
            height: browseButton.height

            BorderSurface {
              id: browseButton
              radius: root.cornerRadius
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              color: root.background
              borderSpec: root.borderSpec
              padding: Style.space(8)
              implicitWidth: browseRow.implicitWidth + browsePadding * 2
              implicitHeight: browseRow.implicitHeight + browsePadding

              property int browsePadding: Style.space(6)

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.browseForVault()
              }

              Row {
                id: browseRow
                anchors.centerIn: parent
                spacing: Style.space(6)

                Text {
                  textFormat: Text.PlainText
                  text: "\uf07c"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  anchors.verticalCenter: parent.verticalCenter
                }

                Text {
                  textFormat: Text.PlainText
                  text: folderPickerProcess.running ? "Browsing..." : "Browse folders"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  anchors.verticalCenter: parent.verticalCenter
                }
              }
            }
          }

          TextInput {
            id: input
            visible: root.step === "capture"
            width: parent.width
            height: root.inputHeight
            focus: true
            activeFocusOnTab: true
            color: root.foreground
            selectionColor: Color.accent
            selectedTextColor: root.background
            font.family: root.fontFamily
            font.pixelSize: Style.font.heading
            wrapMode: TextInput.Wrap
            clip: true
            Keys.priority: Keys.BeforeItem
            Keys.onPressed: function(event) {
              if (event.key === Qt.Key_Escape) {
                root.dismiss()
                event.accepted = true
              } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                root.submit()
                event.accepted = true
              }
            }

            Text {
              visible: input.text === "" && !input.activeFocus
              text: root.placeholderText
              color: root.foreground
              opacity: 0.58
              font: input.font
              anchors.left: parent.left
              anchors.leftMargin: 2
            }
          }
        }

        Item {
          anchors.left: contentColumn.left
          anchors.right: contentColumn.right
          height: footerText.visible ? footerText.height : 0

          Text {
            id: footerText
            anchors.right: parent.right
            textFormat: Text.PlainText
            text: root.step === "vault" && !root.vaultEntryMode ? ""
              : root.step === "vault" ? "Enter to continue \u00B7 Esc to cancel"
              : root.step === "folder" ? "Enter to review \u00B7 Esc to cancel"
              : root.step === "confirm" ? ""
              : "Enter to save \u00B7 Esc to cancel"
            color: root.foreground
            opacity: 0.4
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            visible: text !== ""
          }
        }
      }
    }
    }
  }
}