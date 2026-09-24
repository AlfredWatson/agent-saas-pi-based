type CompactionEvent = {
	type: string;
	reason?: unknown;
	result?: unknown;
	aborted?: unknown;
};

/** Project Pi compaction events without exposing summaries or provider errors. */
export function projectCompactionEvent(event: CompactionEvent): Record<string, string> | undefined {
	if (event.type !== "compaction_start" && event.type !== "compaction_end") return undefined;
	if (event.reason !== "manual" && event.reason !== "threshold" && event.reason !== "overflow") return undefined;
	if (event.type === "compaction_start") return { type: "compaction_started", reason: event.reason };
	return {
		type: "compaction_ended",
		reason: event.reason,
		status: event.aborted ? "aborted" : event.result ? "completed" : "failed",
	};
}
