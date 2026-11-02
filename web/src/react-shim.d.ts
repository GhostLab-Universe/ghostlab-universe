/* Interim minimal React typings for Track D (no JSX syntax is used).
 * M00 has not pinned @types/react yet; this shim covers only the stable APIs
 * the guided UI calls. Request the real types from M00 and delete this file.
 * Runtime behavior always comes from the installed react/react-dom packages. */

declare module "react" {
  export type ReactNode =
    | string
    | number
    | boolean
    | null
    | undefined
    | ReactElement
    | readonly ReactNode[];
  export interface ReactElement {
    readonly __trackDUint: unique symbol;
  }
  export function createElement(
    type: string,
    props: Record<string, unknown> | null,
    ...children: readonly ReactNode[]
  ): ReactElement;
  export function createElement<P extends Record<string, unknown>>(
    type: (props: P) => ReactNode,
    props: P,
  ): ReactElement;
  export function useState<S>(initial: S | (() => S)): [S, (next: S | ((prev: S) => S)) => void];
  export function useRef<T>(initial: T): { current: T };
  export function useEffect(effect: () => void | (() => void), deps?: readonly unknown[]): void;
  export function useMemo<T>(factory: () => T, deps: readonly unknown[]): T;
}

declare module "*.css";

declare module "react-dom/client" {
  import type { ReactNode } from "react";
  export interface Root {
    render(node: ReactNode): void;
    unmount(): void;
  }
  export function createRoot(container: Element): Root;
}
