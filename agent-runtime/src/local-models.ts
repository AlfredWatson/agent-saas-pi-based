import { lookup } from "node:dns/promises";
import { isIP } from "node:net";

export type LocalProvider = "vllm" | "sglang";
export type LocalModel = {
	id: string;
	name: string;
	context_window: number;
	max_tokens: number;
	reasoning: boolean;
};

export class LocalModelError extends Error {
	constructor(message: "invalid_model_base_url" | "model_service_unavailable" | "model_service_auth_failed" | "invalid_model_catalog") {
		super(message);
	}
}

export function isLocalProvider(value: string): value is LocalProvider {
	return value === "vllm" || value === "sglang";
}

function privateAddress(address: string): boolean {
	if (isIP(address) === 4) {
		const parts = address.split(".").map(Number);
		return parts[0] === 10 || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) || (parts[0] === 192 && parts[1] === 168);
	}
	if (isIP(address) === 6) return /^(fc|fd)[0-9a-f]{2}:/i.test(address);
	return false;
}

export async function localApiBase(value: string): Promise<string> {
	let url: URL;
	try { url = new URL(value); } catch { throw new LocalModelError("invalid_model_base_url"); }
	if (!(["http:", "https:"].includes(url.protocol)) || !url.hostname || url.username || url.password || url.search || url.hash || !["/", "/v1", "/v1/"].includes(url.pathname)) {
		throw new LocalModelError("invalid_model_base_url");
	}
	const hostname = url.hostname.replace(/^\[|\]$/g, "");
	if (hostname.toLowerCase() === "localhost") throw new LocalModelError("invalid_model_base_url");
	let addresses: string[];
	try { addresses = isIP(hostname) ? [hostname] : (await lookup(hostname, { all: true })).map((entry) => entry.address); }
	catch { throw new LocalModelError("model_service_unavailable"); }
	if (!addresses.length || addresses.some((address) => !privateAddress(address))) throw new LocalModelError("invalid_model_base_url");
	return `${url.origin}/v1`;
}

export async function discoverLocalModels(baseUrl: string, apiKey: string): Promise<{ base_url: string; models: { id: string; name: string }[] }> {
	const base = await localApiBase(baseUrl);
	let response: Response;
	try {
		response = await fetch(`${base}/models`, {
			headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
			redirect: "error",
			signal: AbortSignal.timeout(10_000),
		});
	} catch { throw new LocalModelError("model_service_unavailable"); }
	if (response.status === 401 || response.status === 403) throw new LocalModelError("model_service_auth_failed");
	if (!response.ok) throw new LocalModelError("model_service_unavailable");
	let payload: unknown;
	try { payload = await response.json(); } catch { throw new LocalModelError("invalid_model_catalog"); }
	const data = (payload as { data?: unknown })?.data;
	if (!Array.isArray(data) || data.length === 0 || data.length > 500) throw new LocalModelError("invalid_model_catalog");
	const seen = new Set<string>();
	const models = data.map((entry: unknown) => {
		const item = entry as { id?: unknown; name?: unknown };
		if (typeof item?.id !== "string" || !item.id.trim() || item.id.length > 256 || seen.has(item.id)) throw new LocalModelError("invalid_model_catalog");
		seen.add(item.id);
		return { id: item.id, name: typeof item.name === "string" && item.name.trim() ? item.name.slice(0, 256) : item.id };
	});
	return { base_url: base, models };
}

export function localRuntimeProviderId(bindingId: string): string {
	if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(bindingId)) throw new Error("invalid_binding");
	return `local-${bindingId.toLowerCase()}`;
}

const guardedOrigins = new Set<string>();
let guardedFetch: typeof fetch | undefined;

/** Pi's OpenAI client uses global fetch. Guard only registered local endpoints. */
export function guardLocalModelFetch(baseUrl: string): void {
	guardedOrigins.add(new URL(baseUrl).origin);
	if (globalThis.fetch === guardedFetch) return;
	const delegate = globalThis.fetch.bind(globalThis);
	guardedFetch = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
		const url = new URL(typeof input === "string" || input instanceof URL ? input.toString() : input.url);
		if (!guardedOrigins.has(url.origin) || !(url.pathname === "/v1" || url.pathname.startsWith("/v1/"))) return delegate(input, init);
		await localApiBase(url.origin);
		return delegate(input, { ...init, redirect: "error" });
	}) as typeof fetch;
	globalThis.fetch = guardedFetch;
}
