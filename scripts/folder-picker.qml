import QtCore
import QtQuick
import QtQuick.Dialogs

Window {
  id: root

  width: 875
  height: 600
  visible: true
  opacity: 0
  flags: Qt.Dialog

  property url startUri: StandardPaths.writableLocation(StandardPaths.DocumentsLocation)

  Component.onCompleted: {
    if (root.startUri.toString() !== "")
      folderDialog.currentFolder = root.startUri
    folderDialog.open()
  }

  FolderDialog {
    id: folderDialog
    title: "Choose your Obsidian vault folder"
    acceptLabel: "Choose"
    onAccepted: {
      console.log("SCRATCHPAD_FOLDER=" + String(selectedFolder))
      Qt.quit()
    }
    onRejected: Qt.quit()
  }
}