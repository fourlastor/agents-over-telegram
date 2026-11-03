/**
 * Test-only Pi extension: registers the local scripted model from
 * scripts/e2e/scripted_model.py as provider "aot-scripted", model "scripted".
 * Load it with `pi -e scripts/e2e/pi_scripted_provider.ts --model aot-scripted/scripted`.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const port = process.env.AOT_SCRIPTED_PORT || "18766";
	pi.registerProvider("aot-scripted", {
		name: "AOT scripted e2e model",
		baseUrl: `http://127.0.0.1:${port}/v1`,
		apiKey: "scripted-not-a-secret",
		api: "openai-completions",
		models: [
			{
				id: "scripted",
				name: "Scripted e2e model",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 128000,
				maxTokens: 4096,
			},
		],
	});
}
