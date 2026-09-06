# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# Follow the installed TEST_BLENDER_EXE layout, including the macOS app bundle.
get_filename_component(agent_bin_dir "${TEST_BLENDER_EXE}" DIRECTORY)
add_blender_test_impl(agent_protocol "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/protocol.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_protocol PROPERTIES TIMEOUT 240)

add_blender_test_impl(agent_describe "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/describe.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_describe PROPERTIES TIMEOUT 240)

add_blender_test_impl(agent_cli "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/cli.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_cli PROPERTIES TIMEOUT 600)

add_blender_test_impl(agent_session "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/session.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_session PROPERTIES TIMEOUT 1800)

add_blender_test_impl(agent_program "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/program.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_program PROPERTIES TIMEOUT 600)

add_blender_test_impl(agent_observe "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/observe.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_observe PROPERTIES TIMEOUT 1800)

add_blender_test_impl(agent_feedback "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/feedback.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_feedback PROPERTIES TIMEOUT 1800)

add_blender_test_impl(agent_fit "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/fit.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
# Every fit path, every metric and a 20-iteration loop, all against real renders.
set_tests_properties(agent_fit PROPERTIES TIMEOUT 3600)
set_tests_properties(agent_session agent_observe agent_feedback agent_fit
  PROPERTIES SKIP_RETURN_CODE 77)
