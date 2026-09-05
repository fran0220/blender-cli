# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

if(NOT WITH_PYTHON OR WITH_PYTHON_MODULE OR NOT WITH_BLENDER)
  message(FATAL_ERROR "WITH_AGENT requires the Blender executable with embedded Python")
endif()

set(INC . ../python ../makesrna)
set(INC_SYS ${PYTHON_INCLUDE_DIRS})
set(SRC agent_command.cc AGENT_command.hh)
set(LIB
  PRIVATE bf::blenkernel
  PRIVATE bf::blenlib
  PRIVATE bf::dna
  PRIVATE bf::intern::guardedalloc
)
blender_add_lib(bf_agent "${SRC}" "${INC}" "${INC_SYS}" "${LIB}")

add_executable(blender-cli launcher.cc)
if(APPLE)
  set(agent_exe_dir "Blender.app/Contents/MacOS")
  set(agent_scripts_dir "Blender.app/Contents/Resources/${BLENDER_VERSION}/scripts/modules")
elseif(UNIX AND NOT WITH_INSTALL_PORTABLE)
  set(agent_exe_dir bin)
  set(agent_scripts_dir "share/blender/${BLENDER_VERSION}/scripts/modules")
else()
  set(agent_exe_dir .)
  set(agent_scripts_dir "${BLENDER_VERSION}/scripts/modules")
endif()
install(TARGETS blender-cli RUNTIME DESTINATION ${agent_exe_dir})
install(FILES agent_runtime.py DESTINATION ${agent_scripts_dir})
