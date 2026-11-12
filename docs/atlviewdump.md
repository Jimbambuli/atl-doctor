# ATLViewDump: view trees under ATL

`atl-doctor` compares the View tree of an app under ATL with the tree `uiautomator dump` gives on
Android. ATL has no built-in way to dump its View tree yet. Until it has, the tree comes from a small
hook, *ATLViewDump*, kept as a patch in this repository:
`contrib/atl/0001-ATLViewDump-View-tree-dump-in-uiautomator-form.patch` (GPL-3.0-or-later like ATL).
It is made against ATL `master` at `c65b272326` (October 2026); apply it with `git am` in the ATL
source tree and rebuild ATL.

Without the hook everything else works; `atl-doctor check` lists the probe `viewdump` as missing and
reports have no view tree section.

## Detection

`atl-doctor` looks for the class `android.atl.ATLViewDump` in the ATL jars of the prefix
(`api-impl.jar`). If it is there, `record` sets `ATL_VIEWDUMP=<capture>/trees` for ATL.
Behind `--launcher` (e.g. a Flatpak with its own `/app`) the jars are out of sight: `record` always sets
`ATL_VIEWDUMP` and finds out at run time whether ATL answers. The launcher must pass the variable on and
let ATL write to the capture directory.

## Protocol

1. With `ATL_VIEWDUMP=DIR` set, the app's main thread checks every 250 ms for the file `DIR/request`.
2. `atl-doctor` writes the wanted name (one line, e.g. `01-Import`) to `DIR/request`, atomically
   (temporary file + rename).
3. The hook writes `DIR/<name>.xml` (via `<name>.xml.tmp` and rename) and deletes `DIR/request`.
4. `atl-doctor` waits up to 10 s for the request file to disappear and the XML to exist; otherwise it
   prints one line on stderr and goes on without a tree for that mark.

`record` asks for one tree per phase mark: `trees/NN-<mark name>.xml`.

The request file name is defined once (`atl_doctor.sources.tree.VIEWDUMP_REQUEST`), so a different
name chosen upstream is a one-line change.

## Format

The file has the form of `uiautomator dump`, so both sides are read by the same code:

```xml
<hierarchy rotation="0" atl="1">
  <window atl-what="activity org.example.Main">
    <node index="0" text="" resource-id="android:id/content" class="android.widget.FrameLayout"
          content-desc="" clickable="false" enabled="true" bounds="[10,20][550,820]"
          atl-class="android.widget.FrameLayout" atl-visibility="visible" atl-alpha="1.0"
          atl-shown="true" atl-gtk-rect="true" atl-jbounds="[10,20][550,820]"> ... </node>
  </window>
</hierarchy>
```

- `hierarchy atl="1"` marks an ATL dump; one `window` per activity, dialog or other window-manager
  view (`atl-what`: `activity|dialog|wm <class>`).
- `class` is the nearest framework class (what TalkBack/uiautomator report), `atl-class` the app's own
  class.
- `bounds` is the GTK widget rectangle in window pixels; `atl-gtk-rect="false"` if the widget is not
  mapped. `atl-jbounds` is the Java layout rectangle in the same coordinates.
- `atl-visibility` (`visible|invisible|gone`), `atl-alpha`, `atl-shown` (`View.isShown()`),
  `atl-hidden-by` (first ancestor that is not visible) and `atl-animation` (a pending animation)
  explain why a node is not visible.

A node counts as visible when it is shown, its total alpha is above 0.05, its GTK widget is mapped
and its area is non-zero after clipping by all ancestors - the same rule uiautomator applies on
Android.

## Upstream

The hook is meant to become a merge request to ATL. Whether ATL offers it as an environment variable
(as here) or as a D-Bus call is up to that merge request.

### Format accepted upstream will be supported

Whatever format and trigger ATL accepts upstream, `atl-doctor` will read it; the format of the patch
above stays supported as long as it is in use.
