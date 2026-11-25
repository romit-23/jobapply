import { useEffect, useState } from "react";
import { Loader2, Wand2, X } from "lucide-react";
import { useProviders } from "@/lib/hooks";
import { useDialog } from "@/lib/useDialog";

export interface TailorOptions {
  provider: string;
  model: string;
  no_pdf: boolean;
  /** Only meaningful when ``showForce`` is true (batch mode); the
   *  parent forwards this to the run endpoint to re-tailor jobs
   *  already in ``done`` status. */
  force: boolean;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onConfirm: (options: TailorOptions) => Promise<void> | void;
  /** ``true`` while the parent's mutation is in flight; we show a
   *  spinner and disable the form so users can't double-submit. */
  pending?: boolean;
  /** Request errors stay in the dialog so the user can fix options and retry. */
  error?: string;
  /** Optional copy override — e.g. "Re-tailor" when the job has
   *  already been processed once. */
  title?: string;
  /** Single-job mode: shown above the form so users can confirm
   *  they're tailoring the right role. */
  jobTitle?: string;
  /** Batch mode: number of selected jobs. Mutually exclusive with
   *  ``jobTitle``. Toggles the "Force re-tailor" checkbox so users
   *  can override the run endpoint's default skip-already-done
   *  behaviour. */
  jobCount?: number;
}

/** Confirmation modal shown when the user clicks "Tailor resume +
 *  cover letter". Pre-fills the provider / model from the active
 *  ``jobapply.toml`` defaults but lets the user override either
 *  field for this run only. We deliberately make the user click
 *  through this dialog instead of firing on first click so they
 *  notice the LLM that's about to spend their tokens. */
export default function TailorModal({
  open,
  onClose,
  onConfirm,
  pending,
  error,
  title,
  jobTitle,
  jobCount,
}: Props) {
  const providers = useProviders();
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [noPdf, setNoPdf] = useState(false);
  const [force, setForce] = useState(false);
  const isBatch = typeof jobCount === "number";

  // Hydrate the form from /api/providers once it lands. We re-run
  // this whenever the modal re-opens so a state change between
  // tailor invocations (e.g. user switched the toml default) is
  // reflected.
  useEffect(() => {
    if (!open || !providers.data) return;
    setProvider(providers.data.active_provider);
    setModel(providers.data.active_model);
  }, [open, providers.data]);

  const dialogRef = useDialog<HTMLFormElement>(open, onClose, !pending);

  if (!open) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (pending) return;
    if (!provider.trim() || !model.trim()) return;
    await onConfirm({
      provider: provider.trim(),
      model: model.trim(),
      no_pdf: noPdf,
      force,
    });
  };

  const meta = providers.data?.providers.find((p) => p.name === provider);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-sm">
      <form
        ref={dialogRef}
        onSubmit={submit}
        role="dialog"
        aria-modal="true"
        aria-labelledby="tailor-modal-title"
        className="card w-full max-w-lg space-y-4 border-slate-700 p-6"
      >
        <div
          id="tailor-modal-title"
          className="flex items-center gap-2 text-base font-semibold text-slate-100"
        >
          <Wand2 size={18} className="text-brand-400" aria-hidden="true" />
          {title ??
            (isBatch
              ? "Tailor selected jobs"
              : "Tailor resume + cover letter")}
          <button
            type="button"
            className="ml-auto text-slate-400 hover:text-slate-100"
            onClick={onClose}
            aria-label="Close"
            disabled={pending}
          >
            <X size={18} />
          </button>
        </div>
        {isBatch ? (
          <p className="text-sm text-slate-400">
            About to tailor{" "}
            <span className="font-medium text-slate-200">
              {jobCount} {jobCount === 1 ? "job" : "jobs"}
            </span>{" "}
            in sequence. Each one will run resume + cover-letter agents
            against your <code className="font-mono">profile.json</code>.
          </p>
        ) : jobTitle ? (
          <p className="text-sm text-slate-400">
            Tailoring for{" "}
            <span className="font-medium text-slate-200">{jobTitle}</span>.
          </p>
        ) : null}

        <label className="block">
          <span className="label">Provider</span>
          <select
            className="input"
            value={provider}
            disabled={providers.isLoading || pending}
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
          {meta && !meta.has_credentials ? (
            <p className="mt-1 text-xs text-amber-300">
              No API key found for this provider. The request will fail
              unless you set one via{" "}
              <code className="font-mono">jobapply config</code> or an env
              var.
            </p>
          ) : null}
        </label>

        <label className="block">
          <span className="label">Model</span>
          <input
            className="input font-mono text-sm"
            value={model}
            placeholder={meta?.fallback_model}
            onChange={(e) => setModel(e.target.value)}
            disabled={pending}
            required
          />
          {meta && model !== meta.default_model ? (
            <p className="mt-1 text-xs text-slate-400">
              Override — your{" "}
              <code className="font-mono">jobapply.toml</code> default for{" "}
              <span className="font-mono">{provider}</span> is{" "}
              <span className="font-mono">{meta.default_model}</span>.
            </p>
          ) : (
            <p className="mt-1 text-xs text-slate-500">
              Default loaded from{" "}
              <code className="font-mono">jobapply.toml</code>. Change it for
              this run only.
            </p>
          )}
        </label>

        <div className="space-y-2">
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              className="accent-brand-500"
              checked={noPdf}
              onChange={(e) => setNoPdf(e.target.checked)}
              disabled={pending}
            />
            Skip PDF rendering (markdown + LaTeX only — faster)
          </label>
          {isBatch ? (
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                className="accent-brand-500"
                checked={force}
                onChange={(e) => setForce(e.target.checked)}
                disabled={pending}
              />
              Force re-tailor even if already done
            </label>
          ) : null}
        </div>

        {error ? (
          <p
            role="alert"
            className="rounded-md border border-rose-800/50 bg-rose-950/30 p-3 text-sm text-rose-200"
          >
            {error}
          </p>
        ) : null}

        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            className="btn-secondary"
            onClick={onClose}
            disabled={pending}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={
              pending ||
              providers.isLoading ||
              !provider.trim() ||
              !model.trim()
            }
          >
            {pending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Wand2 size={14} />
            )}
            Start tailoring
          </button>
        </div>
      </form>
    </div>
  );
}
