/**
 * STOPGAP. Not an M07 contract. Delete this file when `coverage` is in openapi.v1.json.
 *
 * M05 records `coverage` on every experiment at feature/m05-world-campaigns a785d86
 * (Controller._execute). M07's contract at df600e0 has no such field, so no field
 * here is generated. Reading goes through logic/coverage.ts, which treats it as unknown
 * input, and a service that doesn't send it is shown as "not reported".
 */
export interface Coverage {
  baseline: string;
  fork: string;
  complete: boolean;
}

export type WithCoverage<T> = T & { coverage?: Coverage };
