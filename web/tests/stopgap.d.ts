/**
 * STOPGAP. Delete when M00 adds @types/node. Declares only what the tests use, so the
 * tests can be strictly type-checked with the current dependencies.
 */
declare module "node:test" {
  type TestFn = () => void | Promise<void>;
  export function test(name: string, run: TestFn): void;
  export function describe(name: string, run: () => void): void;
}

declare module "node:assert/strict" {
  interface Assert {
    (value: unknown, message?: string): asserts value;
    ok(value: unknown, message?: string): asserts value;
    equal(actual: unknown, expected: unknown, message?: string): void;
    notEqual(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    match(value: string, pattern: RegExp, message?: string): void;
    doesNotMatch(value: string, pattern: RegExp, message?: string): void;
  }
  const assert: Assert;
  export default assert;
}
