#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# build-jvmti-plugin.sh ART_TREE PREFIX - build ART's JVMTI plugin libopenjdkjvmti.so to match an ATL prefix.
#
# art_standalone does not build the plugin (the make target ____art_all lacks libopenjdkjvmti). This compiles only
# the sources of art/openjdkjvmti with exactly the flags Android's make would use for them (taken from `make -n`)
# and links against the libart*.so of PREFIX.
#   ART_TREE  a COPY of the art_standalone tree the libart of PREFIX was built from (never the original worktree:
#             `make -n` creates files). With GCC 14 first apply tools/openjdkjvmti-gcc14.diff in it (patch -p0).
#   PREFIX    the ATL installation prefix (read only)
#   J=N       parallel compile jobs (default 2)
# Result: ART_TREE/jvmti-out/libopenjdkjvmti.so - copy it to PREFIX/lib64/art/ (or point atl-doctor at a prefix
# that has it there).
set -euo pipefail
A=${1:?usage: build-jvmti-plugin.sh ART_TREE PREFIX}
P=$(realpath "${2:?usage: build-jvmti-plugin.sh ART_TREE PREFIX}")
cd "$A"
O=jvmti-out
mkdir -p $O
make -n HOST_GLOBAL_CONLYFLAGS=-std=gnu23 out/host/linux-x86/lib64/libopenjdkjvmti.so 2>/dev/null \
  | grep '^g++ .*-c .*libopenjdkjvmti_intermediates' \
  | sed "s#-MD -MF [^ ]*##; s#-o out/host/linux-x86/obj/SHARED_LIBRARIES/libopenjdkjvmti_intermediates/#-I art/openjdkjvmti/include -I libbase/include -I art/dexlayout -I out/host/linux-x86/obj/SHARED_LIBRARIES/libart_intermediates -o $O/#" \
  > $O/cmds.txt || true
n=$(wc -l < $O/cmds.txt)
[ "$n" -gt 0 ] || { echo "build-jvmti-plugin.sh: no compile commands from make -n in $A (not an art_standalone tree?)" >&2; exit 1; }
echo "$n sources" >&2
xargs -P "${J:-2}" -I{} sh -c '{}' < $O/cmds.txt
g++ -shared -Wl,-soname,libopenjdkjvmti.so -m64 -Wl,-z,noexecstack -Wl,-z,relro -Wl,-z,now $O/*.o \
  -L"$P/lib64/art" -Wl,-rpath,"$P/lib64/art" -lart -lart-compiler -lart-dexlayout -ldexfile -lartbase -lbase \
  -o $O/libopenjdkjvmti.so
echo "$A/$O/libopenjdkjvmti.so"
