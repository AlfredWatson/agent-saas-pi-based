import { expect, test } from "vitest";
import { projectCompactionEvent } from "../src/sessions/compaction-event.js";

test("compaction events retain order and expose only reason and outcome", () => {
	const events = [
		{ type: "compaction_start", reason: "threshold", secret: "provider-key" },
		{ type: "compaction_end", reason: "threshold", result: { summary: "private context" }, aborted: false },
		{ type: "compaction_start", reason: "overflow" },
		{ type: "compaction_end", reason: "overflow", result: undefined, aborted: false, errorMessage: "provider-key" },
		{ type: "compaction_end", reason: "manual", result: undefined, aborted: true },
	].map(projectCompactionEvent);
	expect(events).toEqual([
		{ type: "compaction_started", reason: "threshold" },
		{ type: "compaction_ended", reason: "threshold", status: "completed" },
		{ type: "compaction_started", reason: "overflow" },
		{ type: "compaction_ended", reason: "overflow", status: "failed" },
		{ type: "compaction_ended", reason: "manual", status: "aborted" },
	]);
	expect(JSON.stringify(events)).not.toMatch(/provider-key|private context/);
});
