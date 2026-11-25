import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type {
  JobRecord,
  JobsListResponse,
  ProvidersResponse,
  ServerStatus,
  TaskRecord,
} from "./types";

// Centralized query keys make it easy to invalidate the right caches
// when a mutation lands. (`["jobs"]` covers list + detail; React Query
// invalidates anything starting with the prefix.)
export const queryKeys = {
  status: ["status"] as const,
  providers: ["providers"] as const,
  jobs: ["jobs"] as const,
  job: (id: string) => ["jobs", id] as const,
  searches: ["searches"] as const,
  task: (id: string) => ["task", id] as const,
  activeTasks: ["tasks", "active"] as const,
};

export function useStatus(opts?: Partial<UseQueryOptions<ServerStatus>>) {
  return useQuery({
    queryKey: queryKeys.status,
    queryFn: api.status,
    ...opts,
  });
}

export function useProviders(
  opts?: Partial<UseQueryOptions<ProvidersResponse>>,
) {
  return useQuery({
    queryKey: queryKeys.providers,
    queryFn: api.providers,
    staleTime: 60_000,
    ...opts,
  });
}

export function useJobs(opts?: Partial<UseQueryOptions<JobsListResponse>>) {
  return useQuery({
    queryKey: queryKeys.jobs,
    queryFn: api.jobs,
    ...opts,
  });
}

export function useJob(
  jobId: string | undefined,
  opts?: Partial<UseQueryOptions<JobRecord>>,
) {
  return useQuery({
    queryKey: jobId ? queryKeys.job(jobId) : ["jobs", "missing"],
    enabled: Boolean(jobId),
    queryFn: () => api.job(jobId!),
    ...opts,
  });
}

export function useSearches() {
  return useQuery({ queryKey: queryKeys.searches, queryFn: api.searches });
}

/** Polls /api/tasks/<id> until the task settles. Returns the latest
 *  snapshot plus a `done` flag the caller can use to invalidate
 *  dependent queries exactly once. */
export function useTaskPoll(taskId: string | null) {
  const [task, setTask] = useState<TaskRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!taskId) {
      setTask(null);
      setError(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const t = await api.task(taskId);
        if (cancelled) return;
        setTask(t);
        if (
          t.status === "succeeded" ||
          t.status === "failed" ||
          t.status === "cancelled"
        ) {
          return; // stop polling
        }
        timer = window.setTimeout(tick, 1200);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    };
    let timer = window.setTimeout(tick, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [taskId]);

  const isTerminal =
    !!task &&
    (task.status === "succeeded" ||
      task.status === "failed" ||
      task.status === "cancelled");

  return { task, error, isTerminal };
}

export function useStartSearch() {
  return useMutation({ mutationFn: api.search });
}

export function useStartRun() {
  return useMutation({ mutationFn: api.run });
}

export function useStartTailor(jobId: string) {
  return useMutation({
    mutationFn: (
      vars: { provider?: string; model?: string; no_pdf?: boolean } = {},
    ) => api.tailor(jobId, vars),
  });
}

export function useStartFreeform() {
  return useMutation({ mutationFn: api.freeform });
}

export function useStartEmail(jobId: string) {
  return useMutation({
    mutationFn: (vars: {
      recipient: string;
      additional_info?: string;
      provider?: string;
      model?: string;
    }) => api.email(jobId, vars),
  });
}

/** Read the text of an editable artifact (resume.tex / cover_letter.tex
 *  / .md). Cached short — we always want a fresh read when the editor
 *  mounts but don't need to refetch on every focus. */
export function useArtifactText(
  jobId: string | undefined,
  name: string | undefined,
) {
  return useQuery({
    queryKey: ["artifact-text", jobId, name],
    enabled: Boolean(jobId && name),
    queryFn: () => api.artifactText(jobId!, name!),
    staleTime: 5_000,
    gcTime: 60_000,
  });
}

export function useSaveArtifact(jobId: string, name: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (content: string) => api.saveArtifact(jobId, name, content),
    onSuccess: () => {
      // The cached text now matches what we just sent — no need to
      // refetch, but invalidate so any peer subscriber re-aligns.
      qc.invalidateQueries({ queryKey: ["artifact-text", jobId, name] });
      qc.invalidateQueries({ queryKey: queryKeys.job(jobId) });
    },
  });
}

export function useCompileArtifact(jobId: string, name: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.compileArtifact(jobId, name),
    onSuccess: () => {
      // Refresh the job record so ``available_artifacts`` includes
      // the freshly-written .pdf and the JobDetail download buttons
      // show up again if they were missing.
      qc.invalidateQueries({ queryKey: queryKeys.job(jobId) });
    },
  });
}

export function useDeleteJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => api.deleteJob(jobId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.jobs });
      qc.invalidateQueries({ queryKey: queryKeys.status });
    },
  });
}

export function useEmailHint(jobId: string | undefined) {
  return useQuery({
    queryKey: ["email-hint", jobId],
    enabled: Boolean(jobId),
    queryFn: () => api.emailHint(jobId!),
  });
}

// ---------------------------------------------------------------------------
// Live task tracker
// ---------------------------------------------------------------------------

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

export interface ActiveTaskMap {
  /** ``true`` when at least one tailor/email/run task is currently
   *  running or pending. The job card layer uses this to crank up the
   *  refetch frequency only while there's actually work to track. */
  hasActive: boolean;
  /** Indexed by ``job_id``. ``run`` tasks (which target a list of
   *  job_ids) get duplicated across every selected job_id so each
   *  affected card can pick the same task up. */
  byJobId: Map<string, TaskRecord>;
  /** Most-recent task per ``job_id`` regardless of status. Lets the
   *  UI show "last tailor failed" badges that survive the active →
   *  terminal transition. */
  lastByJobId: Map<string, TaskRecord>;
  /** Bag of currently-active tasks regardless of whether they map to
   *  a single job (e.g. the search task). */
  all: TaskRecord[];
}

/** Polls ``/api/tasks`` on a short interval and returns a structured
 *  view that the dashboard / job cards / detail page can subscribe to.
 *
 *  We invalidate the jobs cache automatically whenever a tracked task
 *  transitions from active → terminal so the UI can reflect the new
 *  workspace state (status, artifacts, fit score) without the user
 *  having to refresh.
 */
export function useActiveTasks(
  { invalidateOnFinish = false }: { invalidateOnFinish?: boolean } = {},
): ActiveTaskMap {
  const qc = useQueryClient();
  const previousIdsRef = useRef<Set<string>>(new Set());

  const query = useQuery({
    queryKey: queryKeys.activeTasks,
    queryFn: () => api.tasksList(50),
    // 1.5s when something's active, 5s otherwise — TanStack runs
    // refetchInterval as a callback so we can adapt based on the
    // last response.
    refetchInterval: (q) => {
      const data = q.state.data as
        | { tasks: TaskRecord[] }
        | undefined;
      const active = data?.tasks.some(
        (t) => !TERMINAL_STATUSES.has(t.status),
      );
      return active ? 1500 : 5000;
    },
    refetchIntervalInBackground: true,
    staleTime: 0,
  });

  const result = useMemo<ActiveTaskMap>(() => {
    const tasks = query.data?.tasks ?? [];
    const active = tasks.filter((t) => !TERMINAL_STATUSES.has(t.status));
    const byJobId = new Map<string, TaskRecord>();
    for (const t of active) {
      const meta = t.metadata ?? {};
      const single =
        typeof meta["job_id"] === "string"
          ? (meta["job_id"] as string)
          : null;
      if (single) byJobId.set(single, t);
      const batch = meta["job_ids"];
      if (Array.isArray(batch)) {
        for (const id of batch) {
          if (typeof id === "string") byJobId.set(id, t);
        }
      }
    }
    // Sorted newest-first so the first task we see for a given
    // job_id is also the most recent one.
    const sorted = [...tasks].sort((a, b) =>
      (b.created_at || "").localeCompare(a.created_at || ""),
    );
    const lastByJobId = new Map<string, TaskRecord>();
    for (const t of sorted) {
      const meta = t.metadata ?? {};
      const single =
        typeof meta["job_id"] === "string"
          ? (meta["job_id"] as string)
          : null;
      if (single && !lastByJobId.has(single)) lastByJobId.set(single, t);
      const batch = meta["job_ids"];
      if (Array.isArray(batch)) {
        for (const id of batch) {
          if (typeof id === "string" && !lastByJobId.has(id)) {
            lastByJobId.set(id, t);
          }
        }
      }
    }
    return {
      hasActive: active.length > 0,
      byJobId,
      lastByJobId,
      all: active,
    };
  }, [query.data]);

  // Detect terminal transitions: any id present in the previous
  // active set that's no longer active means the task finished, so
  // refresh dependent caches.
  // Only one subscriber (the Layout) should do this. Every JobCard also
  // calls this hook, and N cards each invalidating ``jobs`` on the same
  // transition caused a burst of /api/jobs refetches after each task.
  useEffect(() => {
    if (!invalidateOnFinish) return;
    const currentIds = new Set(result.all.map((t) => t.task_id));
    const prevIds = previousIdsRef.current;
    let didFinish = false;
    for (const id of prevIds) {
      if (!currentIds.has(id)) {
        didFinish = true;
        break;
      }
    }
    if (didFinish) {
      qc.invalidateQueries({ queryKey: queryKeys.jobs });
      qc.invalidateQueries({ queryKey: queryKeys.status });
      qc.invalidateQueries({ queryKey: queryKeys.searches });
    }
    previousIdsRef.current = currentIds;
  }, [result.all, qc, invalidateOnFinish]);

  return result;
}

/** Convenience helper for components that only care about a single
 *  job_id — returns the active task (if any) plus a stable label /
 *  percent the card can render.
 *
 *  Pass ``kinds`` to filter (e.g. ``["email"]`` for the email modal
 *  so a parallel tailor task on the same job doesn't get picked up).
 */
export function useActiveTaskFor(
  jobId: string | undefined,
  kinds?: readonly string[],
): {
  task: TaskRecord | null;
  label: string;
  percent: number;
} {
  const active = useActiveTasks();
  if (!jobId) return { task: null, label: "", percent: 0 };
  let task: TaskRecord | null;
  if (kinds && kinds.length > 0) {
    const allowed = new Set(kinds);
    task =
      active.all.find((t) => {
        if (!allowed.has(t.kind)) return false;
        const meta = t.metadata ?? {};
        if (typeof meta["job_id"] === "string" && meta["job_id"] === jobId) {
          return true;
        }
        const batch = meta["job_ids"];
        return (
          Array.isArray(batch) &&
          batch.some((id) => typeof id === "string" && id === jobId)
        );
      }) ?? null;
  } else {
    task = active.byJobId.get(jobId) ?? null;
  }
  if (!task) return { task: null, label: "", percent: 0 };
  return {
    task,
    label: task.progress?.label || _kindLabel(task.kind),
    percent: task.progress?.percent ?? 0,
  };
}

/** Returns the most recent task for a job — active or terminal — so
 *  callers can render a "last tailor failed" hint without blocking
 *  on a fresh server-side query. ``null`` when no recent task is on
 *  record (the workspace task list is bounded; very old failures
 *  fall off automatically). */
export function useLastTaskFor(jobId: string | undefined): TaskRecord | null {
  const { lastByJobId } = useActiveTasks();
  if (!jobId) return null;
  return lastByJobId.get(jobId) ?? null;
}

function _kindLabel(kind: string): string {
  switch (kind) {
    case "tailor":
      return "Tailoring…";
    case "run":
      return "Tailoring batch…";
    case "email":
      return "Drafting email…";
    case "freeform":
      return "Tailoring pasted JD…";
    case "search":
      return "Searching…";
    default:
      return "Working…";
  }
}
