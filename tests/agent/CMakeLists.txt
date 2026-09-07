# SPDX-FileCopyrightText: 2026 blender-cli Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

# A ctest TIMEOUT is a harness budget, not a performance assertion: it exists to
# end a hung run, and a test that trips it tells you nothing about the product.
# Each is at least twice the slowest measured evidence platform. The basis is
# Windows x64 on software Vulkan (run 34101160262, all eight passing): protocol
# 222.08, describe 118.31, cli 84.85, session 1108.22, program 135.47, observe
# 334.13, feedback 150.58, fit 1309.31 seconds. A diagnostic run on a slower
# host had already ended protocol at 240.02 s against its 240 s budget, which is
# what an 8% margin buys. Re-measure these when the evidence platform changes.

# Follow the installed TEST_BLENDER_EXE layout, including the macOS app bundle.
get_filename_component(agent_bin_dir "${TEST_BLENDER_EXE}" DIRECTORY)
add_blender_test_impl(agent_protocol "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/protocol.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_protocol PROPERTIES TIMEOUT 600)

add_blender_test_impl(agent_describe "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/describe.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_describe PROPERTIES TIMEOUT 600)

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
set_tests_properties(agent_session PROPERTIES TIMEOUT 2400)

add_blender_test_impl(agent_program "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/program.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_program PROPERTIES TIMEOUT 600)

add_blender_test_impl(agent_io "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/io.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
# Seven formats, including fresh-process replay and nine real fit evaluations each.
# Non-render IO remains mandatory on a no-device machine: never skip the test.
set_tests_properties(agent_io PROPERTIES TIMEOUT 1800)

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
