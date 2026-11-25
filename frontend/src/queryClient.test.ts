import { describe, expect, it, vi } from "vitest";
import { createQueryClient, errorMessage } from "./queryClient";

describe("errorMessage", () => {
  it("prefers what the server said", () => {
    expect(errorMessage({ response: { data: { detail: "Project is archived" } } })).toBe("Project is archived");
  });
  it("falls back to the error text, also when detail is a validation list", () => {
    expect(errorMessage(new Error("Network Error"))).toBe("Error: Network Error");
    expect(errorMessage({ response: { data: { detail: [{ msg: "x" }] } } })).toContain("object");
  });
});

describe("a mutation that fails", () => {
  const fail = () => Promise.reject({ response: { data: { detail: "nope" } } });

  it("is reported, so the person is not left wondering", async () => {
    const report = vi.fn();
    const client = createQueryClient(report);
    await client.getMutationCache().build(client, { mutationFn: fail }).execute(undefined).catch(() => {});
    expect(report).toHaveBeenCalledWith("nope");
  });

  it("is not reported twice when it handles the failure itself", async () => {
    const report = vi.fn();
    const own = vi.fn();
    const client = createQueryClient(report);
    await client.getMutationCache().build(client, { mutationFn: fail, onError: own }).execute(undefined).catch(() => {});
    expect(own).toHaveBeenCalled();
    expect(report).not.toHaveBeenCalled();
  });
});
