# SPDX-License-Identifier: GPL-3.0-or-later
project('atl-doctor-helpers', 'c', version: '0.1.0', meson_version: '>=0.60', default_options: ['c_std=c17'])

java_home = get_option('jdk')
if java_home == ''
  java_home = run_command('sh', '-c', 'echo "${JAVA_HOME:-/usr/lib/jvm/default-java}"', check: true).stdout().strip()
endif
jni_inc = include_directories(java_home / 'include', java_home / 'include' / 'linux')

cc = meson.get_compiler('c')
helper_dir = get_option('libdir') / 'atl-doctor'
agent_deps = [dependency('threads'), cc.find_library('dl', required: false)]

if host_machine.system() == 'android'
  # cross build for a device: only the agent, named after the ABI
  abi = meson.get_external_property('android_abi')
  shared_module('atl_doctor_agent-android-' + abi, 'atl_doctor_agent.c', name_prefix: 'lib',
                include_directories: jni_inc, dependencies: agent_deps, install: true, install_dir: helper_dir)
else
  shared_module('atl_doctor_agent', 'atl_doctor_agent.c', name_prefix: 'lib',
                include_directories: jni_inc, dependencies: agent_deps, install: true, install_dir: helper_dir)
  executable('atl-doctor-tracecount', 'atl_doctor_tracecount.c', install: true, install_dir: helper_dir)
endif
