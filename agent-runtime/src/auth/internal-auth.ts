import type { FastifyRequest } from "fastify";
import { config } from "../config.js";

export function tenantFrom(request: FastifyRequest): string {
	if (request.headers.authorization !== `Bearer ${config.sharedSecret}`) throw new Error("unauthorized");
	const tenant = request.headers["x-tenant-id"];
	if (typeof tenant !== "string" || !/^[0-9a-f-]{36}$/i.test(tenant)) throw new Error("invalid tenant");
	if (config.dedicatedTenant && tenant !== config.dedicatedTenant) throw new Error("tenant mismatch");
	return tenant;
}
