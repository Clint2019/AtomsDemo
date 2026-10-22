export function formatMoney(minorUnits: number, currency: string): string {
  try {
    const format = new Intl.NumberFormat("zh-CN", { style: "currency", currency });
    const digits = format.resolvedOptions().maximumFractionDigits ?? 2;
    return format.format(minorUnits / 10 ** digits);
  } catch {
    return `${minorUnits} ${currency}（最小货币单位）`;
  }
}

export function parseCnyMinorUnits(input: string): number | null {
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(input)) return null;
  const [yuan, fraction = ""] = input.split(".");
  const minorUnits = Number(yuan) * 100 + Number(fraction.padEnd(2, "0"));
  return Number.isSafeInteger(minorUnits) && minorUnits > 0 ? minorUnits : null;
}
