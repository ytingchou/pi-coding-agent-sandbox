import { fileURLToPath } from "node:url";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const script = fileURLToPath(new URL("../scripts/stats.py", import.meta.url));

export default function (pi: ExtensionAPI) {
  async function calculate(numbers: number[]) {
    if (!numbers.length || numbers.length > 1000 || !numbers.every(Number.isFinite)) {
      throw new Error("Provide 1–1000 finite numbers");
    }
    const result = await pi.exec("python", [script, JSON.stringify(numbers)], { timeout: 10000 });
    if (result.code !== 0) throw new Error(result.stderr || "Python statistics failed");
    return JSON.parse(result.stdout);
  }
  pi.registerTool({
    name: "python_stats", label: "Python statistics",
    description: "Execute the stats-kit Python helper in this session's venv and return statistics.",
    parameters: Type.Object({ numbers: Type.Array(Type.Number(), { minItems: 1, maxItems: 1000 }) }),
    async execute(_toolCallId, params) {
      const details = await calculate(params.numbers);
      return { content: [{ type: "text", text: JSON.stringify(details) }], details };
    },
  });
  pi.registerCommand("package-stats", {
    description: "Run stats-kit without a model: /package-stats 2,4,6,8",
    handler: async (args) => {
      const tokens = args.split(",").map((value) => value.trim());
      if (tokens.some((value) => !value)) throw new Error("Provide comma-separated numbers");
      const details = await calculate(tokens.map(Number));
      pi.sendMessage({ customType: "package-stats", content: JSON.stringify(details), display: true }, { triggerTurn: false });
    },
  });
}
