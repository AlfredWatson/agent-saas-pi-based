import { access, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, test } from "vitest";

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true }))); });

test("Faux sessions use stable tenant-agnostic data paths and Pi default coding tools", async () => {
	const root = await mkdtemp(join(tmpdir(), "pi-saas-runtime-")); roots.push(root);
	process.env.RUNTIME_DATA_ROOT = root;
	const { createSession, deleteWorkspaceData, ensureRuntimeHome, InvalidSessionFileKeyError } = await import("../src/sessions/session-factory.js");
	const { deleteWorkspaceFile, InvalidWorkspaceFilePathError, listWorkspaceFiles, uploadWorkspaceFile, WorkspaceFileExistsError, WorkspaceFileNotFoundError, UnsupportedWorkspaceFileTypeError } = await import("../src/workspaces/file-storage.js");
	await ensureRuntimeHome();
	const managed = await createSession("00000000-0000-0000-0000-000000000001", { workspace_key: "workspace", provider_id: "faux", api_key: "unused", model_id: "faux-1", thinking_level: "low" });
	expect(managed.session.agent.state.tools.map((tool) => tool.name).sort()).toEqual(["bash", "edit", "read", "write"]);
	const events: string[] = [];
	const stop = managed.session.subscribe((event) => events.push(event.type));
	await managed.session.prompt("Say OK");
	stop(); managed.session.dispose();
	expect(events).toContain("agent_settled");
	expect(events).toContain("message_update");
	expect(managed.sessionFile).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl$/);
	expect(managed.sessionFile).not.toContain("/");
	await expect(access(join(root, "agent"))).resolves.toBeUndefined();
	await expect(access(join(root, "home"))).resolves.toBeUndefined();
	await expect(access(join(root, "sessions", managed.sessionFile))).resolves.toBeUndefined();
	await expect(access(join(root, "workspaces", "workspace"))).resolves.toBeUndefined();
	await expect(access(join(root, "tenants", "00000000-0000-0000-0000-000000000001"))).rejects.toThrow();

	const resumed = await createSession("00000000-0000-0000-0000-000000000001", { workspace_key: "workspace", provider_id: "faux", api_key: "unused", model_id: "faux-1", session_file_key: managed.sessionFile });
	expect(resumed.sessionFile).toBe(managed.sessionFile);
	resumed.session.dispose();
	await expect(createSession("00000000-0000-0000-0000-000000000001", { workspace_key: "workspace", provider_id: "faux", api_key: "unused", model_id: "faux-1", session_file_key: "../outside.jsonl" })).rejects.toBeInstanceOf(InvalidSessionFileKeyError);
	await expect(createSession("00000000-0000-0000-0000-000000000001", { workspace_key: "../outside", provider_id: "faux", api_key: "unused", model_id: "faux-1" })).rejects.toThrow("invalid_workspace_key");

	await mkdir(join(root, "workspaces", "other"), { recursive: true });
	await writeFile(join(root, "workspaces", "other", "keep.txt"), "keep");
	await deleteWorkspaceData("workspace", [{ session_id: "session", session_file_key: managed.sessionFile }]);
	await expect(access(join(root, "workspaces", "workspace"))).rejects.toThrow();
	await expect(access(join(root, "sessions", managed.sessionFile))).rejects.toThrow();
	await expect(access(join(root, "workspaces", "other", "keep.txt"))).resolves.toBeUndefined();

	async function* content(value: string) { yield Buffer.from(value); }
	const uploaded = await uploadWorkspaceFile("workspace", "nested/note.txt", content("first"), false);
	expect(uploaded).toMatchObject({ path: "nested/note.txt", size_bytes: 5, created: true });
	expect(await listWorkspaceFiles("workspace")).toMatchObject([{ path: "nested/note.txt", size_bytes: 5 }]);
	await expect(uploadWorkspaceFile("workspace", "nested/note.txt", content("second"), false)).rejects.toBeInstanceOf(WorkspaceFileExistsError);
	await expect(uploadWorkspaceFile("workspace", "../outside.txt", content("blocked"), false)).rejects.toBeInstanceOf(InvalidWorkspaceFilePathError);
	const replaced = await uploadWorkspaceFile("workspace", "nested/note.txt", content("second"), true);
	expect(replaced).toMatchObject({ path: "nested/note.txt", size_bytes: 6, created: false });
	expect(await readFile(join(root, "workspaces", "workspace", "nested", "note.txt"), "utf8")).toBe("second");
	await deleteWorkspaceFile("workspace", "nested/note.txt");
	expect(await listWorkspaceFiles("workspace")).toEqual([]);
	await expect(access(join(root, "workspaces", "workspace", "nested"))).rejects.toThrow();
	await expect(deleteWorkspaceFile("workspace", "nested/note.txt")).rejects.toBeInstanceOf(WorkspaceFileNotFoundError);

	await mkdir(join(root, "workspaces", "workspace", "linked"), { recursive: true });
	await symlink(join(root, "workspaces", "other"), join(root, "workspaces", "workspace", "linked", "escape"));
	await expect(uploadWorkspaceFile("workspace", "linked/escape/outside.txt", content("blocked"), false)).rejects.toBeInstanceOf(UnsupportedWorkspaceFileTypeError);
});
