// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { MemoryResourceCreateInput } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { ResourceEditor } from "./Resources";

/**
 * N14-obs2: the create editor seeds every merge strategy knob empty. An empty knob
 * means "use the default" (the card says so, and the API omits it), so a form with only
 * the name filled must POST without a `merge` block and without a merge field error.
 * A knob set away from its default travels alone; an out-of-range knob disables the
 * button with its message instead of letting `create()` return silently.
 */

const memoryResourceCreate = vi.fn(async (input: MemoryResourceCreateInput) => ({ id: "mem-1", name: input.name }));

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      memoryResource: vi.fn(async () => null),
      memoryResources: vi.fn(async () => null),
      memoryResourceCreate: (input: MemoryResourceCreateInput) => memoryResourceCreate(input),
    },
  };
});
vi.mock("../../hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../hooks")>()),
  useV2Toast: () => () => undefined,
}));

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  memoryResourceCreate.mockClear(); // in a block: a returned mock would run as a cleanup hook
});
afterEach(cleanup);

const MERGE_ERROR = /Whole number between/;

function renderCreate() {
  return render(
    <MemoryRouter initialEntries={["/v2/memory?view=resource-new"]}>
      <ResourceEditor id={null} />
    </MemoryRouter>,
  );
}

describe("ResourceEditor create (N14-obs2)", () => {
  it("posts with only the name filled and sends no merge block", async () => {
    renderCreate();
    const button = screen.getByTestId("v2-memory-res-create") as HTMLButtonElement;
    // an untouched form is disabled for the missing name only, never for the empty knobs
    expect(button.disabled).toBe(true);
    expect(screen.queryByText(MERGE_ERROR)).toBeNull();

    fireEvent.change(screen.getByTestId("v2-memory-res-name"), { target: { value: "hr_memory_test" } });
    expect(button.disabled).toBe(false);
    expect(screen.queryByText(MERGE_ERROR)).toBeNull();

    fireEvent.click(button);
    await waitFor(() => expect(memoryResourceCreate).toHaveBeenCalledTimes(1));
    const body = memoryResourceCreate.mock.calls[0][0];
    expect(body).toEqual({
      name: "hr_memory_test",
      description: "",
      event_expiry_days: 30,
      strategies: ["semantic", "user_preference"],
    });
    expect(body).not.toHaveProperty("merge");
  });

  it("sends only the knob set away from its default", async () => {
    renderCreate();
    fireEvent.change(screen.getByTestId("v2-memory-res-name"), { target: { value: "hr_memory_test" } });
    fireEvent.change(screen.getByTestId("v2-memory-merge-max_facts_per_actor"), { target: { value: "500" } });
    expect(screen.queryByText(MERGE_ERROR)).toBeNull();

    fireEvent.click(screen.getByTestId("v2-memory-res-create"));
    await waitFor(() => expect(memoryResourceCreate).toHaveBeenCalledTimes(1));
    expect(memoryResourceCreate.mock.calls[0][0].merge).toEqual({ max_facts_per_actor: 500 });
  });

  it("disables the button with the field's message when a knob is out of range", () => {
    renderCreate();
    fireEvent.change(screen.getByTestId("v2-memory-res-name"), { target: { value: "hr_memory_test" } });
    fireEvent.change(screen.getByTestId("v2-memory-merge-recall_half_life_days"), { target: { value: "0" } });

    const button = screen.getByTestId("v2-memory-res-create") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe("Fix the merge strategy fields first.");
    expect(screen.getByText("Whole number between 1 and 3650, or empty.")).toBeTruthy();
    fireEvent.click(button);
    expect(memoryResourceCreate).not.toHaveBeenCalled();
  });
});
