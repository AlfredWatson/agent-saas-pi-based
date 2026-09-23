import { expect, test } from "@playwright/test";

test("authenticated user reaches the three-pane Chinese workbench", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname.endsWith("/auth/login")) return json({ access_token: "browser-test-token", token_type: "bearer" });
    if (url.pathname.endsWith("/auth/me")) return json({ id: "user-1", email: "browser@example.com", status: "active" });
    if (url.pathname.endsWith("/workspaces")) return json({ items: [{ id: "workspace-1", name: "default", status: "active", is_current: true }] });
    if (url.pathname.endsWith("/sessions")) return json({ items: [] });
    if (url.pathname.endsWith("/agent-profiles")) return json({ items: [] });
    if (url.pathname.includes("/knowledge-bases")) return json({ items: [] });
    return json({});
  });

  await page.goto("/login");
  await page.getByLabel("邮箱").fill("browser@example.com");
  await page.getByLabel("密码").fill("very-long-browser-password");
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByText("智能体应用开发工具集")).toBeVisible();
  await expect(page.getByText("default", { exact: true })).toBeVisible();
  await expect(page.getByText("选择或创建会话")).toBeVisible();
  await expect(page.getByTitle("新聊天")).toBeVisible();
  await expect(page.getByText("工作区文件", { exact: true })).toBeVisible();
});
