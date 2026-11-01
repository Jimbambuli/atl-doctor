# atl-doctor

Diagnostics for [Android Translation Layer (ATL)](https://gitlab.com/android_translation_layer/android_translation_layer):
*why doesn't this app run (correctly) under ATL, and what should be fixed first?*

Records an app under ATL (and optionally on Android for comparison) and reports hit stubs, missing methods,
swallowed exceptions, view-tree differences, jank and memory growth, plus frames and scroll distance per step –
ranked by what to fix first. Behaves like a standard Linux command-line tool (text/JSON on stdout, exit codes,
no prompts). The manual page is `atl-doctor(1)`.

**Status:** early (0.1). The recording side needs a Linux host with ATL; the Android side needs `adb` and a device or emulator.

## Installation

```
pipx install .                                  # the command line tool (Python >= 3.11, no dependencies)
meson setup build agent && meson install -C build   # JVMTI agent, method-trace counter, input helper, man page
```

The helpers need a JDK (for `jni.h`); the input helper (`--headless`/`--steps`) additionally needs wayland-client,
xkbcommon and wayland-scanner and is skipped without them. The helpers are installed to `<prefix>/lib/atl-doctor`
(also on multiarch/lib64 systems; that is where `atl-doctor` looks for them). `atl-doctor check` lists what is available and how to add
what is missing. Two optional pieces live outside this repository's build: the JVMTI plugin of ART
(`tools/build-jvmti-plugin.sh`) and the view-tree hook (`docs/atlviewdump.md`).

Configuration: options win over the environment variables `ATL_DOCTOR_PREFIX`, `ATL_DOCTOR_ATL_SRC`,
`ATL_DOCTOR_AOSP`, `ATL_DOCTOR_AGENT`; there is no configuration file.

## Examples

```
atl-doctor check --prefix ~/.local/opt/atl            # what can be measured on this machine
atl-doctor scan app.apk --atl-src ~/atl-src           # static coverage of an APK, nothing is started
atl-doctor record -o run1 --prefix ~/.local/opt/atl app.apk   # record until the app ends (or Ctrl-C)
atl-doctor mark -d run1 "Settings open"               # from another terminal: start a new phase
atl-doctor report run1                                # text report on stdout
atl-doctor report -f json -o run1.json run1           # machine-readable; -f html -o FILE for a page
atl-doctor report run1 android-run1                   # ATL against Android, phases and steps side by side
atl-doctor report --log crash.log                     # evaluate an ATL log without a recording
atl-doctor explain -d run1 3f2a                       # everything about one finding (short ID)
atl-doctor diff before.json after.json --fail-on new  # after a fix: new, gone, changed; exit 4 for CI
```

A jank finding needs at least 30 frames in the phase (`--jank-min-frames N`); with fewer, the report only notes
"too few frames".

Exit status: 0 ok, 1 error, 2 usage error, 3 required prerequisite missing, 4 findings requested with `--fail-on`.

## Scenarios

A *scenario* is a step file (`record --steps FILE`) that drives an app: click, type, key, `scroll N` (N real mouse-wheel notches), `scroll-smooth DY` (touchpad-like), wait, mark,
screenshot, `repeat N { ... }`. The same file runs under ATL and on Android, and the report lists, for every
step that ran, the frames drawn and the distance scrolled (from the view trees before and after the step),
ATL next to Android.

atl-doctor itself knows nothing about particular apps. Tests for a specific app are step files that live
outside the tool, next to whoever cares about that app; the tool only measures. [`examples/`](examples) holds
two step files for a free app (Lightning browser, from F-Droid) to start from.

## Layout

```
atl_doctor/        the tool (Python); sources/ has one module per kind of measurement
agent/             C helpers: JVMTI agent, method-trace counter, input helper (meson)
doc/atl-doctor.1   manual page
examples/          example step files
docs/              notes on the optional pieces
```

License: GPL-3.0-or-later (same as ATL), see [LICENSE](LICENSE).
