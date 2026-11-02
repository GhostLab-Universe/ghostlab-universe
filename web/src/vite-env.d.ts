/* Minimal Vite client typings for the guided UI (no new dependencies).
 * Only the build-time flags the app reads are declared. */

interface ImportMetaEnv {
  /** True under `vite dev`, false in `vite build` output. */
  readonly DEV: boolean;
  /** True in `vite build` output, false under `vite dev`. */
  readonly PROD: boolean;
  /** The active mode name (`vite --mode ...`). */
  readonly MODE: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
