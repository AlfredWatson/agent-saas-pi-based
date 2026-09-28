import { estimateTokens, sessionEntryToContextMessages, type SessionManager } from "@earendil-works/pi-coding-agent";

export type SessionTokenStats = { total_tokens: number; context_tokens: number };

function sumTokens(messages: Parameters<typeof estimateTokens>[0][]): number {
	const total = messages.reduce((sum, message) => sum + estimateTokens(message), 0);
	if (!Number.isSafeInteger(total) || total < 0) throw new Error("invalid_session_token_stats");
	return total;
}

/** Content estimates, not provider usage. Compaction summaries only count in the active context. */
export function sessionTokenStats(manager: SessionManager): SessionTokenStats {
	const originalMessages = manager.getEntries()
		.filter((entry) => entry.type === "message" || entry.type === "custom_message")
		.flatMap(sessionEntryToContextMessages);
	return {
		total_tokens: sumTokens(originalMessages),
		context_tokens: sumTokens(manager.buildSessionContext().messages),
	};
}
