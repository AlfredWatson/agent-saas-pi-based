import { resolve } from "node:path";

function positiveMegabytes(value: string | undefined, fallback: number): number {
	const megabytes = Number(value ?? fallback);
	if (!Number.isFinite(megabytes) || megabytes <= 0) throw new Error("workspace_file_limit_invalid");
	return Math.floor(megabytes * 1024 * 1024);
}

export const config = {
	dataRoot: resolve(process.env.RUNTIME_DATA_ROOT ?? "/runtime-data"),
	sharedSecret: process.env.RUNTIME_SHARED_SECRET ?? "shared-dev",
	dedicatedTenant: process.env.TENANT_ID,
	host: process.env.RUNTIME_HOST ?? "0.0.0.0",
	port: Number(process.env.RUNTIME_PORT ?? 3000),
	workspaceStorageLimitBytes: positiveMegabytes(process.env.WORKSPACE_STORAGE_LIMIT_MB, 1024),
	workspaceFileMaxBytes: positiveMegabytes(process.env.WORKSPACE_FILE_MAX_MB, 100),
};
