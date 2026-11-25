import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useTaskPoll } from "@/lib/hooks";
import { queryKeys } from "@/lib/hooks";
import { api } from "@/lib/api";

interface Props {
  taskId: string;
  onDone?: (status: "succeeded" | "failed" | "cancelled") => void;
  invalidateOnDone?: ReadonlyArray<readonly unknown[]>;
}

/** Card that polls a backend task and renders its progress live. */
export default function TaskProgress({
  taskId,
  onDone,
  invalidateOnDone,
}: Props) {
  const { task, error } = useTaskPoll(taskId);
  const qc = useQueryClient();
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    if (!task) return;
    if (
      task.status === "succeeded" ||
      task.status === "failed" ||
      task.status === "cancelled"
    ) {
      const keys = invalidateOnDone ?? [queryKeys.jobs, queryKeys.status];
      for (const key of keys) qc.invalidateQueries({ queryKey: key });
      onDone?.(task.status);
    }
  }, [task?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!task) {
    return (
      <div className="card flex items-center gap-3 p-4">
        <Loader2 size={16} className="animate-spin text-brand-400" />
        <span className="text-sm text-slate-300">Starting task…</span>
        {error ? (
          <span className="text-xs text-rose-400">{error}</span>
        ) : null}
      </div>
    );
  }

  const Icon =
    task.status === "succeeded"
      ? CheckCircle2
      : task.status === "failed"
        ? XCircle
        : Loader2;
  const tone =
    task.status === "succeeded"
      ? "text-brand-400"
      : task.status === "failed"
        ? "text-rose-400"
        : "text-amber-300";
  const isRunning = task.status === "pending" || task.status === "running";

  return (
    <div className="card space-y-3 p-4">
      <div className="flex items-center gap-3 text-sm">
        <Icon
          size={16}
          className={tone + (isRunning ? " animate-spin" : "")}
        />
        <span className="font-semibold text-slate-100">
          {task.kind.charAt(0).toUpperCase() + task.kind.slice(1)} task
        </span>
        <span className="ml-auto text-xs uppercase tracking-wide text-slate-500">
          {task.status}
        </span>
        {isRunning ? (
          <button
            className="btn-ghost text-xs"
            disabled={cancelling}
            title={
              cancelling
                ? "Stopping after the current step finishes"
                : "Stop this task after the current step"
            }
            onClick={() => {
              setCancelling(true);
              api.cancelTask(task.task_id).catch(() => setCancelling(false));
            }}
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        ) : null}
      </div>
      <div className="text-sm text-slate-300">
        {task.progress.label || "Working…"}
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full transition-all ${
            task.status === "failed"
              ? "bg-rose-500"
              : task.status === "succeeded"
                ? "bg-brand-500"
                : "bg-brand-400/80"
          }`}
          style={{
            width: `${
              task.status === "succeeded"
                ? 100
                : Math.max(2, Math.min(100, task.progress.percent))
            }%`,
          }}
        />
      </div>
      {task.error ? (
        <pre className="max-h-40 overflow-auto rounded bg-rose-900/30 p-2 text-xs text-rose-100">
          {task.error}
        </pre>
      ) : null}
      {task.progress.log.length > 0 ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
            Log ({task.progress.log.length})
          </summary>
          <div className="mt-2 max-h-48 space-y-0.5 overflow-auto rounded bg-slate-950/80 p-2 font-mono text-[11px] text-slate-300">
            {task.progress.log.slice(-100).map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}
