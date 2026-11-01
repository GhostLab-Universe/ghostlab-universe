import type { ReactNode } from "react";
import type { ErrorInfo, Ref } from "../api/api.generated.ts";
import { isTestData, type Failure, type Origin } from "../data/source.ts";
import { describeFailure } from "../logic/failure.ts";
import { prettyJson, shortDigest } from "../logic/format.ts";
import type { Tone } from "../logic/repair.ts";

export function Tag({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className="tag" data-tone={tone}>
      {children}
    </span>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <code className="mono">{children}</code>;
}

export function Digest({ value }: { value: string }) {
  return (
    <code className="mono digest" title={value}>
      {value}
    </code>
  );
}

const ORIGIN_LABEL: Record<Exclude<Origin["kind"], "service">, string> = {
  published_example: "Published contract example",
  recorded_core_output: "Recorded core output",
  hand_authored: "Hand-authored test data",
};

/** Says where a section's data came from. Renders nothing for service data. */
export function OriginNote({ origins }: { origins: readonly Origin[] }) {
  if (!isTestData(origins)) return null;
  return (
    <details className="origin">
      <summary>
        <span className="origin-flag">Test data</span> Where this came from
      </summary>
      <ul>
        {origins.map((origin, index) =>
          origin.kind === "service" ? null : (
            <li key={index}>
              {ORIGIN_LABEL[origin.kind]}: <Mono>{origin.source}</Mono>
              {origin.note === null ? null : <span className="muted"> ({origin.note})</span>}
            </li>
          ),
        )}
      </ul>
    </details>
  );
}

export function Section({
  title,
  origins = [],
  id,
  children,
}: {
  title: string;
  origins?: readonly Origin[];
  id?: string;
  children: ReactNode;
}) {
  return (
    <section className="section" {...(id === undefined ? {} : { id })}>
      <header className="section-head">
        <h2>{title}</h2>
        <OriginNote origins={origins} />
      </header>
      {children}
    </section>
  );
}

export interface Fact {
  term: string;
  detail: ReactNode;
}

export function Facts({ facts }: { facts: readonly Fact[] }) {
  return (
    <dl className="facts">
      {facts.map((fact) => (
        <div key={fact.term}>
          <dt>{fact.term}</dt>
          <dd>{fact.detail}</dd>
        </div>
      ))}
    </dl>
  );
}

export function JsonBlock({ value, label }: { value: unknown; label: string }) {
  return (
    <details className="json">
      <summary>{label}</summary>
      <pre tabIndex={0} aria-label={label}>
        {prettyJson(value)}
      </pre>
    </details>
  );
}

export function RefLabel({ value }: { value: Ref }) {
  return (
    <span>
      <Mono>{value.id}</Mono> <span className="muted">({value.kind})</span>
      {value.sha256 === null ? null : <span title={value.sha256}> {shortDigest(value.sha256)}</span>}
    </span>
  );
}

export type NoticeState =
  | "blocked"
  | "incomplete"
  | "loading"
  | "empty"
  | "denied"
  | "validation_error"
  | "not_found"
  | "conflict"
  | "cancelled"
  | "failed"
  | "reconnecting"
  | "awaiting_approval";

const NOTICE_TONE: Record<NoticeState, Tone> = {
  blocked: "warn",
  incomplete: "warn",
  loading: "neutral",
  empty: "neutral",
  denied: "fail",
  validation_error: "fail",
  not_found: "warn",
  conflict: "warn",
  cancelled: "warn",
  failed: "fail",
  reconnecting: "warn",
  awaiting_approval: "active",
};

export function Notice({
  state,
  title,
  children,
  live = false,
}: {
  state: NoticeState;
  title: string;
  children?: ReactNode;
  live?: boolean;
}) {
  const urgent = state === "denied" || state === "failed" || state === "validation_error";
  return (
    <div
      className="notice"
      data-tone={NOTICE_TONE[state]}
      data-testid={`state-${state}`}
      {...(live ? { role: urgent ? "alert" : "status" } : {})}
    >
      <strong>{title}</strong>
      {children === undefined ? null : <div className="notice-body">{children}</div>}
    </div>
  );
}

export function FailureNotice({ failure, children }: { failure: Failure; children?: ReactNode }) {
  const summary = describeFailure(failure);
  return (
    <Notice state={summary.state} title={summary.title} live>
      <p>{summary.message}</p>
      {summary.help === null ? null : <p>{summary.help}</p>}
      {summary.code === null ? null : (
        <p className="muted">
          Service code <Mono>{summary.code}</Mono>
          {summary.reason === null ? null : (
            <>
              , reason <Mono>{summary.reason}</Mono>
            </>
          )}
          {summary.sideEffect === null ? null : (
            <>
              , side effect <Mono>{summary.sideEffect}</Mono>
            </>
          )}
        </p>
      )}
      {children}
    </Notice>
  );
}

export function RecordedErrors({ errors }: { errors: readonly ErrorInfo[] }) {
  return (
    <ul className="plain">
      {errors.map((error, index) => (
        <li key={index}>
          <Tag tone="fail">{error.code}</Tag> {error.message}{" "}
          <span className="muted">
            side effect <Mono>{error.side_effect}</Mono>
          </span>
        </li>
      ))}
    </ul>
  );
}

export function Loading({ what }: { what: string }) {
  return <Notice state="loading" title={`Loading ${what}`} live />;
}
