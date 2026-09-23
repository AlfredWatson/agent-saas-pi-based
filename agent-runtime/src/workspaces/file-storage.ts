import { createReadStream } from "node:fs";
import { mkdir, mkdtemp, lstat, readdir, rename, rm, rmdir, unlink, open } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { config } from "../config.js";

export class InvalidWorkspaceFilePathError extends Error {
	constructor() { super("invalid_file_path"); }
}

export class UnsupportedWorkspaceFileTypeError extends Error {
	constructor() { super("unsupported_file_type"); }
}

export class WorkspaceFileExistsError extends Error {
	constructor() { super("file_exists"); }
}

export class WorkspaceFileNotFoundError extends Error {
	constructor() { super("file_not_found"); }
}

export class WorkspaceStorageLimitError extends Error {
	constructor() { super("workspace_storage_limit_reached"); }
}

export class WorkspaceFileTooLargeError extends Error {
	constructor() { super("file_too_large"); }
}

export type WorkspaceFile = { path: string; size_bytes: number; modified_at: string };
export type UploadedWorkspaceFile = WorkspaceFile & { created: boolean };
export type DownloadedWorkspaceFile = { content: ReturnType<typeof createReadStream>; size_bytes: number };

const root = resolve(config.dataRoot);
let mutationTail: Promise<void> = Promise.resolve();

function runtimePath(...parts: string[]): string {
	const path = resolve(root, ...parts);
	if (path !== root && !path.startsWith(`${root}/`)) throw new Error("unsafe path");
	return path;
}

function workspaceRoot(workspaceKey: string): string {
	if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(workspaceKey)) throw new InvalidWorkspaceFilePathError();
	return runtimePath("workspaces", workspaceKey);
}

function pathSegments(value: string): string[] {
	if (!value || value.length > 1024 || value.includes("\0") || value.startsWith("/") || value.includes("\\")) throw new InvalidWorkspaceFilePathError();
	const segments = value.split("/");
	if (segments.some((segment) => !segment || segment === "." || segment === ".." || Buffer.byteLength(segment) > 255)) throw new InvalidWorkspaceFilePathError();
	return segments;
}

function targetFor(workspaceKey: string, value: string): { root: string; target: string; segments: string[] } {
	const workspace = workspaceRoot(workspaceKey);
	const segments = pathSegments(value);
	const target = resolve(workspace, ...segments);
	if (!target.startsWith(`${workspace}/`)) throw new InvalidWorkspaceFilePathError();
	return { root: workspace, target, segments };
}

async function lstatOrMissing(path: string) {
	try {
		return await lstat(path);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
		throw error;
	}
}

async function assertSafeParents(workspace: string, segments: string[]): Promise<void> {
	const workspaceStat = await lstatOrMissing(workspace);
	if (workspaceStat?.isSymbolicLink() || (workspaceStat && !workspaceStat.isDirectory())) throw new UnsupportedWorkspaceFileTypeError();
	let current = workspace;
	for (const segment of segments.slice(0, -1)) {
		current = join(current, segment);
		const entry = await lstatOrMissing(current);
		if (!entry) return;
		if (entry.isSymbolicLink() || !entry.isDirectory()) throw new UnsupportedWorkspaceFileTypeError();
	}
}

function render(value: string, stat: { size: number; mtime: Date }): WorkspaceFile {
	return { path: value, size_bytes: stat.size, modified_at: stat.mtime.toISOString() };
}

async function regularFile(path: string) {
	const entry = await lstatOrMissing(path);
	if (!entry) return undefined;
	if (entry.isSymbolicLink() || !entry.isFile()) throw new UnsupportedWorkspaceFileTypeError();
	return entry;
}

async function workspaceUsageBytes(directory: string): Promise<number> {
	const rootStat = await lstatOrMissing(directory);
	if (!rootStat) return 0;
	if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) throw new UnsupportedWorkspaceFileTypeError();
	let total = 0;
	const pending = [directory];
	while (pending.length) {
		const current = pending.pop()!;
		const currentStat = await lstatOrMissing(current);
		if (!currentStat || currentStat.isSymbolicLink() || !currentStat.isDirectory()) continue;
		for (const entry of await readdir(current, { withFileTypes: true })) {
			const path = join(current, entry.name);
			if (entry.isDirectory()) pending.push(path);
			else if (entry.isFile()) {
				const stat = await lstat(path);
				if (stat.isFile()) total += stat.size;
			}
		}
	}
	return total;
}

async function writeToTemporaryFile(source: AsyncIterable<Uint8Array>): Promise<{ directory: string; path: string; size: number }> {
	const temporaryRoot = runtimePath("tmp", "workspace-uploads");
	await mkdir(temporaryRoot, { recursive: true });
	const directory = await mkdtemp(join(temporaryRoot, "upload-"));
	const path = join(directory, "content");
	const handle = await open(path, "wx");
	let size = 0;
	try {
		for await (const chunk of source) {
			const bytes = Buffer.from(chunk);
			size += bytes.length;
			if (size > config.workspaceFileMaxBytes) throw new WorkspaceFileTooLargeError();
			await handle.write(bytes);
		}
		await handle.sync();
		return { directory, path, size };
	} catch (error) {
		await rm(directory, { recursive: true, force: true });
		throw error;
	} finally {
		await handle.close();
	}
}

async function withMutationLock<T>(action: () => Promise<T>): Promise<T> {
	let release!: () => void;
	const previous = mutationTail;
	mutationTail = new Promise<void>((resolve) => { release = resolve; });
	await previous;
	try {
		return await action();
	} finally {
		release();
	}
}

export async function listWorkspaceFiles(workspaceKey: string): Promise<WorkspaceFile[]> {
	const workspace = workspaceRoot(workspaceKey);
	const rootStat = await lstatOrMissing(workspace);
	if (!rootStat) return [];
	if (rootStat.isSymbolicLink() || !rootStat.isDirectory()) throw new UnsupportedWorkspaceFileTypeError();
	const items: WorkspaceFile[] = [];
	const pending: Array<{ directory: string; prefix: string }> = [{ directory: workspace, prefix: "" }];
	while (pending.length) {
		const { directory, prefix } = pending.pop()!;
		const directoryStat = await lstatOrMissing(directory);
		if (!directoryStat || directoryStat.isSymbolicLink() || !directoryStat.isDirectory()) continue;
		const entries = await readdir(directory, { withFileTypes: true });
		for (const entry of entries) {
			const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
			const path = join(directory, entry.name);
			if (entry.isDirectory()) pending.push({ directory: path, prefix: relative });
			else if (entry.isFile()) {
				const stat = await lstat(path);
				if (stat.isFile()) items.push(render(relative, stat));
			}
		}
	}
	return items.sort((left, right) => left.path.localeCompare(right.path));
}

/** Open a regular workspace file only after applying the same path and
 * symlink protections used by upload and deletion. */
export async function downloadWorkspaceFile(workspaceKey: string, value: string): Promise<DownloadedWorkspaceFile> {
	const { root: workspace, target, segments } = targetFor(workspaceKey, value);
	await assertSafeParents(workspace, segments);
	const entry = await regularFile(target);
	if (!entry) throw new WorkspaceFileNotFoundError();
	return { content: createReadStream(target), size_bytes: entry.size };
}

export async function uploadWorkspaceFile(workspaceKey: string, value: string, source: AsyncIterable<Uint8Array>, overwrite: boolean): Promise<UploadedWorkspaceFile> {
	return withMutationLock(async () => {
		const { root: workspace, target, segments } = targetFor(workspaceKey, value);
		await assertSafeParents(workspace, segments);
		const existing = await regularFile(target);
		if (existing && !overwrite) throw new WorkspaceFileExistsError();
		const temporary = await writeToTemporaryFile(source);
		try {
			const usage = await workspaceUsageBytes(runtimePath("workspaces"));
			if (usage - (existing?.size ?? 0) + temporary.size > config.workspaceStorageLimitBytes) throw new WorkspaceStorageLimitError();
			await mkdir(dirname(target), { recursive: true });
			await assertSafeParents(workspace, segments);
			const destination = await regularFile(target);
			if (destination && !overwrite) throw new WorkspaceFileExistsError();
			if ((destination?.size ?? 0) !== (existing?.size ?? 0)) {
				const currentUsage = await workspaceUsageBytes(runtimePath("workspaces"));
				if (currentUsage - (destination?.size ?? 0) + temporary.size > config.workspaceStorageLimitBytes) throw new WorkspaceStorageLimitError();
			}
			await rename(temporary.path, target);
			return { ...render(value, await lstat(target)), created: !destination };
		} finally {
			await rm(temporary.directory, { recursive: true, force: true });
		}
	});
}

export async function deleteWorkspaceFile(workspaceKey: string, value: string): Promise<void> {
	return withMutationLock(async () => {
		const { root: workspace, target, segments } = targetFor(workspaceKey, value);
		await assertSafeParents(workspace, segments);
		if (!(await regularFile(target))) throw new WorkspaceFileNotFoundError();
		await unlink(target);
		let current = dirname(target);
		while (current !== workspace) {
			try {
				await rmdir(current);
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code === "ENOTEMPTY" || (error as NodeJS.ErrnoException).code === "ENOENT") break;
				throw error;
			}
			current = dirname(current);
		}
	});
}
