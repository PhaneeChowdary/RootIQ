import { expect, test } from "@playwright/test";

test("support agent signs in, creates an incident, and sees its audit event", async ({ page }) => {
  const title = `E2E database latency ${Date.now()}`;

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "RootIQ" })).toBeVisible();
  await page.getByLabel("Username").fill("e2e-agent");
  await page.getByLabel("Password").fill("e2e-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByRole("button", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText("RootIQ changes customer incidents and application logs into diagnoses")).toHaveCount(0);
  await page.getByRole("button", { name: "About" }).click();
  await expect(page.getByText("RootIQ changes customer incidents and application logs into diagnoses, response drafts and records. A person approves each step.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Controlled publishing" })).toBeVisible();
  await page.getByRole("button", { name: "Dashboard" }).click();
  await page.getByRole("button", { name: "New case" }).click();
  await page.getByPlaceholder("Case title").fill(title);
  await page.getByPlaceholder("Customer", { exact: true }).fill("Northwind");
  await page.getByPlaceholder("Affected service").fill("Checkout API");
  await page.getByPlaceholder("Customer-reported issue").fill("Checkout requests time out.");
  await page.getByPlaceholder("Paste logs, error messages, request IDs...").fill("ERROR query timeout request=e2e-42");
  await page.getByRole("button", { name: "Create case" }).click();

  await expect(page.locator(".notice-toast", { hasText: "Case created." })).toBeVisible();
  await page.locator(".notice-toast", { hasText: "Case created." }).getByRole("button", { name: "Close message" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page.getByRole("button", { name: "Insights" }).click();
  await expect(page.locator(".audit")).toContainText("Case created");
  await expect(page.locator(".audit")).toContainText("e2e-agent");
  await page.getByRole("button", { name: "Dashboard" }).click();

  await page.getByRole("button", { name: `Delete ${title}` }).click();
  await expect(page.getByRole("alertdialog", { name: "Confirm case delete" })).toContainText(title);
  await page.getByRole("alertdialog", { name: "Confirm case delete" }).getByRole("button", { name: "Move to bin" }).click();
  await expect(page.locator(".notice-toast", { hasText: "Case moved to the recycle bin." })).toBeVisible();
  await page.locator(".notice-toast", { hasText: "Case moved to the recycle bin." }).getByRole("button", { name: "Close message" }).click();
  await expect(page.getByRole("button", { name: `Delete ${title}` })).toHaveCount(0);

  // The recycle bin: restore the case, then delete it forever.
  const bin = page.locator(".bin-group");
  await bin.locator("summary").click();
  await bin.locator(".bin-row", { hasText: title }).getByRole("button", { name: "Restore" }).click();
  await expect(page.getByRole("button", { name: `Delete ${title}` })).toHaveCount(1);
  await page.getByRole("button", { name: `Delete ${title}` }).click();
  await page.getByRole("alertdialog", { name: "Confirm case delete" }).getByRole("button", { name: "Move to bin" }).click();
  await page.locator(".notice-toast").last().getByRole("button", { name: "Close message" }).click();
  await bin.locator("summary").click();
  await bin.getByRole("button", { name: `Delete ${title} forever` }).click();
  await page.getByRole("alertdialog", { name: "Confirm delete forever" }).getByRole("button", { name: "Delete forever" }).click();
  await expect(page.locator(".notice-toast", { hasText: "Case deleted forever." })).toBeVisible();
  await page.locator(".notice-toast", { hasText: "Case deleted forever." }).getByRole("button", { name: "Close message" }).click();
  await expect(page.locator(".bin-row", { hasText: title })).toHaveCount(0);

  await page.getByRole("button", { name: "Insights" }).click();
  await expect(page.locator(".audit")).toContainText("Case moved to bin");
  await expect(page.locator(".audit")).toContainText("Case restored");

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("alertdialog", { name: "Confirm sign out" })).toContainText("Sign out of RootIQ?");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Dashboard" })).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByRole("alertdialog", { name: "Confirm sign out" }).getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});
