/**
 * Check Available Models (e-infra.cz)
 *
 * Syncs the live model list from the e-infra.cz LLM gateway
 * (https://llm.ai.e-infra.cz/v1/models) into @agent/models.json so the
 * provider (`cerit` by default) shows up-to-date models in /model.
 *
 * The gateway's available models change as cluster GPUs come and go, so the
 * sync runs once at session start (silent unless new models appear) and can
 * be triggered manually with:
 *
 *   /check-models              sync, keeping stale entries
 *   /check-models --prune      sync, removing entries the gateway no longer reports
 *
 * The LLM can also call the `check_available_models` tool for the same job.
 *
 * Auth resolution order:
 *   1. pi's own provider auth (auth.json via /login, CLI --api-key, etc.) via
 *      ctx.modelRegistry.getProviderAuth("cerit")
 *   2. the provider `apiKey`/`headers` fields in models.json, supporting the
 *      usual value syntax: $ENV_VAR, ${ENV_VAR}, $$, $!, and `!command`
 */

import { defineTool, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import {
	MODELS_URL,
	fetchModelIds,
	findEInfraProvider,
	formatSummary,
	readModelsFile,
	resolveAuthFromFile,
	syncConfig,
	writeModelsFile,
	type RefreshAuth,
	type SyncSummary,
} from "./sync-core.ts";

// ---------------------------------------------------------------------------
// Core refresh pipeline (serialized so startup + manual refreshes never race)
// ---------------------------------------------------------------------------

async function fullRefresh(
	ctx: ExtensionContext,
	opts: { prune?: boolean },
): Promise<{ text: string; summary: SyncSummary }> {
	const config = await readModelsFile();
	const providerId = findEInfraProvider(config);

	// 1. Resolve auth, preferring pi's effective provider auth (auth.json, /login,
	//    CLI --api-key, env vars) and falling back to models.json config values.
	let auth: RefreshAuth | undefined;
	try {
		const resolved = await ctx.modelRegistry.getProviderAuth(providerId);
		if (resolved?.auth?.apiKey || resolved?.auth?.headers) {
			auth = {
				apiKey: resolved.auth.apiKey,
				headers: resolved.auth.headers,
			};
		}
	} catch {
		// Registry unavailable (e.g. tool running outside a live session) -
		// fall through to file-based resolution.
	}
	if (!auth) auth = await resolveAuthFromFile(providerId);

	if (!auth?.apiKey && !auth?.headers) {
		throw new Error(
			`No API key configured for provider "${providerId}". Set "apiKey" in ` +
				`models.json (e.g. "$CERIT_API_KEY"), run /login ${providerId}, or pass --api-key.`,
		);
	}

	// 2. Fetch the live model list.
	const fetched = await fetchModelIds(auth, ctx.signal);
	if (!fetched.ok) {
		const hint =
			fetched.status === 401
				? " (unauthorized - wrong or missing API key)"
				: fetched.status === 403
					? " (forbidden)"
					: "";
		throw new Error(`GET ${MODELS_URL} returned HTTP ${fetched.status}${hint}`);
	}
	if (fetched.ids.length === 0) {
		throw new Error(`GET ${MODELS_URL} returned an empty model list`);
	}

	// 3. Re-read the file (it may have been edited since step 1), merge, write.
	const latest = await readModelsFile();
	const { config: updated, summary } = syncConfig(latest, providerId, fetched.ids, opts);
	summary.modelsFile = await writeModelsFile(updated);
	return { text: formatSummary(summary, opts), summary };
}

/** Serialize refreshes so two triggers (startup + manual) never write concurrently. */
let refreshChain: Promise<unknown> = Promise.resolve();

function queuedRefresh(
	ctx: ExtensionContext,
	opts: { prune?: boolean },
): Promise<{ text: string; summary: SyncSummary }> {
	const run = () => fullRefresh(ctx, opts);
	const next = refreshChain.then(run, run);
	refreshChain = next.catch(() => {});
	return next;
}

// ---------------------------------------------------------------------------
// Extension registration
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
	// Startup: background sync. Silent on failure (e.g. auth not configured yet),
	// and only notifies when genuinely new models appeared.
	pi.on("session_start", (_event, ctx) => {
		void queuedRefresh(ctx, {})
			.then(({ summary }) => {
				if (summary.added.length > 0 && ctx.hasUI) {
					ctx.ui.notify(
						`e-infra: ${summary.added.length} new model(s) synced -> @agent/models.json`,
						"info",
					);
				}
			})
			.catch(() => {
				/* silent at startup */
			});
	});

	// Manual command: /check-models [--prune]
	pi.registerCommand("check-models", {
		description:
			"Fetch available models from llm.ai.e-infra.cz and sync them into @agent/models.json",
		handler: async (args, ctx) => {
			const prune = /\b--?prune\b/i.test(args);
			try {
				const { text } = await queuedRefresh(ctx, { prune });
				ctx.ui.notify(text.split("\n")[0], "info");
			} catch (err) {
				ctx.ui.notify(`/check-models failed: ${(err as Error).message}`, "error");
			}
		},
	});

	// Tool the LLM can call.
	pi.registerTool(
		defineTool({
			name: "check_available_models",
			label: "Check Available Models (e-infra)",
			description:
				`Fetch the currently available models from the e-infra.cz LLM gateway ` +
				`(${MODELS_URL}) and sync them into @agent/models.json so they appear in ` +
				`/model. Returns a summary of added / kept / stale models and the file path. ` +
				`Use prune: true to also remove models that the gateway no longer reports.`,
			parameters: Type.Object({
				prune: Type.Optional(
					Type.Boolean({
						description:
							"Remove models from models.json that the gateway no longer reports (default false)",
					}),
				),
			}),
			async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
				try {
					const { text } = await queuedRefresh(ctx, { prune: params.prune === true });
					return { content: [{ type: "text", text }], details: {} };
				} catch (err) {
					return {
						content: [
							{
								type: "text",
								text: `check_available_models failed: ${(err as Error).message}`,
							},
						],
						details: {},
						isError: true,
					};
				}
			},
		}),
	);
}