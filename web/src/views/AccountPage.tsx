import { useEffect, useState } from "react";
type AuthStatus = { enabled: boolean; providers: string[]; signed_in: boolean };
export function AccountPage() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { let active = true; void fetch("/v1/auth/status").then(async response => {
    if (!response.ok) throw new Error("Account service is unavailable. Restart with the updated API.");
    const value = await response.json() as AuthStatus;
    if (typeof value.enabled !== "boolean" || !Array.isArray(value.providers) || !value.providers.every(p => typeof p === "string")) throw new Error("Invalid account service response.");
    if (active) setStatus(value);
  }).catch(e => {if (active) setError(String(e.message));}); return () => {active = false;}; }, []);
  return <section className="account-page"><h1>Your workspace, your account.</h1><p>Sign in to access the projects assigned to you.</p>
    {error && <p role="alert">{error}</p>}
    {!status && !error && <p role="status">Checking sign-in options…</p>}
    {status && <><div className="provider-buttons">{["github", "google"].map(provider => <div key={provider}><a className="provider-button" href={status.providers.includes(provider) ? `/v1/auth/${provider}/start` : undefined} aria-disabled={!status.providers.includes(provider)}>{provider === "github" ? "Continue with GitHub" : "Continue with Google"}</a>{!status.providers.includes(provider) && <small>Workspace owner needs to configure {provider === "github" ? "GitHub" : "Google"} sign-in.</small>}</div>)}</div>
    {status.signed_in && <button onClick={async () => { const response = await fetch("/v1/auth/logout", {method: "POST", headers: {"X-GhostLab-Intent": "logout"}}); if (response.ok) window.location.reload(); else setError("Could not sign out. Please try again."); }}>Sign out</button>}
    {!status.enabled && <p className="setup-note">This installation currently uses local access. Sign-in will become available after the provider setup is complete.</p>}</>}
  </section>;
}
