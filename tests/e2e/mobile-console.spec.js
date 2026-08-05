import { expect, test } from "@playwright/test";

test("mobile console has no horizontal overflow and exposes touch controls", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: "音声入力を開始" })).toBeVisible();
  await expect(page.getByRole("button", { name: "音声入力を停止" })).toBeVisible();
  await expect(page.locator(".keycap").first()).toBeHidden();

  const metrics = await page.evaluate(() => ({
    bodyWidth: document.body.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
  }));
  expect(metrics.bodyWidth).toBeLessThanOrEqual(metrics.viewportWidth);

  for (const name of ["音声入力を開始", "音声入力を停止"]) {
    const box = await page.getByRole("button", { name }).boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
    expect(box?.width).toBeGreaterThanOrEqual(44);
  }
  await expect(page.getByRole("heading", { name: "会話" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "アクティビティ" })).toBeVisible();
});
