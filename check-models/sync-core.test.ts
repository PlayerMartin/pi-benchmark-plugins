/**
 * Self-test for sync-core.ts. Run with:
 *   node check-models/sync-core.test.ts
 * (Node 22.7+ runs TS directly via type stripping; no build step needed.)
 */

import { strict as assert } from "node:assert";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
	DEFAULT_PROVIDER_ID,
	fetchModelIds,
	findEInfraProvider,
	interpolateValue,
	readModelsFile,
	resolveConfigValue,
	syncConfig,
	writeModelsFile,
} from "./sync-core.ts";

let failures = 0;
const tests: Array<{ name: string; fn: () => Promise<void> | void }> = [];

function test(name: string, fn: () => Promise<void> | void) {
	tests.push({ name, fn });
}

// --- interpolation ----------------------------------------------------------

test("interpolateValue resolves $VAR, ${VAR}, $$, $!", () => {
	assert.equal(interpolateValue("$K", { K: "v" }), "v");
	assert.equal(interpolateValue("${K}ick", { K: "qu" }), "quick");
	assert.equal(interpolateValue("$$150", {}), "$150");
	assert.equal(interpolateValue("$!ls", {}), "!ls");
	assert.equal(interpolateValue("a-$MISSING-b", {}), "a--b");
	assert.equal(interpolateValue("no dollars", {}), "no dollars");
});

test("resolveConfigValue runs !commands and interpolates plain values", async () => {
	assert.equal(await resolveConfigValue("plain"), "plain");
	assert.equal(await resolveConfigValue("$$X"), "$X");
	const out = await resolveConfigValue("!echo hello-from-shell");
	assert.equal(out, "hello-from-shell");
	assert.equal(await resolveConfigValue(undefined), undefined);
});

// --- provider matching ------------------------------------------------------

test("findEInfraProvider matches by baseUrl and falls back to cerit", () => {
	assert.equal(
		findEInfraProvider({
			providers: {
				other: { baseUrl: "https://other.example/v1" },
				gateway: { baseUrl: "https://llm.ai.e-infra.cz/v1" },
			},
		}),
		"gateway",
	);
	assert.equal(findEInfraProvider({ providers: { x: { baseUrl: "https://x.example" } } }), "cerit");
	assert.equal(findEInfraProvider({}), DEFAULT_PROVIDER_ID);
});

// --- syncConfig -------------------------------------------------------------

const F1 = ["a", "b", "c"];

test("syncConfig creates provider, entries, and dedupes ids", () => {
	const { config, summary } = syncConfig({ providers: {} }, "cerit", F1);
	const p = config.providers!["cerit"];
	assert.equal(p.baseUrl, "https://llm.ai.e-infra.cz/v1");
	assert.equal(p.api, "openai-completions");
	assert.deepEqual(p.models, [{ id: "a" }, { id: "b" }, { id: "c" }]);
	assert.deepEqual(summary.added, F1);
	assert.deepEqual(summary.kept, []);
	assert.deepEqual(summary.stale, []);
	assert.equal(summary.total, 3);
	assert.equal(summary.changed, true);
});

test("syncConfig preserves existing extended config by id", () => {
	const before = {
		providers: {
			cerit: {
				baseUrl: "https://llm.ai.e-infra.cz/v1",
				api: "openai-completions",
				apiKey: "KEY",
				models: [{ id: "a", contextWindow: 999, cost: { input: 1, output: 2 } }],
			},
		},
	};
	const { config, summary } = syncConfig(before, "cerit", ["a", "b"]);
	const models = config.providers!["cerit"].models!;
	assert.deepEqual(models[0], { id: "a", contextWindow: 999, cost: { input: 1, output: 2 } });
	assert.deepEqual(models[1], { id: "b" });
	assert.deepEqual(summary.added, ["b"]);
	assert.deepEqual(summary.kept, ["a"]);
	assert.equal(before.providers.cerit.apiKey, "KEY", "apiKey untouched");
});

test("syncConfig keeps stale without prune and removes them with prune", () => {
	const before = {
		providers: {
			cerit: {
				baseUrl: "https://llm.ai.e-infra.cz/v1",
				models: [{ id: "a" }, { id: "gone" }],
			},
		},
	};
	const noPrune = syncConfig(structuredClone(before), "cerit", F1);
	assert.deepEqual(noPrune.summary.stale, ["gone"]);
	assert.deepEqual(noPrune.summary.pruned, []);
	assert.deepEqual(
		noPrune.config.providers!["cerit"].models!.map((m) => m.id),
		["a", "b", "c", "gone"],
	);
	assert.equal(noPrune.summary.changed, true, "added models => changed");

	// prune with an already-in-sync file => changed: false
	const pruned = syncConfig(structuredClone(before), "cerit", F1, { prune: true });
	assert.deepEqual(pruned.summary.pruned, ["gone"]);
	assert.deepEqual(
		pruned.config.providers!["cerit"].models!.map((m) => m.id),
		["a", "b", "c"],
	);

	const same = syncConfig(
		{ providers: { cerit: { models: [{ id: "x" }, { id: "y" }] } } },
		"cerit",
		["x", "y"],
		{ prune: true },
	);
	assert.equal(same.summary.changed, false);
});

// --- fetchModelIds ----------------------------------------------------------

function stubFetch(impl: (url: string, init?: RequestInit) => Promise<Response>) {
	const orig = globalThis.fetch;
	(globalThis as { fetch: typeof fetch }).fetch = impl;
	return () => {
		(globalThis as { fetch: typeof fetch }).fetch = orig;
	};
}

test("fetchModelIds parses the OpenAI list format and dedupes", async () => {
	const restore = stubFetch(async () => {
		return new Response(
			JSON.stringify({
				object: "list",
				data: [
					{ id: "llama-3.3-70b", object: "model", created: 1, owned_by: "meta" },
					{ id: "llama-3.3-70b", object: "model", created: 1, owned_by: "meta" },
					{ id: "qwen2.5-coder:32b", object: "model", created: 2, owned_by: "qwen" },
				],
			}),
			{ status: 200, headers: { "content-type": "application/json" } },
		);
	});
	try {
		const res = await fetchModelIds({ apiKey: "sk-test" });
		assert.equal(res.ok, true);
		assert.deepEqual(res.ids, ["llama-3.3-70b", "qwen2.5-coder:32b"]);
	} finally {
		restore();
	}
});

test("fetchModelIds sends Bearer auth and respects custom authorization headers", async () => {
	let seen: Record<string, string> | undefined;
	const restore = stubFetch(async (_url, init) => {
		seen = (init?.headers as Record<string, string>) ?? {};
		return new Response(JSON.stringify({ data: [{ id: "m1" }] }), { status: 200 });
	});
	try {
		await fetchModelIds({ apiKey: "sk-default" });
		assert.equal(seen?.["Authorization"], "Bearer sk-default");

		await fetchModelIds({
			apiKey: "sk-default",
			headers: { Authorization: "Bearer sk-custom", "x-extra": "1" },
		});
		assert.equal(seen?.["Authorization"], "Bearer sk-custom", "custom header wins");
		assert.equal(seen?.["x-extra"], "1");
	} finally {
		restore();
	}
});

test("fetchModelIds surfaces non-2xx status", async () => {
	const restore = stubFetch(async () => new Response("Unauthorized", { status: 401 }));
	try {
		const res = await fetchModelIds({ apiKey: "bad" });
		assert.equal(res.ok, false);
		assert.equal(res.status, 401);
		assert.deepEqual(res.ids, []);
	} finally {
		restore();
	}
});

// --- file round-trip --------------------------------------------------------

test("readModelsFile / writeModelsFile round-trip against a temp agent dir", async () => {
	const dir = mkdtempSync(join(tmpdir(), "pi-check-models-"));
	const prev = process.env.PI_CODING_AGENT_DIR;
	process.env.PI_CODING_AGENT_DIR = dir;
	try {
		assert.deepEqual(await readModelsFile(), { providers: {} }, "missing file => empty config");
		const cfg = { providers: { cerit: { baseUrl: "https://llm.ai.e-infra.cz/v1", models: [{ id: "z" }] } } };
		const path = await writeModelsFile(cfg);
		assert.equal(path, join(dir, "models.json"));
		assert.deepEqual(await readModelsFile(), cfg);
	} finally {
		if (prev === undefined) delete process.env.PI_CODING_AGENT_DIR;
		else process.env.PI_CODING_AGENT_DIR = prev;
	}
});

// --- runner ----------------------------------------------------------------

async function main() {
	for (const { name, fn } of tests) {
		try {
			await fn();
			console.log(`  ok   ${name}`);
		} catch (err) {
			failures += 1;
			console.error(`FAIL   ${name}`);
			console.error(`       ${(err as Error).stack?.split("\n").slice(0, 4).join("\n       ")}`);
		}
	}
	console.log(`\n${tests.length - failures}/${tests.length} tests passed`);
	process.exit(failures === 0 ? 0 : 1);
}

void main();