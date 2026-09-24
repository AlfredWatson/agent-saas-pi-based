import type { LocalModel } from "../local-models.js";

/** Pi's 16k/20k defaults cannot compact a local model with a smaller window. */
export function localCompactionSettings(model: LocalModel): { reserveTokens: number; keepRecentTokens: number } {
	const reserveTokens = Math.min(model.context_window - 1, Math.max(model.max_tokens + 256, Math.floor(model.context_window / 4)));
	return { reserveTokens, keepRecentTokens: Math.max(1, Math.floor((model.context_window - reserveTokens) / 3)) };
}
