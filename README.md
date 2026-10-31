# atl-doctor

Diagnostics for [Android Translation Layer (ATL)](https://gitlab.com/android_translation_layer/android_translation_layer):
*why doesn't this app run (correctly) under ATL, and what should be fixed first?*

Records an app under ATL (and optionally on Android for comparison) and reports hit stubs, missing methods,
swallowed exceptions, view-tree differences, jank and memory growth – ranked by what to fix first. Behaves like a
standard Linux command-line tool (text/JSON on stdout, exit codes, no prompts).

**Status:** design phase, no code yet.

License: GPL-3.0-or-later (same as ATL), see [LICENSE](LICENSE).
