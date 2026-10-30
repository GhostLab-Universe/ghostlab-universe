/**
 * STOPGAP. Delete this file when M00 adds @types/react and @types/react-dom.
 *
 * web/package.json has no React type packages, and React 19 ships none, so a strict
 * check of web/src can't pass without declarations. This covers only what the UI uses,
 * with the same names and signatures as @types/react, so deleting it should need no
 * source changes. It is not a fork of those types and has no runtime effect.
 */
declare module "react" {
  export type Key = string | number;
  export type ReactNode = string | number | bigint | boolean | null | undefined | ReactElement | Iterable<ReactNode>;
  export interface ReactElement {
    type: unknown;
    props: unknown;
    key: string | null;
  }
  export type SetStateAction<S> = S | ((previous: S) => S);
  export type Dispatch<A> = (value: A) => void;
  export interface RefObject<T> {
    current: T;
  }
  export type Ref<T> = RefObject<T | null> | ((instance: T | null) => void) | null;
  export type DependencyList = readonly unknown[];
  export type ReducerAction<A> = Dispatch<A>;

  export function useState<S>(initial: S | (() => S)): [S, Dispatch<SetStateAction<S>>];
  export function useEffect(effect: () => void | (() => void), deps?: DependencyList): void;
  export function useRef<T>(initial: T): RefObject<T>;
  export function useMemo<T>(factory: () => T, deps: DependencyList): T;
  export function useCallback<T extends (...args: never[]) => unknown>(callback: T, deps: DependencyList): T;
  export function useId(): string;
  export function useSyncExternalStore<S>(
    subscribe: (onChange: () => void) => () => void,
    getSnapshot: () => S,
    getServerSnapshot?: () => S,
  ): S;

  export const StrictMode: (props: { children?: ReactNode }) => ReactElement;
  export const Fragment: (props: { children?: ReactNode }) => ReactElement;

  export interface SyntheticEvent<T = Element> {
    currentTarget: T;
    target: EventTarget;
    preventDefault(): void;
  }
  export type FormEvent<T = Element> = SyntheticEvent<T>;
  export type ChangeEvent<T = Element> = SyntheticEvent<T>;
  export type MouseEvent<T = Element> = SyntheticEvent<T>;

  interface AriaAttributes {
    "aria-label"?: string;
    "aria-labelledby"?: string;
    "aria-describedby"?: string;
    "aria-current"?: "page" | "step" | "location" | "true" | "false" | boolean;
    "aria-live"?: "off" | "polite" | "assertive";
    "aria-atomic"?: boolean;
    "aria-busy"?: boolean;
    "aria-invalid"?: boolean;
    "aria-hidden"?: boolean;
  }

  export interface HTMLAttributes<T> extends AriaAttributes {
    children?: ReactNode;
    className?: string;
    id?: string;
    key?: Key | null;
    role?: string;
    hidden?: boolean;
    title?: string;
    tabIndex?: number;
    lang?: string;
    ref?: Ref<T>;
    onClick?: (event: MouseEvent<T>) => void;
    "data-tone"?: string;
    "data-testid"?: string;
    "data-side"?: string;
    "data-kind"?: string;
  }
  export interface AnchorAttributes extends HTMLAttributes<HTMLAnchorElement> {
    href?: string;
    download?: string;
  }
  export interface ButtonAttributes extends HTMLAttributes<HTMLButtonElement> {
    type?: "button" | "submit" | "reset";
    disabled?: boolean;
  }
  export interface FormAttributes extends HTMLAttributes<HTMLFormElement> {
    onSubmit?: (event: FormEvent<HTMLFormElement>) => void;
  }
  export interface InputAttributes extends HTMLAttributes<HTMLInputElement> {
    type?: string;
    name?: string;
    value?: string;
    required?: boolean;
    pattern?: string;
    maxLength?: number;
    autoComplete?: string;
    spellCheck?: boolean;
    disabled?: boolean;
    onChange?: (event: ChangeEvent<HTMLInputElement>) => void;
  }
  export interface TextareaAttributes extends HTMLAttributes<HTMLTextAreaElement> {
    name?: string;
    value?: string;
    rows?: number;
    maxLength?: number;
    disabled?: boolean;
    onChange?: (event: ChangeEvent<HTMLTextAreaElement>) => void;
  }
  export interface SelectAttributes extends HTMLAttributes<HTMLSelectElement> {
    name?: string;
    value?: string;
    disabled?: boolean;
    onChange?: (event: ChangeEvent<HTMLSelectElement>) => void;
  }
  export interface OptionAttributes extends HTMLAttributes<HTMLOptionElement> {
    value?: string;
  }
  export interface LabelAttributes extends HTMLAttributes<HTMLLabelElement> {
    htmlFor?: string;
  }
  export interface TableCellAttributes extends HTMLAttributes<HTMLTableCellElement> {
    scope?: "col" | "row" | "colgroup" | "rowgroup";
    colSpan?: number;
  }

  export namespace JSX {
    type Element = ReactElement;
    interface ElementChildrenAttribute {
      children: unknown;
    }
    interface IntrinsicAttributes {
      key?: Key | null;
    }
    interface IntrinsicElements {
      a: AnchorAttributes;
      article: HTMLAttributes<HTMLElement>;
      aside: HTMLAttributes<HTMLElement>;
      button: ButtonAttributes;
      caption: HTMLAttributes<HTMLElement>;
      code: HTMLAttributes<HTMLElement>;
      dd: HTMLAttributes<HTMLElement>;
      details: HTMLAttributes<HTMLDetailsElement> & { open?: boolean };
      div: HTMLAttributes<HTMLDivElement>;
      dl: HTMLAttributes<HTMLDListElement>;
      dt: HTMLAttributes<HTMLElement>;
      em: HTMLAttributes<HTMLElement>;
      fieldset: HTMLAttributes<HTMLFieldSetElement>;
      footer: HTMLAttributes<HTMLElement>;
      form: FormAttributes;
      h1: HTMLAttributes<HTMLHeadingElement>;
      h2: HTMLAttributes<HTMLHeadingElement>;
      h3: HTMLAttributes<HTMLHeadingElement>;
      header: HTMLAttributes<HTMLElement>;
      input: InputAttributes;
      label: LabelAttributes;
      legend: HTMLAttributes<HTMLLegendElement>;
      li: HTMLAttributes<HTMLLIElement>;
      main: HTMLAttributes<HTMLElement>;
      nav: HTMLAttributes<HTMLElement>;
      ol: HTMLAttributes<HTMLOListElement>;
      option: OptionAttributes;
      p: HTMLAttributes<HTMLParagraphElement>;
      pre: HTMLAttributes<HTMLPreElement>;
      section: HTMLAttributes<HTMLElement>;
      select: SelectAttributes;
      small: HTMLAttributes<HTMLElement>;
      span: HTMLAttributes<HTMLSpanElement>;
      strong: HTMLAttributes<HTMLElement>;
      summary: HTMLAttributes<HTMLElement>;
      table: HTMLAttributes<HTMLTableElement>;
      tbody: HTMLAttributes<HTMLTableSectionElement>;
      td: TableCellAttributes;
      textarea: TextareaAttributes;
      th: TableCellAttributes;
      thead: HTMLAttributes<HTMLTableSectionElement>;
      tr: HTMLAttributes<HTMLTableRowElement>;
      ul: HTMLAttributes<HTMLUListElement>;
    }
  }
}

declare module "react/jsx-runtime" {
  import type { JSX as ReactJSX, ReactElement } from "react";
  export { JSX } from "react";
  export function jsx(type: unknown, props: unknown, key?: string): ReactElement;
  export function jsxs(type: unknown, props: unknown, key?: string): ReactElement;
  export const Fragment: unknown;
  export type { ReactJSX };
}

declare module "react-dom/client" {
  import type { ReactNode } from "react";
  export function createRoot(container: Element): { render(node: ReactNode): void; unmount(): void };
}

declare module "react-dom/server" {
  import type { ReactNode } from "react";
  export function renderToStaticMarkup(node: ReactNode): string;
}
