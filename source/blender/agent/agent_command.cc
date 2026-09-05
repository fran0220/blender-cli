/* SPDX-FileCopyrightText: 2026 blender-cli Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

#include <cstdio>
#include <cstring>

#include "BLI_utildefines.hh"

#include "BKE_blender_cli_command.hh"
#include "BKE_blender_version.h"

#include "AGENT_command.hh"

namespace blender::agent {

class AgentCommand : public CommandHandler {
 public:
  AgentCommand() : CommandHandler("agent") {}

  int exec(bContext * /*C*/, int argc, const char **argv) override
  {
    if (argc == 0 || STREQ(argv[0], "--help")) {
      puts(
          "Usage: blender-cli <session|exec|inspect|observe|compare|describe> [options]\n"
          "  exec -c CODE | FILE.py [--timeout S]\n"
          "  inspect [--object NAME] [--full] [--select PATH ...]\n"
          "  Common: --file F --save [F] --json\n"
          "  --version: upstream version and fork tag");
      return 0;
    }
    if (STREQ(argv[0], "--version")) {
      printf("Blender %s-agent.1\n", BKE_blender_version_string());
      return 0;
    }
    puts(
        "{\"ok\":false,\"error\":{\"type\":\"NotImplemented\","
        "\"message\":\"This verb is not implemented yet\"}}");
    return 1;
  }
};

void command_register()
{
  BKE_blender_cli_command_register(std::make_unique<AgentCommand>());
}

}  // namespace blender::agent
