import { useState } from "react";

type Inspection = { commit_sha: string; repository_url: string; setup_files: string[]; next_steps: string[]; status: string };
function valid(value: unknown): value is Inspection {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return typeof v.commit_sha === "string" && /^[a-f0-9]{40}$/.test(v.commit_sha) && typeof v.repository_url === "string"
    && v.status === "source-inspected-not-runnable" && Array.isArray(v.setup_files) && v.setup_files.every(x => typeof x === "string")
    && Array.isArray(v.next_steps) && v.next_steps.every(x => typeof x === "string");
}

export function GitHubSource({ enabled }: { enabled: boolean }) {
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState<Inspection | null>(null);
  return <section className="source-connect">
    <div><h2>Start with your GitHub project</h2><p>Inspect a public repository and find the setup needed to test it.</p></div>
    <form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError(""); setReport(null);
      try {
        const response = await fetch("/v1/sources/github/inspect", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({repository_url: url.trim(), ref: ref.trim() || "HEAD"}), signal: AbortSignal.timeout(25000)});
        const body: unknown = await response.json();
        if (!response.ok) {
          const message = body && typeof body === "object" && "error" in body && body.error && typeof body.error === "object" && "message" in body.error && typeof body.error.message === "string" ? body.error.message : `Inspection failed (${response.status}). Check your connection and sign-in.`;
          throw new Error(message);
        }
        if (!valid(body)) throw new Error("The service returned an invalid inspection report.");
        setReport(body);
      } catch (e) { setError(e instanceof Error ? e.message : "Could not inspect this repository."); }
      finally { setBusy(false); }
    }}>
      <label className="field"><span>Repository URL</span><input type="url" required placeholder="https://github.com/your-team/your-project" value={url} disabled={busy || !enabled} onChange={e => {setUrl(e.target.value); setReport(null);}} /></label>
      <label className="field"><span>Branch or commit (optional)</span><input value={ref} disabled={busy || !enabled} placeholder="Default branch" onChange={e => {setRef(e.target.value); setReport(null);}} /></label>
      <button disabled={busy || !enabled} aria-busy={busy}>{busy ? "Inspecting repository…" : "Inspect repository"}</button>
    </form>
    {!enabled && <p>Connect to the service to inspect a repository.</p>}
    {error && <p role="alert" className="source-error">{error}</p>}
    {report && <div className="source-report" role="status"><strong>Source inspected · Setup required</strong><p>Commit <code>{report.commit_sha}</code></p><p>Detected: {report.setup_files.join(", ") || "No supported root setup files"}</p><ol>{report.next_steps.map(step => <li key={step}>{step}</li>)}</ol></div>}
    <small>Inspection does not install or run repository code. Private repositories and automatic execution are not connected here yet.</small>
  </section>;
}
