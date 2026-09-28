/**
 * Pure sync logic for the e-infra.cz LLM gateway model list -> ~/.pi/agent/models.json.
 * No pi imports here so it can be unit-tested standalone with Node type stripping.
 */

import { homedir } from "node:os";
import { join } from "node:path";
import { readFile, writeFile, rename } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export const MODELS_URL = "https://llm.ai.e-infra.cz/v1/models";
export const GATEWAY_BASE_URL = "https://llm.ai.e-infra.cz/v1";
export const DEFAULT_PROVIDER_ID = "cerit";
export const FETCH_TIMEOUT_MS = 20_000;

export interface ProviderConfig {
	baseUrl?: string;
	api?: string;
	apiKey?: string;
	headers?: Record<string, string>;
	models?: ModelConfig[];
	[key: string]: unknown;
}

export interface ModelConfig {
	id: string;
	[key: string]: unknown;
}

export interface ModelsFile {
	providers?: Record<string, ProviderConfig>;
	[key: string]: unknown;
}

export interface RefreshAuth {
	apiKey?: string;
	headers?: Record<string, string>;
}

export interface SyncSummary {
	/** Provider id that was synced (auto-detected from baseUrl, else "cerit"). */
	providerId: string;
	/** Effective baseUrl of the synced provider. */
	providerBaseUrl: string;
	/** Model ids newly added to models.json. */
	added: string[];
	/** Model ids already present and left untouched. */
	kept: string[];
	/** Model ids configured in models.json but not reported by the gateway. */
	stale: string[];
	/** Model ids removed because prune was requested. */
	pruned: string[];
	/** Total number of models in the provider after sync. */
	total: number;
	/** Path of the models.json that was written. */
	modelsFile: string;
	/** True when the file contents actually changed. */
	changed: boolean;
}

export interface FetchResult {
	ok: boolean;
	status: number;
	ids: string[];
}

/** Path of the global models.json, honoring the PI_CODING_AGENT_DIR override. */
export function modelsJsonPath(): string {
	const agentDir = process.env.PI_CODING_AGENT_DIR ?? join(homedir(), ".pi", "agent");
	return join(agentDir, "models.json");
}

export async function readModelsFile(): Promise<ModelsFile> {
	try {
		return JSON.parse(await readFile(modelsJsonPath(), "utf8")) as ModelsFile;
	} catch {
		return { providers: {} };
	}
}

export async function writeModelsFile(config: ModelsFile): Promise<string> {
	const path = modelsJsonPath();
	const tmp = `${path}.tmp`;
	await writeFile(tmp, JSON.stringify(config, null, 2) + "\n", "utf8");
	await rename(tmp, path);
	return path;
}

/**
 * Find the provider whose baseUrl targets the e-infra gateway.
 * Falls back to DEFAULT_PROVIDER_ID ("cerit") when nothing matches.
 */
export function findEInfraProvider(config: ModelsFile): string {
	for (const [id, p] of Object.entries(config.providers ?? {})) {
		if (typeof p?.baseUrl === "string" && p.baseUrl.includes("llm.ai.e-infra.cz")) {
			return id;
		}
	}
	return DEFAULT_PROVIDER_ID;
}

/**
 * Interpolate a models.json config value: `$VAR`, `${VAR}`, `$$` -> literal `$`,
 * `$!` -> literal `!`. Missing variables expand to the empty string.
 */
export function interpolateValue(raw: string, env: NodeJS.ProcessEnv = process.env): string {
	let out = "";
	let i = 0;
	while (i < raw.length) {
		const c = raw[i];
		if (c === "$" && i + 1 < raw.length) {
			const next = raw[i + 1];
			if (next === "$") {
				out += "$";
				i += 2;
				continue;
			}
			if (next === "!") {
				out += "!";
				i += 2;
				continue;
			}
			if (next === "{") {
				const close = raw.indexOf("}", i + 2);
				if (close !== -1) {
					out += env[raw.slice(i + 2, close)] ?? "";
					i = close + 1;
					continue;
				}
			}
			const m = /^[A-Za-z_][A-Za-z0-9_]*/.exec(raw.slice(i + 1));
			if (m) {
				out += env[m[0]] ?? "";
				i += 1 + m[0].length;
				continue;
			}
		}
		out += c;
		i += 1;
	}
	return out;
}

async function runShellCommand(cmd: string): Promise<string> {
	const flags = process.platform === "win32" ? ["/d", "/c"] : ["-c"];
	const shell = process.platform === "win32" ? "cmd" : "/bin/sh";
	const { stdout } = await execFileAsync(shell, [...flags, cmd], {
		timeout: 10_000,
		encoding: "utf8",
	});
	return stdout.trim();
}

/**
 * Resolve a models.json `apiKey`/`headers` value:
 * `!command` executes the command, anything else gets $-interpolation.
 */
export async function resolveConfigValue(raw: string | undefined): Promise<string | undefined> {
	if (!raw) return undefined;
	if (raw.startsWith("!") && raw.length > 1) {
		return runShellCommand(raw.slice(1));
	}
	return interpolateValue(raw);
}

/** Resolve auth from models.json provider config (apiKey + headers with value resolution). */
export async function resolveAuthFromFile(providerId: string): Promise<RefreshAuth | undefined> {
	const config = await readModelsFile();
	const provider = config.providers?.[providerId];
	if (!provider) return undefined;

	const auth: RefreshAuth = {};
	if (typeof provider.apiKey === "string" && provider.apiKey.trim().length > 0) {
		auth.apiKey = await resolveConfigValue(provider.apiKey);
	}
	if (provider.headers && typeof provider.headers === "object") {
		const headers: Record<string, string> = {};
		for (const [k, v] of Object.entries(provider.headers)) {
			if (typeof v === "string") headers[k] = (await resolveConfigValue(v)) ?? "";
		}
		auth.headers = headers;
	}
	return auth.apiKey || (auth.headers && Object.keys(auth.headers).length > 0) ? auth : undefined;
}

/**
 * GET the gateway's /v1/models and return the ids, deduplicated, in server order.
 * Sends `Authorization: Bearer <apiKey>` unless the custom headers already set it.
 * Network/parse failures throw; HTTP errors are returned as ok:false with the status.
 */
export async function fetchModelIds(auth: RefreshAuth, signal?: AbortSignal): Promise<FetchResult> {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
	const onAbort = () => controller.abort();
	if (signal) {
		if (signal.aborted) controller.abort();
		else signal.addEventListener("abort", onAbort, { once: true });
	}
	try {
		const headers: Record<string, string> = { ...(auth.headers ?? {}) };
		if (
			auth.apiKey &&
			!Object.keys(headers).some((h) => h.toLowerCase() === "authorization")
		) {
			headers["Authorization"] = `Bearer ${auth.apiKey}`;
		}
		const res = await fetch(MODELS_URL, { headers, signal: controller.signal });
		if (!res.ok) return { ok: false, status: res.status, ids: [] };
		const payload = (await res.json()) as { data?: Array<{ id?: string }> };
		const ids = [
			...new Set(
				(payload.data ?? [])
					.map((m) => m.id)
					.filter((id): id is string => typeof id === "string" && id.length > 0),
			),
		];
		return { ok: true, status: res.status, ids };
	} finally {
		clearTimeout(timer);
		signal?.removeEventListener("abort", onAbort);
	}
}

/**
 * Merge fetched model ids into a models.json config.
 * Existing model entries are preserved by id (extended fields like contextWindow,
 * cost, compat survive). Without prune, models the gateway no longer reports are
 * kept at the end of the list; with prune they are removed.
 */
export function syncConfig(
	config: ModelsFile,
	providerId: string,
	fetchedIds: string[],
	opts: { prune?: boolean } = {},
): { config: ModelsFile; summary: SyncSummary } {
	const providers = (config.providers ??= {});
	const provider = (providers[providerId] ??= {}) as ProviderConfig;
	provider.baseUrl ??= GATEWAY_BASE_URL;
	provider.api ??= "openai-completions";

	const existing = (Array.isArray(provider.models) ? provider.models : []) as ModelConfig[];
	const byId = new Map<string, ModelConfig>();
	for (const m of existing) {
		if (m && typeof m.id === "string" && m.id.length > 0) byId.set(m.id, m);
	}

	const seen = new Set<string>();
	const merged: ModelConfig[] = [];
	const added: string[] = [];
	const kept: string[] = [];
	for (const id of fetchedIds) {
		const prev = byId.get(id);
		if (prev) kept.push(id);
		else added.push(id);
		merged.push(prev ?? { id });
		seen.add(id);
	}

	const staleEntries = existing.filter((m) => !seen.has(m.id));
	const stale = staleEntries.map((m) => m.id);

	if (opts.prune) {
		provider.models = merged;
	} else {
		provider.models = [...merged, ...staleEntries];
	}

	const summary: SyncSummary = {
		providerId,
		providerBaseUrl: provider.baseUrl ?? GATEWAY_BASE_URL,
		added,
		kept,
		stale,
		pruned: opts.prune ? stale : [],
		total: provider.models.length,
		modelsFile: "",
		changed: added.length > 0 || (opts.prune ? stale.length > 0 : false),
	};
	return { config, summary };
}

/** Human-readable summary line(s) for command output / tool results. */
export function formatSummary(
	summary: SyncSummary,
	opts: { prune?: boolean } = {},
): string {
	const lines: string[] = [];
	const suffix = opts.prune ? " (pruned stale)" : "";
	lines.push(
		`Synced ${summary.total} model(s) for provider "${summary.providerId}" ` +
			`-> ${summary.modelsFile}${suffix}`,
	);
	if (summary.added.length > 0) {
		const shown = summary.added.slice(0, 8).join(", ");
		const more = summary.added.length > 8 ? `, +${summary.added.length - 8} more` : "";
		lines.push(`  added: ${shown}${more}`);
	}
	if (summary.stale.length > 0) {
		const shown = summary.stale.slice(0, 8).join(", ");
		const more = summary.stale.length > 8 ? `, +${summary.stale.length - 8} more` : "";
		lines.push(`  stale (not on gateway${opts.prune ? ", removed" : ", kept"}): ${shown}${more}`);
	}
	if (summary.added.length === 0 && summary.stale.length === 0) {
		lines.push("  no changes: models.json is already in sync");
	}
	return lines.join("\n");
}