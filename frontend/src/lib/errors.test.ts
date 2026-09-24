import { expect, test } from "vitest";
import { ApiError, readableError } from "./errors";

test("maps machine errors without losing their inspectable code", () => {
  const error = new ApiError(409, "session_busy");
  expect(readableError(error)).toContain("正在执行");
  expect(error.code).toBe("session_busy");
});

test("outdated Runtime asks for a rebuild", () => {
  expect(readableError(new ApiError(503, "runtime_update_required"))).toContain("重建");
});
