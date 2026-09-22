import { expect, test } from "vitest";
import { ApiError, readableError } from "./errors";

test("maps machine errors without losing their inspectable code", () => {
  const error = new ApiError(409, "session_busy");
  expect(readableError(error)).toContain("正在执行");
  expect(error.code).toBe("session_busy");
});
