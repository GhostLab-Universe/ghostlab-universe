export interface PathDifference {
  path: string;
  baseline: unknown;
  fork: unknown;
}

const LIMIT = 200;

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function walk(a: unknown, b: unknown, path: string, out: PathDifference[]): void {
  if (out.length >= LIMIT) return;
  if (Array.isArray(a) && Array.isArray(b)) {
    const length = Math.max(a.length, b.length);
    for (let index = 0; index < length; index += 1) {
      walk(a[index], b[index], `${path}[${index}]`, out);
    }
    return;
  }
  if (isObject(a) && isObject(b)) {
    for (const key of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()) {
      walk(a[key], b[key], path === "" ? key : `${path}.${key}`, out);
    }
    return;
  }
  if (a !== b) out.push({ path, baseline: a, fork: b });
}

/**
 * Where two recorded values differ, to point a reader at the evidence. A display aid
 * only: it says nothing about which side is right. Verdicts come from the verifier.
 * An undefined side means the path is absent on that side.
 */
export function differences(baseline: unknown, fork: unknown): PathDifference[] {
  const out: PathDifference[] = [];
  walk(baseline, fork, "", out);
  return out;
}
