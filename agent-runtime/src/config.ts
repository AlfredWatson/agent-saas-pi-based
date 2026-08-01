import { resolve } from "node:path";

export const config = {
	dataRoot: resolve(process.env.RUNTIME_DATA_ROOT ?? ".runtime-data"),
	sharedSecret: process.env.RUNTIME_SHARED_SECRET ?? "shared-dev",
	dedicatedTenant: process.env.TENANT_ID,
	host: process.env.RUNTIME_HOST ?? "127.0.0.1",
	toolProfile: (): string => process.env.RUNTIME_TOOL_PROFILE ?? "readonly",
	port: Number(process.env.RUNTIME_PORT ?? 3000),
};
