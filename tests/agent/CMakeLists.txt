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
# what an 8% margin buys.
#
# Run 34114563805 then measured a slower Windows host: describe 190.58, cli
# 121.47, program 221.48, observe 540.94, feedback 241.66, fit 2085.22 seconds.
#
# Run 34128930729, the first Windows build with the budgets above and the
# render that used to run before its reference was read, is the basis now:
# protocol 294.64, describe 160.94, cli 99.91, session 1281.04, program 176.59,
# observe 421.48, feedback 191.57, io 1159.13 seconds. Its fit reached 1363.24
# before failing an assertion, which is a lower bound on a run and not a
# measurement of one, so fit keeps the 4800 that 2085.22 earned.
#
# protocol takes the next step up rather than the floor: it is the only test
# these budgets have ever killed, twice, and it grows with every one-shot verb
# asserted, because each renders its own feedback. Re-measure when the evidence
# platform changes.

# Follow the installed TEST_BLENDER_EXE layout, including the macOS app bundle.
get_filename_component(agent_bin_dir "${TEST_BLENDER_EXE}" DIRECTORY)
add_blender_test_impl(agent_protocol "" "${TEST_PYTHON_EXE}"
  ${TEST_PYTHON_EXE_EXTRA_ARGS}
  "${CMAKE_CURRENT_SOURCE_DIR}/protocol.py"
  "${agent_bin_dir}/blender-cli${CMAKE_EXECUTABLE_SUFFIX}"
)
set_tests_properties(agent_protocol PROPERTIES TIMEOUT 1200)

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
set_tests_properties(agent_session PROPERTIES TIMEOUT 3000)

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
set_tests_properties(agent_io PROPERTIES TIMEOUT 2400)

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
set_tests_properties(agent_fit PROPERTIES TIMEOUT 4800)
set_tests_properties(agent_session agent_observe agent_feedback agent_fit
  PROPERTIES SKIP_RETURN_CODE 77)
