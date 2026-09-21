import { resolve } from "node:path";

const ragSharedSecret = process.env.RUNTIME_RAG_SHARED_SECRET ?? "";
// Pi's bash tool inherits this process environment. Keep the secret only in
// the Runtime process after startup; Docker-level inspection remains the
// documented trust boundary for this deployment model.
delete process.env.RUNTIME_RAG_SHARED_SECRET;

function positiveMegabytes(value: string | undefined, fallback: number): number {
	const megabytes = Number(value ?? fallback);
	if (!Number.isFinite(megabytes) || megabytes <= 0) throw new Error("workspace_file_limit_invalid");
	return Math.floor(megabytes * 1024 * 1024);
}

function positiveBytes(value: string | undefined, fallback: number): number {
	const bytes = Number(value ?? fallback);
	if (!Number.isFinite(bytes) || bytes < 1024 || bytes > 256 * 1024) throw new Error("runtime_rag_result_max_bytes_invalid");
	return Math.floor(bytes);
}

function positiveSeconds(value: string | undefined, fallback: number): number {
	const seconds = Number(value ?? fallback);
	if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 120) throw new Error("runtime_rag_request_timeout_invalid");
	return Math.floor(seconds);
}

function gatewayBaseUrl(value: string | undefined): string | undefined {
	if (!value) return undefined;
	const url = new URL(value);
	if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password || url.search || url.hash) throw new Error("runtime_gateway_base_url_invalid");
	return url.toString().replace(/\/$/, "");
}

export const config = {
	dataRoot: resolve(process.env.RUNTIME_DATA_ROOT ?? "/runtime-data"),
	sharedSecret: process.env.RUNTIME_SHARED_SECRET ?? "shared-dev",
	dedicatedTenant: process.env.TENANT_ID,
	host: process.env.RUNTIME_HOST ?? "0.0.0.0",
	port: Number(process.env.RUNTIME_PORT ?? 3000),
	workspaceStorageLimitBytes: positiveMegabytes(process.env.WORKSPACE_STORAGE_LIMIT_MB, 1024),
	workspaceFileMaxBytes: positiveMegabytes(process.env.WORKSPACE_FILE_MAX_MB, 100),
	ragSharedSecret,
	gatewayBaseUrl: gatewayBaseUrl(process.env.RUNTIME_GATEWAY_BASE_URL),
	ragRequestTimeoutMs: positiveSeconds(process.env.RUNTIME_RAG_REQUEST_TIMEOUT_SECONDS, 30) * 1000,
	ragResultMaxBytes: positiveBytes(process.env.RUNTIME_RAG_RESULT_MAX_BYTES, 64 * 1024),
};
