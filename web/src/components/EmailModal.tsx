import { useEffect, useState } from "react";
import { Loader2, Mail, X } from "lucide-react";
import {
  useActiveTaskFor,
  useEmailHint,
  useProviders,
  useStartEmail,
  useTaskPoll,
} from "@/lib/hooks";
import type { EmailDraft } from "@/lib/types";
import { useDialog } from "@/lib/useDialog";

interface Props {
  jobId: string;
  open: boolean;
  onClose: () => void;
  defaultRecipient?: string;
  /** ``true`` when the job already has a tailored resume + cover
   *  letter on disk. We surface this in the header so users know
   *  whether the draft will reference attachments or not. */
  isTailored?: boolean;
}

/** Modal that drafts and shows the application email. The recipient
 *  defaults to whatever the JD-extractor parsed out of the JD body
 *  (so direct-recruiter postings don't require user input).
 *
 *  The provider + model fields are pre-filled from the active
 *  ``jobapply.toml`` defaults but always visible — same UX as the
 *  tailor confirmation modal — so users notice which LLM is about
 *  to spend their tokens. */
export default function EmailModal({
  jobId,
  open,
  onClose,
  defaultRecipient,
  isTailored,
}: Props) {
  const hint = useEmailHint(open ? jobId : undefined);
  const providers = useProviders();
  const [recipient, setRecipient] = useState(defaultRecipient ?? "");
  const [context, setContext] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [taskId, setTaskId] = useState<string | null>(null);
  const start = useStartEmail(jobId);
  const { task } = useTaskPoll(taskId);
  // If a draft for this job was kicked off (here or from somewhere
  // else) and the user closed/re-opened the modal, the task tracker
  // resurfaces it so progress + the eventual result show up again.
  const live = useActiveTaskFor(jobId, ["email"]);

  useEffect(() => {
    if (open && hint.data?.primary_email && !recipient) {
      setRecipient(hint.data.primary_email);
    }
    if (open && hint.data?.subject_line && !context) {
      setContext(`Use subject line: ${hint.data.subject_line}`);
    }
  }, [open, hint.data]); // eslint-disable-line react-hooks/exhaustive-deps

  // Hydrate provider/model from the active toml whenever the modal
  // opens (or the providers payload arrives). We rerun this on
  // every open so a config change between drafts is reflected.
  useEffect(() => {
    if (!open || !providers.data) return;
    setProvider(providers.data.active_provider);
    setModel(providers.data.active_model);
  }, [open, providers.data]);

  // Re-attach to an in-flight draft when the modal is re-opened.
  useEffect(() => {
    if (!open) return;
    if (taskId) return;
    if (live.task) setTaskId(live.task.task_id);
  }, [open, live.task, taskId]);

  const dialogRef = useDialog<HTMLDivElement>(open, onClose);

  if (!open) return null;
  const draft = task?.result as EmailDraft | undefined;
  const meta = providers.data?.providers.find((p) => p.name === provider);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!recipient.trim() || !provider.trim() || !model.trim()) return;
    try {
      const t = await start.mutateAsync({
        recipient: recipient.trim(),
        additional_info: context.trim(),
        provider: provider.trim(),
        model: model.trim(),
      });
      setTaskId(t.task_id);
    } catch {
      // surfaced via mutation error state
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-sm">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="email-modal-title"
        className="card w-full max-w-2xl space-y-4 border-slate-700 p-6"
      >
        <div
          id="email-modal-title"
          className="flex items-center gap-2 text-base font-semibold text-slate-100"
        >
          <Mail size={18} className="text-brand-400" aria-hidden="true" />
          Draft application email
          <button
            className="ml-auto text-slate-400 hover:text-slate-100"
            onClick={onClose}
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {!draft ? (
          <form onSubmit={onSubmit} className="space-y-4">
            <div
              className={
                "rounded-md border px-3 py-2 text-xs " +
                (isTailored
                  ? "border-brand-700/40 bg-brand-900/10 text-brand-200"
                  : "border-amber-700/40 bg-amber-900/10 text-amber-200")
              }
            >
              {isTailored
                ? "Tailored resume + cover letter detected — the draft will reference them as attachments."
                : "No tailored resume yet — the draft will be grounded in the JD + your profile and won't claim attachments. Tailor first if you want a polished email referencing your tailored docs."}
            </div>
            <label className="block">
              <span className="label">Recipient email</span>
              <input
                className="input"
                type="email"
                placeholder="recruiter@acme.com"
                value={recipient}
                onChange={(e) => setRecipient(e.target.value)}
                required
              />
              {hint.data?.primary_email ? (
                <p className="mt-1 text-xs text-slate-400">
                  Detected from JD:{" "}
                  <span className="font-mono">
                    {hint.data.primary_email}
                  </span>
                </p>
              ) : null}
            </label>
            <label className="block">
              <span className="label">
                Additional context (referrals, availability, …)
              </span>
              <textarea
                className="input min-h-[88px]"
                placeholder="Referred by Bob — available to start in two weeks."
                value={context}
                onChange={(e) => setContext(e.target.value)}
              />
              {hint.data?.subject_line ? (
                <p className="mt-1 text-xs text-slate-400">
                  Recruiter requested subject:{" "}
                  <span className="font-mono">{hint.data.subject_line}</span>
                </p>
              ) : null}
            </label>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="label">Provider</span>
                <select
                  className="input"
                  value={provider}
                  disabled={
                    providers.isLoading ||
                    start.isPending ||
                    task?.status === "running"
                  }
                  onChange={(e) => {
                    const next = e.target.value;
                    setProvider(next);
                    const m = providers.data?.providers.find(
                      (p) => p.name === next,
                    );
                    if (m) setModel(m.default_model);
                  }}
                >
                  {providers.data?.providers.map((p) => (
                    <option key={p.name} value={p.name}>
                      {p.name}
                      {p.configured ? "" : " (not configured)"}
                      {p.has_credentials ? "" : " — no credentials"}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className="label">Model</span>
                <input
                  className="input font-mono text-sm"
                  value={model}
                  placeholder={meta?.fallback_model}
                  onChange={(e) => setModel(e.target.value)}
                  disabled={start.isPending || task?.status === "running"}
                  required
                />
              </label>
            </div>
            {meta && !meta.has_credentials ? (
              <p className="-mt-1 text-xs text-amber-300">
                No API key found for {provider}. The request will fail
                unless you set one via{" "}
                <code className="font-mono">jobapply config</code> or an
                env var.
              </p>
            ) : meta && model !== meta.default_model ? (
              <p className="-mt-1 text-xs text-slate-400">
                Override — your{" "}
                <code className="font-mono">jobapply.toml</code> default
                for <span className="font-mono">{provider}</span> is{" "}
                <span className="font-mono">{meta.default_model}</span>.
              </p>
            ) : (
              <p className="-mt-1 text-xs text-slate-500">
                Defaults loaded from{" "}
                <code className="font-mono">jobapply.toml</code>. Change
                them for this draft only.
              </p>
            )}
            {task && task.status !== "succeeded" ? (
              <div className="flex items-center gap-2 text-sm text-slate-300">
                <Loader2 size={14} className="animate-spin text-brand-400" />
                {task.progress.label || "Drafting…"}
              </div>
            ) : null}
            {start.isError ? (
              <p className="text-sm text-rose-400">
                {String(start.error?.message ?? start.error)}
              </p>
            ) : null}
            {task?.status === "failed" ? (
              <pre className="max-h-32 overflow-auto rounded bg-rose-900/40 p-2 text-xs text-rose-100">
                {task.error}
              </pre>
            ) : null}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="btn-secondary"
                onClick={onClose}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="btn-primary"
                disabled={
                  start.isPending ||
                  task?.status === "running" ||
                  providers.isLoading ||
                  !provider.trim() ||
                  !model.trim()
                }
              >
                {start.isPending || task?.status === "running" ? (
                  <Loader2 size={16} className="animate-spin" />
                ) : (
                  <Mail size={16} />
                )}
                Generate email
              </button>
            </div>
          </form>
        ) : (
          <div className="space-y-3">
            <div className="rounded-md border border-slate-700 bg-slate-950/40 p-3 text-sm">
              <div className="text-xs text-slate-400">To</div>
              <div className="text-slate-100">{draft.to}</div>
              <div className="mt-2 text-xs text-slate-400">Subject</div>
              <div className="text-slate-100">{draft.subject}</div>
              <div className="mt-2 text-xs text-slate-400">Body</div>
              <pre className="whitespace-pre-wrap font-sans text-slate-100">
                {draft.body}
              </pre>
            </div>
            {isTailored ? (
              <p className="text-xs text-slate-400">
                Mail links can&apos;t attach files. Attach{" "}
                <code className="font-mono">resume.pdf</code> and{" "}
                <code className="font-mono">cover_letter.pdf</code> yourself
                (Download them from the job page).
              </p>
            ) : null}
            <div className="flex flex-wrap items-center justify-end gap-2">
              <button
                className="btn-secondary"
                onClick={() => {
                  navigator.clipboard
                    ?.writeText(draft.text)
                    .catch(() => {});
                }}
              >
                Copy to clipboard
              </button>
              <a
                className="btn-primary"
                href={`mailto:${encodeURIComponent(draft.to)}?subject=${encodeURIComponent(draft.subject)}&body=${encodeURIComponent(draft.body)}`}
              >
                Open in mail client
              </a>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
