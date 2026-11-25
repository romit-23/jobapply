import type {
  EmailDraft,
  EmailHint,
  JobRecord,
  JobsListResponse,
  ProvidersResponse,
  SearchHistoryRow,
  ServerStatus,
  TaskRecord,
} from "./types";

// Tiny fetch wrapper with JSON in/out and friendly error surfaces.
// We deliberately avoid axios — it's overkill for the handful of
// endpoints the UI talks to, and `fetch` is universally available.

class ApiError extends Error {
  status: number;
  payload: unknown;
  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { json?: unknown },
): Promise<T> {
  const headers = new Headers(init?.headers);
  // Required by the server's CSRF guard on POST/PUT/DELETE; harmless on GET.
  headers.set("X-JobApply-Client", "web");
  let body: BodyInit | null | undefined = init?.body ?? null;
  if (init?.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  const res = await fetch(path, { ...init, headers, body });
  const text = await res.text();
  let payload: unknown = undefined;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!res.ok) {
    const message =
      (payload &&
        typeof payload === "object" &&
        "detail" in payload &&
        typeof (payload as { detail: unknown }).detail === "string"
        ? ((payload as { detail: string }).detail)
        : `HTTP ${res.status}`);
    throw new ApiError(res.status, message, payload);
  }
  return payload as T;
}

export const api = {
  status: () => request<ServerStatus>("/api/status"),
  providers: () => request<ProvidersResponse>("/api/providers"),
  jobs: () => request<JobsListResponse>("/api/jobs"),
  job: (jobId: string) => request<JobRecord>(`/api/jobs/${jobId}`),
  deleteJob: (jobId: string) =>
    request<{ deleted: string }>(`/api/jobs/${jobId}`, { method: "DELETE" }),
  searches: () =>
    request<{ workspace: string; searches: SearchHistoryRow[] }>(
      "/api/searches",
    ),
  task: (taskId: string) => request<TaskRecord>(`/api/tasks/${taskId}`),
  tasksList: (limit = 25) =>
    request<{ tasks: TaskRecord[] }>(`/api/tasks?limit=${limit}`),
  cancelTask: (taskId: string) =>
    request<{ cancelled: string }>(`/api/tasks/${taskId}/cancel`, {
      method: "POST",
    }),
  search: (body: {
    titles: string[];
    skills?: string[];
    location?: string;
    remote?: boolean;
    results_wanted?: number;
    sites?: string[];
    score?: boolean;
    provider?: string;
    model?: string;
    linkedin_descriptions?: boolean;
    force?: boolean;
  }) =>
    request<TaskRecord>("/api/search", {
      method: "POST",
      json: body,
    }),
  run: (body: {
    job_ids: string[];
    provider?: string;
    model?: string;
    no_pdf?: boolean;
    force?: boolean;
  }) =>
    request<TaskRecord>("/api/run", {
      method: "POST",
      json: body,
    }),
  tailor: (
    jobId: string,
    body: { provider?: string; model?: string; no_pdf?: boolean } = {},
  ) =>
    request<TaskRecord>(`/api/jobs/${jobId}/tailor`, {
      method: "POST",
      json: body,
    }),
  freeform: (body: {
    description: string;
    title?: string;
    company?: string;
    location?: string;
    skills?: string[];
    provider?: string;
    model?: string;
    no_pdf?: boolean;
  }) =>
    request<TaskRecord>("/api/freeform", {
      method: "POST",
      json: body,
    }),
  email: (
    jobId: string,
    body: {
      recipient: string;
      additional_info?: string;
      provider?: string;
      model?: string;
    },
  ) =>
    request<TaskRecord>(`/api/jobs/${jobId}/email`, {
      method: "POST",
      json: body,
    }),
  emailHint: (jobId: string) =>
    request<EmailHint>(`/api/jobs/${jobId}/email-hint`),
  artifactUrl: (jobId: string, name: string) =>
    `/api/jobs/${jobId}/artifacts/${encodeURIComponent(name)}`,
  /** Fetch an editable artifact's text content. The backend serves
   *  the raw file via the same ``/artifacts/<name>`` endpoint as the
   *  download link — so we just read it as text. */
  artifactText: async (jobId: string, name: string): Promise<string> => {
    const res = await fetch(
      `/api/jobs/${jobId}/artifacts/${encodeURIComponent(name)}`,
    );
    if (!res.ok) {
      throw new ApiError(res.status, `HTTP ${res.status}`, null);
    }
    return res.text();
  },
  saveArtifact: (jobId: string, name: string, content: string) =>
    request<{ name: string; bytes: number; mtime: number; saved: string }>(
      `/api/jobs/${jobId}/artifacts/${encodeURIComponent(name)}`,
      { method: "PUT", json: { content } },
    ),
  compileArtifact: (jobId: string, name: string) =>
    request<{
      name: string;
      size: number;
      mtime: number;
      backend: string;
    }>(
      `/api/jobs/${jobId}/artifacts/${encodeURIComponent(name)}/compile`,
      { method: "POST" },
    ),
};

export type { EmailDraft, EmailHint };
export { ApiError };
