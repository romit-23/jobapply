/** Formatting helpers shared by the UI. */

export function formatStatus(status: string): {
  label: string;
  className: string;
} {
  switch (status) {
    case "done":
      return { label: "Tailored", className: "bg-brand-500/15 text-brand-300" };
    case "tailored":
      return { label: "Tailored", className: "bg-brand-500/15 text-brand-300" };
    case "cached":
      return {
        label: "Cached",
        className: "bg-amber-500/15 text-amber-300",
      };
    case "skipped":
      return { label: "Low fit", className: "bg-slate-500/20 text-slate-300" };
    case "failed":
      return { label: "Failed", className: "bg-rose-500/15 text-rose-300" };
    case "pending":
      return {
        label: "Pending",
        className: "bg-slate-700/40 text-slate-300",
      };
    case "rendered":
      return { label: "Rendered", className: "bg-brand-500/10 text-brand-300" };
    default:
      return {
        label: status || "Unknown",
        className: "bg-slate-700/40 text-slate-300",
      };
  }
}

export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return "—";
  return (score * 100).toFixed(0) + "%";
}

export function pickAcceptableUrl(job: {
  apply_url?: string | null;
  job_url?: string | null;
}): string | null {
  return job.apply_url || job.job_url || null;
}

export function shortHostname(url: string | null | undefined): string {
  if (!url) return "";
  try {
    const u = new URL(url);
    return u.hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** Parse a backend timestamp. The workspace DB (SQLite) drops timezone
 *  info, so some timestamps arrive as naive ISO strings that are really
 *  UTC; `new Date()` would read those as local time (e.g. "5h ago" for a
 *  search that just ran in IST). Treat a missing offset as UTC. */
export function parseServerDate(iso: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso.trim());
  return new Date(hasZone ? iso : `${iso}Z`);
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    const t = parseServerDate(iso).getTime();
    if (Number.isNaN(t)) return "";
    const diff = Math.max(0, Date.now() - t);
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 48) return `${hr}h ago`;
    const day = Math.floor(hr / 24);
    return `${day}d ago`;
  } catch {
    return "";
  }
}
