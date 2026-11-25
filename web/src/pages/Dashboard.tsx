import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Filter, Loader2, PenSquare, Wand2 } from "lucide-react";
import { Link } from "react-router-dom";
import SearchForm from "@/components/SearchForm";
import JobCard from "@/components/JobCard";
import TailorModal, {
  type TailorOptions,
} from "@/components/TailorModal";
import TaskProgress from "@/components/TaskProgress";
import {
  useActiveTasks,
  useJobs,
  useStartRun,
  useStatus,
} from "@/lib/hooks";
import type { JobRecord, TaskRecord } from "@/lib/types";
import clsx from "clsx";

const STATUS_FILTERS = [
  { value: "all", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "done", label: "Tailored" },
  { value: "skipped", label: "Low fit" },
  { value: "failed", label: "Failed" },
];

type SortOrder = "fit" | "title" | "workspace";

export default function Dashboard() {
  const status = useStatus();
  const jobs = useJobs();
  const [filter, setFilter] = useState<string>("all");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortOrder>("fit");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [searchTask, setSearchTask] = useState<TaskRecord | null>(null);
  // Any search still running (including one started in another tab or
  // before a page reload) keeps the Search button disabled so a
  // double-click can't kick off duplicate JobSpy scrapes.
  const activeTasks = useActiveTasks();
  const searchRunning = activeTasks.all.some((t) => t.kind === "search");
  const [runTask, setRunTask] = useState<TaskRecord | null>(null);
  const [tailorOpen, setTailorOpen] = useState(false);
  const startRun = useStartRun();

  const filtered: JobRecord[] = useMemo(() => {
    const list = jobs.data?.jobs ?? [];
    const q = query.trim().toLowerCase();
    const matches = list.filter((j) => {
      if (filter !== "all" && j.status !== filter) return false;
      if (!q) return true;
      const hay =
        (j.title || "") +
        " " +
        (j.company || "") +
        " " +
        (j.location || "") +
        " " +
        (j.description || "");
      return hay.toLowerCase().includes(q);
    });
    if (sort === "fit") {
      matches.sort((a, b) => (b.fit?.score ?? -1) - (a.fit?.score ?? -1));
    } else if (sort === "title") {
      matches.sort((a, b) => (a.title || "").localeCompare(b.title || ""));
    }
    return matches;
  }, [jobs.data, filter, query, sort]);

  // A job may be deleted on its detail page or by another browser tab.
  useEffect(() => {
    if (!jobs.data) return;
    const available = new Set(jobs.data.jobs.map((job) => job.job_id));
    setSelected((current) => {
      const next = new Set([...current].filter((id) => available.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [jobs.data]);

  const totalCount = jobs.data?.jobs.length ?? 0;
  const visibleSelectedCount = filtered.filter((job) =>
    selected.has(job.job_id),
  ).length;
  const allVisibleSelected =
    filtered.length > 0 && visibleSelectedCount === filtered.length;
  const hiddenSelectedCount = selected.size - visibleSelectedCount;
  const hasFilters = filter !== "all" || query.trim().length > 0;
  const profileLoaded = status.data?.profile_loaded ?? false;

  const clearFilters = () => {
    setFilter("all");
    setQuery("");
  };

  const toggleVisible = () => {
    setSelected((current) => {
      const next = new Set(current);
      for (const job of filtered) {
        if (allVisibleSelected) next.delete(job.job_id);
        else next.add(job.job_id);
      }
      return next;
    });
  };

  const toggleSelected = (jobId: string, on: boolean) => {
    setSelected((s) => {
      const next = new Set(s);
      if (on) next.add(jobId);
      else next.delete(jobId);
      return next;
    });
  };

  const onConfirmTailor = async (opts: TailorOptions) => {
    if (selected.size === 0) return;
    try {
      const t = await startRun.mutateAsync({
        job_ids: Array.from(selected),
        provider: opts.provider,
        model: opts.model,
        no_pdf: opts.no_pdf,
        force: opts.force,
      });
      setRunTask(t);
      setSelected(new Set());
      setTailorOpen(false);
    } catch {
      // The modal displays the mutation error and keeps the selection for retry.
    }
  };

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight">Jobs</h1>
        <p className="text-sm text-slate-400">
          Run a search, then tailor your resume + cover letter for the roles
          you like.
        </p>
      </div>

      {status.data && !profileLoaded ? (
        <div className="card border-amber-700/40 bg-amber-900/10 p-4 text-sm text-amber-200">
          <div className="font-semibold">profile.json not found</div>
          <p className="mt-1 text-amber-100/80">
            Run{" "}
            <code className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-xs">
              jobapply init --resume &lt;path&gt;
            </code>{" "}
            in this directory before tailoring or scoring.
          </p>
        </div>
      ) : null}

      <SearchForm
        onTaskStarted={(t) => setSearchTask(t)}
        busy={searchRunning}
      />

      {searchTask ? (
        <TaskProgress
          taskId={searchTask.task_id}
          onDone={() => jobs.refetch()}
        />
      ) : null}
      {runTask ? (
        <TaskProgress
          taskId={runTask.task_id}
          onDone={() => jobs.refetch()}
        />
      ) : null}

      <div className="card flex flex-wrap items-center gap-3 p-4">
        <div className="flex min-w-0 flex-[1_1_240px] items-center gap-2">
          <Filter size={16} className="text-slate-400" />
          <input
            className="input flex-1"
            placeholder="Filter by title, company, description…"
            aria-label="Filter jobs"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="flex flex-wrap gap-1">
          {STATUS_FILTERS.map((f) => (
            <button
              key={f.value}
              className={clsx(
                "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                filter === f.value
                  ? "bg-brand-500/20 text-brand-200"
                  : "text-slate-400 hover:bg-slate-800 hover:text-slate-100",
              )}
              aria-pressed={filter === f.value}
              onClick={() => setFilter(f.value)}
            >
              {f.label}{" "}
              <span className="font-mono opacity-70">
                {f.value === "all"
                  ? totalCount
                  : (jobs.data?.jobs ?? []).filter((j) => j.status === f.value)
                      .length}
              </span>
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <Link to="/freeform" className="btn-ghost text-sm">
            <PenSquare size={14} />
            Paste JD
          </Link>
          <button
            className="btn-primary"
            disabled={
              selected.size === 0 || startRun.isPending || !profileLoaded
            }
            onClick={() => {
              startRun.reset();
              setTailorOpen(true);
            }}
          >
            {startRun.isPending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Wand2 size={14} />
            )}
            Tailor selected ({selected.size})
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-sm text-slate-400">
        <p aria-live="polite">
          Showing{" "}
          <span className="font-mono text-slate-200">{filtered.length}</span>
          {" "}of <span className="font-mono text-slate-200">{totalCount}</span>
          {" "}jobs
          {hiddenSelectedCount > 0 ? (
            <span className="ml-2 text-amber-300">
              · {hiddenSelectedCount} selected outside this view
            </span>
          ) : null}
        </p>
        {hasFilters ? (
          <button className="btn-ghost" onClick={clearFilters}>
            Clear filters
          </button>
        ) : null}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2">
            <span className="text-xs">Sort by</span>
            <select
              className="input w-auto"
              value={sort}
              onChange={(e) => setSort(e.target.value as SortOrder)}
            >
              <option value="fit">Highest fit</option>
              <option value="title">Title A–Z</option>
              <option value="workspace">Workspace order</option>
            </select>
          </label>
          <button
            className="btn-secondary"
            disabled={filtered.length === 0 || startRun.isPending}
            onClick={toggleVisible}
          >
            {allVisibleSelected ? "Deselect visible" : "Select visible"}
          </button>
          {selected.size > 0 ? (
            <button
              className="btn-ghost"
              disabled={startRun.isPending}
              onClick={() => setSelected(new Set())}
            >
              Clear selection
            </button>
          ) : null}
        </div>
      </div>

      {jobs.isError ? (
        <div
          role="alert"
          className="card flex flex-wrap items-center gap-3 border-rose-800/50 p-4 text-sm text-rose-200"
        >
          <AlertTriangle size={18} />
          <div className="min-w-0 flex-1">
            <p className="font-semibold">Could not load jobs</p>
            <p className="mt-1 break-words text-rose-200/80">
              {jobs.error.message}
            </p>
          </div>
          <button
            className="btn-secondary"
            disabled={jobs.isFetching}
            onClick={() => jobs.refetch()}
          >
            {jobs.isFetching ? "Retrying…" : "Retry"}
          </button>
        </div>
      ) : null}

      {jobs.isLoading ? (
        <div className="card flex items-center gap-2 p-6 text-sm text-slate-400">
          <Loader2 size={16} className="animate-spin" /> Loading jobs…
        </div>
      ) : jobs.isError && !jobs.data ? null : filtered.length === 0 ? (
        <div className="card flex flex-col items-center gap-3 p-10 text-center">
          <div className="text-base font-semibold text-slate-200">
            {totalCount === 0
              ? "Your job list is empty"
              : "No jobs match these filters"}
          </div>
          <p className="max-w-md text-sm text-slate-400">
            {totalCount === 0
              ? "Search job boards above or paste a job description to add your first role."
              : "Try another title, company, location, or status to find the roles you want."}
          </p>
          {hasFilters ? (
            <button className="btn-secondary" onClick={clearFilters}>
              Clear filters
            </button>
          ) : null}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {filtered.map((job) => (
            <JobCard
              key={job.job_id}
              job={job}
              selected={selected.has(job.job_id)}
              onToggle={(on) => toggleSelected(job.job_id, on)}
            />
          ))}
        </div>
      )}

      <TailorModal
        open={tailorOpen}
        onClose={() => setTailorOpen(false)}
        onConfirm={onConfirmTailor}
        pending={startRun.isPending}
        error={startRun.error?.message}
        jobCount={selected.size}
      />
    </div>
  );
}
