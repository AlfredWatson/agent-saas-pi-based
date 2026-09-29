import type { AgentSession, ModelRuntime } from "@earendil-works/pi-coding-agent";
import type { PayloadRedactor } from "./event-projection.js";

export type ManagedSession = {
	tenant: string;
	session: AgentSession;
	modelRuntime: ModelRuntime;
	providerId: string;
	modelId: string;
	thinkingLevel?: string;
	busy: boolean;
	sessionFile: string;
	redactor: PayloadRedactor;
	configVersion: number;
	isSubagent: boolean;
};

export class SessionRegistry {
	private readonly entries = new Map<string, ManagedSession>();
	get(id: string): ManagedSession | undefined { return this.entries.get(id); }
	set(id: string, entry: ManagedSession): void { this.entries.set(id, entry); }
	delete(id: string): void { this.entries.get(id)?.session.dispose(); this.entries.delete(id); }
	anyBusy(ids: Iterable<string>): boolean { return [...ids].some((id) => this.entries.get(id)?.busy); }
}
