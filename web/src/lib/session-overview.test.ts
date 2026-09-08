import { describe, expect, it } from "vitest";
import { selectOverviewSessions } from "./session-overview";

type Row = { id: string; is_active?: boolean };

const row = (id: string, is_active = false): Row => ({ id, is_active });

describe("selectOverviewSessions", () => {
  it("keeps live sessions instead of filtering them out (regression)", () => {
    // The original implementation was `.filter((s) => !s.is_active)`, which
    // hid exactly the session the user was sitting in.
    const picked = selectOverviewSessions([row("live", true)]);
    expect(picked.map((s) => s.id)).toEqual(["live"]);
  });

  it("puts live sessions ahead of finished ones", () => {
    const picked = selectOverviewSessions([
      row("idle-1"),
      row("live-1", true),
      row("idle-2"),
      row("live-2", true),
    ]);
    expect(picked.map((s) => s.id)).toEqual([
      "live-1",
      "live-2",
      "idle-1",
      "idle-2",
    ]);
  });

  it("preserves server order within each group", () => {
    const picked = selectOverviewSessions([
      row("a"),
      row("b"),
      row("c"),
    ]);
    expect(picked.map((s) => s.id)).toEqual(["a", "b", "c"]);
  });

  it("caps the result at the limit, live sessions winning the slots", () => {
    const picked = selectOverviewSessions(
      [
        row("idle-1"),
        row("idle-2"),
        row("idle-3"),
        row("live-1", true),
        row("live-2", true),
      ],
      3,
    );
    expect(picked.map((s) => s.id)).toEqual(["live-1", "live-2", "idle-1"]);
  });

  it("treats a missing is_active flag as not live", () => {
    const picked = selectOverviewSessions([{ id: "unknown" }, row("live", true)]);
    expect(picked.map((s) => s.id)).toEqual(["live", "unknown"]);
  });

  it("handles an empty list and a zero limit without throwing", () => {
    expect(selectOverviewSessions([])).toEqual([]);
    expect(selectOverviewSessions([row("live", true)], 0)).toEqual([]);
    expect(selectOverviewSessions([row("live", true)], -1)).toEqual([]);
  });

  it("does not mutate the input array", () => {
    const input = [row("idle"), row("live", true)];
    const snapshot = input.map((s) => s.id);
    selectOverviewSessions(input);
    expect(input.map((s) => s.id)).toEqual(snapshot);
  });
});
