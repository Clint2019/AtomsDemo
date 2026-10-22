import { describe, expect, it } from "vitest";
import { parseCnyMinorUnits } from "./money";

describe("CNY checkout amount", () => {
  it("converts fractional yuan to integer fen", () => {
    expect(parseCnyMinorUnits("0.01")).toBe(1);
    expect(parseCnyMinorUnits("0.50")).toBe(50);
    expect(parseCnyMinorUnits("10.23")).toBe(1023);
  });

  it("rejects zero, excess precision and unsafe amounts", () => {
    for (const input of ["0", "0.00", "1.001", "-1", "01", "999999999999999999999"])
      expect(parseCnyMinorUnits(input)).toBeNull();
  });
});
