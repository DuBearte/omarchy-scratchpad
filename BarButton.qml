import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "jk.scratchpad"

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function toggleOverlay() {
    if (root.bar) root.bar.run("omarchy-shell shell toggle jk.scratchpad")
  }

  function reconfigure() {
    if (root.bar) root.bar.run("omarchy-shell shell summon jk.scratchpad '{\"reconfigure\":true}'")
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "\uf044"
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    tooltipText: "Scratchpad"
    onPressed: function(btn) {
      if (btn === Qt.RightButton) root.reconfigure()
      else root.toggleOverlay()
    }
  }
}