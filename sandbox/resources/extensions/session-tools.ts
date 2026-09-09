import { appendFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const info = () => ({
    uid: process.getuid?.(), cwd: process.cwd(), home: process.env.HOME,
    venv: process.env.VIRTUAL_ENV,
    tools: pi.getAllTools().map((tool) => tool.name),
  });
  pi.on("session_start", async (_event, ctx) => {
    await writeFile(join(ctx.cwd, "extension-loaded.json"), JSON.stringify(info(), null, 2));
  });
  pi.on("tool_result", async (event, ctx) => {
    await appendFile(join(ctx.cwd, "extension-audit.jsonl"), JSON.stringify({
      tool: event.toolName, isError: event.isError, timestamp: new Date().toISOString(),
    }) + "\n");
  });
  pi.registerTool({
    name: "session_info", label: "Session info",
    description: "Inspect this session's UID, workspace, venv and available tools.",
    parameters: Type.Object({}),
    async execute() {
      const details = info();
      return { content: [{ type: "text", text: JSON.stringify(details) }], details };
    },
  });
  pi.registerCommand("session-info", {
    description: "Show isolated session information without calling a model",
    handler: async () => {
      pi.sendMessage({ customType: "session-info", content: JSON.stringify(info()), display: true }, { triggerTurn: false });
    },
  });
}
