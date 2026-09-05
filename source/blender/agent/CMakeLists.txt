# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

if(NOT WITH_PYTHON OR WITH_PYTHON_MODULE OR NOT WITH_BLENDER)
  message(FATAL_ERROR "WITH_AGENT requires the Blender executable with embedded Python")
endif()

# A CLI verb is one request and its flags are that request's fields, so the
# flag table is generated from the request table instead of being written twice.
# Point this at whichever module holds `REQUESTS`; nothing else needs to change.
set(agent_contract "${CMAKE_CURRENT_SOURCE_DIR}/agent_contract.py")
set(agent_cli_table "${CMAKE_CURRENT_BINARY_DIR}/agent_cli_table.hh")
add_custom_command(
  OUTPUT
    ${agent_cli_table}
  COMMAND
    ${PYTHON_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/agent_cli_gen.py
    ${agent_contract}
    ${agent_cli_table}
  DEPENDS
    ${agent_contract}
    ${CMAKE_CURRENT_SOURCE_DIR}/agent_cli_gen.py
  COMMENT "Generating the agent CLI projection from the request table"
)
set_source_files_properties(${agent_cli_table} PROPERTIES GENERATED TRUE)

set(INC . ${CMAKE_CURRENT_BINARY_DIR} ../python ../makesrna ../blenloader
  ../editors/include ../windowmanager ../render)
set(INC_SYS ${PYTHON_INCLUDE_DIRS})
set(SRC agent_command.cc AGENT_command.hh agent_session.cc agent_session.hh
  agent_cli.hh ${agent_cli_table} agent_events.hh agent_socket.hh agent_transport.hh
  agent_context.cc agent_context.hh agent_render.cc agent_render.hh)
set(LIB
  PRIVATE bf::blenkernel
  PRIVATE bf::blenlib
  PRIVATE bf::dna
  PRIVATE bf::imbuf
  PRIVATE bf::intern::guardedalloc
  PRIVATE bf::extern::json
)
blender_add_lib(bf_agent "${SRC}" "${INC}" "${INC_SYS}" "${LIB}")

add_executable(blender-cli launcher.cc ${agent_cli_table})
target_include_directories(blender-cli PRIVATE ${CMAKE_CURRENT_SOURCE_DIR} ${CMAKE_CURRENT_BINARY_DIR})
target_link_libraries(blender-cli PRIVATE bf::extern::json)
option(AGENT_WARNINGS_AS_ERRORS "Treat warnings in agent targets as errors" OFF)
if(AGENT_WARNINGS_AS_ERRORS)
  if(MSVC)
    target_compile_options(bf_agent PRIVATE /WX)
    target_compile_options(blender-cli PRIVATE /WX)
  else()
    target_compile_options(bf_agent PRIVATE -Werror)
    target_compile_options(blender-cli PRIVATE -Werror)
  endif()
endif()
if(WIN32)
  target_link_libraries(blender-cli PRIVATE ws2_32)
  target_link_libraries(bf_agent PRIVATE ws2_32)
endif()
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
install(FILES agent_runtime.py agent_contract.py agent.py agent_observe.py agent_rna.py
  agent_compare.py
  agent_program.py agent_feedback.py agent_target.py agent_fit.py
  DESTINATION ${agent_scripts_dir})
