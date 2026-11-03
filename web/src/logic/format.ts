/** Formats a recorded amount for reading. The minor-unit value stays beside it on screen. */
export function formatMinor(minor: number, currency: string): string {
  try {
    const format = new Intl.NumberFormat("en-IN", { style: "currency", currency, currencyDisplay: "code" });
    const digits = format.resolvedOptions().maximumFractionDigits ?? 2;
    return format.format(minor / 10 ** digits).replace(/ /g, " ");
  } catch {
    return `${minor} minor units of ${currency}`;
  }
}

export function formatTimestamp(value: string): string {
  const time = Date.parse(value);
  if (Number.isNaN(time)) return value;
  return new Date(time).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}

export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

export function shortDigest(value: string): string {
  return value.length > 16 ? `${value.slice(0, 12)}…${value.slice(-4)}` : value;
}

export function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? String(value);
}
