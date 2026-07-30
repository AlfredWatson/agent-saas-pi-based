import type { AgentSession } from "@earendil-works/pi-coding-agent";

export type ManagedSession = { tenant: string; session: AgentSession; busy: boolean; sessionFile: string | undefined };

export class SessionRegistry {
	private readonly entries = new Map<string, ManagedSession>();
	get(id: string): ManagedSession | undefined { return this.entries.get(id); }
	set(id: string, entry: ManagedSession): void { this.entries.set(id, entry); }
	delete(id: string): void { this.entries.get(id)?.session.dispose(); this.entries.delete(id); }
}
