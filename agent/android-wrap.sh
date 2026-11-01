#!/system/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# android-wrap.sh - start wrapper for an app on a rooted Android device (setprop wrap.@PACKAGE@ <this file>):
#   loads ART's JVMTI plugin and the atl-doctor agent at process start. `atl-doctor record --android` fills
#   in @PACKAGE@ and @OPTS@ (further agent options such as ",arm=FILE,marks=FILE") and pushes the result.
#   Loading at start makes the runtime debuggable: `am attach-agent` is refused for non-debuggable apps.
a=$1; shift
exec $a -Xplugin:libopenjdkjvmti.so -agentpath:/data/local/tmp/atl-doctor/agent.so=/data/data/@PACKAGE@/atl-doctor-agent.tsv@OPTS@ "$@"
