/**
 * pi-token-counter — counts input and output tokens for the current session.
 *
 * Interactive (tui): live token status in the footer + /tokens command.
 * Headless (print): prints the totals to the terminal before Pi exits.
 * JSON mode: totals are written to stderr so the JSON stream on stdout stays valid.
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { writeSync } from "node:fs";

interface Totals {
	requests: number;
	input: number;
	output: number;
}

interface UsageLike {
	input: number;
	output: number;
}

function zeroTotals(): Totals {
	return {
		requests: 0,
		input: 0,
		output: 0,
	};
}

function addUsage(totals: Totals, usage: UsageLike): void {
	totals.input += usage.input;
	totals.output += usage.output;
}

function fmt(n: number): string {
	return n.toLocaleString("en-US");
}

function summaryLine(totals: Totals): string {
	return `in ${fmt(totals.input)} · out ${fmt(totals.output)} · ${totals.requests} req`;
}

function report(totals: Totals): string {
	const lines = [
		"---- token usage ----",
		`requests:     ${fmt(totals.requests)}`,
		`input:        ${fmt(totals.input)}`,
		`output:       ${fmt(totals.output)}`,
	];
	return "\n" + lines.join("\n") + "\n";
}

export default function tokenCounter(pi: ExtensionAPI) {
	const totals = zeroTotals();
	let printed = false;

	function updateStatus(ctx: ExtensionContext): void {
		if (ctx.mode !== "tui") return;
		const theme = ctx.ui.theme;
		ctx.ui.setStatus(
			"token-counter",
			theme.fg("dim", ` tokens: ↑${fmt(totals.input)} ↓${fmt(totals.output)}`)
		);
	}

	// Accumulate usage from every assistant response, plus usage reported by
	// tools that made nested model calls (e.g. subagents).
	pi.on("message_end", async (event, ctx) => {
		const message = event.message;
		if (message.role === "assistant" && message.usage) {
			totals.requests++;
			addUsage(totals, message.usage);
		} else if (message.role === "toolResult" && message.usage) {
			addUsage(totals, message.usage);
		}
		updateStatus(ctx);
	});

	// Headless modes: print the totals before Pi exits.
	//
	// Print mode redirects `process.stdout` (extension writes to it end up on
	// stderr), and pi emits the final response after `session_shutdown` fires.
	// So we write to fd 1 directly from a process `exit` hook, which lands
	// after the response, just before the process exits.
	pi.on("session_shutdown", async (_event, ctx) => {
		if (printed) return;
		if (ctx.mode !== "print" && ctx.mode !== "json") return;
		printed = true;

		const text = report(totals);
		if (ctx.mode === "print") {
			process.on("exit", () => {
				writeSync(1, text);
			});
		} else {
			// JSON mode: stdout carries the JSON stream, write to stderr instead.
			process.stderr.write(text);
		}
	});

	// Interactive: /tokens shows the current totals.
	pi.registerCommand("tokens", {
		description: "Show input/output token usage for this session",
		handler: async (_args, ctx) => {
			ctx.ui.notify(report(totals).trim(), "info");
		},
	});

	// Clear the footer status on teardown (quit/reload/session switch).
	pi.on("session_shutdown", async (_event, ctx) => {
		if (ctx.mode === "tui") {
			ctx.ui.setStatus("token-counter", undefined);
		}
	});
}
