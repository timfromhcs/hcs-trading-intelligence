/** Exact decimal-string arithmetic for display (no binary floats). */

const DECIMAL = /^\d+(\.\d+)?$/;

export function isDecimal(value: string): boolean {
  return DECIMAL.test(value);
}

function scale(value: string): { units: bigint; places: number } {
  const [whole, fraction = ""] = value.split(".");
  return { units: BigInt(whole + fraction), places: fraction.length };
}

/** Multiply two non-negative decimal strings exactly. Returns null on invalid input. */
export function multiplyDecimals(a: string, b: string): string | null {
  if (!isDecimal(a) || !isDecimal(b)) return null;
  const left = scale(a);
  const right = scale(b);
  const places = left.places + right.places;
  const digits = (left.units * right.units).toString().padStart(places + 1, "0");
  const whole = digits.slice(0, digits.length - places);
  const fraction = digits.slice(digits.length - places).replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole;
}

export function isPositive(value: string): boolean {
  return isDecimal(value) && /[1-9]/.test(value);
}
