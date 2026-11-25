import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Eye,
  Hammer,
  Loader2,
  RotateCcw,
  Save,
} from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { StreamLanguage } from "@codemirror/language";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { EditorView } from "@codemirror/view";
import {
  useArtifactText,
  useCompileArtifact,
  useJob,
  useSaveArtifact,
} from "@/lib/hooks";
import { api } from "@/lib/api";

// Whitelist mirrors the FastAPI ``EDITABLE_ARTIFACTS`` set.
const TABS = [
  { name: "resume.tex", label: "Resume (LaTeX)" },
  { name: "cover_letter.tex", label: "Cover letter (LaTeX)" },
  { name: "resume.md", label: "Resume (Markdown)" },
  { name: "cover_letter.md", label: "Cover letter (Markdown)" },
] as const;

const PDF_MAP: Record<string, string | null> = {
  "resume.tex": "resume.pdf",
  "cover_letter.tex": "cover_letter.pdf",
  "resume.md": null,
  "cover_letter.md": null,
};

type ArtifactName = (typeof TABS)[number]["name"];

function isEditable(name: string): name is ArtifactName {
  return TABS.some((t) => t.name === name);
}

/** Full-screen editor: edits the LaTeX source for resume / cover
 *  letter, saves to disk, and recompiles to PDF on demand.
 *
 *  We split the screen left/right with the editor on the left and a
 *  PDF preview iframe on the right. The iframe URL carries a
 *  cache-busting `?v=<mtime>` so the browser actually re-renders the
 *  PDF after a successful compile (Chromium's PDF viewer aggressively
 *  caches the URL otherwise).
 */
export default function ArtifactEditor() {
  const { jobId, name } = useParams<{ jobId: string; name: string }>();
  const navigate = useNavigate();
  const job = useJob(jobId);

  // Validate the route param up front so we never call the editable-
  // only API endpoints with a bogus filename.
  useEffect(() => {
    if (name && !isEditable(name)) {
      navigate(`/jobs/${jobId}/edit/resume.tex`, { replace: true });
    }
  }, [name, jobId, navigate]);

  if (!jobId || !name || !isEditable(name)) return null;

  return <Editor jobId={jobId} name={name} job={job} />;
}

interface EditorProps {
  jobId: string;
  name: ArtifactName;
  job: ReturnType<typeof useJob>;
}

function Editor({ jobId, name, job }: EditorProps) {
  const text = useArtifactText(jobId, name);
  const save = useSaveArtifact(jobId, name);
  const compile = useCompileArtifact(jobId, name);

  // We mirror the server text into local state so the editor stays
  // responsive while the user types — only flushing back on save.
  const [draft, setDraft] = useState<string>("");
  const baselineRef = useRef<string>("");
  // Cache-buster for the PDF preview iframe — bumped after every
  // successful compile so the browser actually reloads the file.
  const [pdfVersion, setPdfVersion] = useState<number>(() => Date.now());

  useEffect(() => {
    if (text.data !== undefined) {
      setDraft(text.data);
      baselineRef.current = text.data;
    }
  }, [text.data]);

  const dirty = draft !== baselineRef.current;
  const pdfName = PDF_MAP[name];
  const pdfAvailable = Boolean(
    pdfName && job.data?.available_artifacts?.[pdfName],
  );

  const onSave = async () => {
    const snap = draft;
    await save.mutateAsync(snap);
    baselineRef.current = snap;
  };

  const onSaveAndCompile = async () => {
    const snap = draft;
    if (snap !== baselineRef.current) {
      await save.mutateAsync(snap);
      baselineRef.current = snap;
    }
    if (pdfName) {
      await compile.mutateAsync();
      setPdfVersion(Date.now());
    }
  };

  const onRevert = () => {
    setDraft(baselineRef.current);
  };

  // CodeMirror extension list — we only use the ``stex`` syntax for
  // LaTeX files; Markdown gets the default plain-text experience to
  // keep the bundle small (no markdown grammar import).
  const extensions = useMemo(() => {
    const exts = [
      EditorView.lineWrapping,
      EditorView.theme({
        "&": { fontSize: "13px" },
        ".cm-content": { fontFamily: "ui-monospace, SFMono-Regular, monospace" },
      }),
    ];
    if (name.endsWith(".tex")) {
      exts.push(StreamLanguage.define(stex));
    }
    return exts;
  }, [name]);

  const tabName = TABS.find((t) => t.name === name)?.label ?? name;

  // The whole editor page is locked to the viewport so the source
  // editor and the PDF preview each scroll *inside* their card. The
  // 230px subtracted accounts for the layout chrome (header + footer
  // + main padding) plus the editor's own breadcrumb / tab rows. We
  // keep a generous floor so very short viewports still get a usable
  // editor area.
  return (
    <div className="flex h-[calc(100vh-230px)] min-h-[520px] flex-col gap-3 overflow-hidden">
      <div className="flex flex-wrap items-center gap-2">
        <Link to={`/jobs/${jobId}`} className="btn-ghost">
          <ArrowLeft size={14} /> Back to job
        </Link>
        <h1 className="text-base font-semibold text-slate-100">
          {job.data?.title || job.data?.company || jobId}
          <span className="ml-2 font-mono text-xs text-slate-500">
            {tabName}
          </span>
        </h1>
        <span
          className={
            "ml-auto rounded-full px-2 py-0.5 text-[11px] font-medium " +
            (dirty
              ? "bg-amber-500/15 text-amber-200 ring-1 ring-amber-500/40"
              : "bg-slate-800 text-slate-400")
          }
        >
          {dirty ? "unsaved changes" : "in sync with disk"}
        </span>
      </div>

      <nav className="flex flex-wrap gap-1 border-b border-slate-800 pb-1 text-sm">
        {TABS.map((t) => (
          <Link
            key={t.name}
            to={`/jobs/${jobId}/edit/${t.name}`}
            className={
              "rounded-md px-3 py-1.5 transition-colors " +
              (t.name === name
                ? "bg-slate-800 text-slate-100"
                : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-100")
            }
          >
            {t.label}
          </Link>
        ))}
      </nav>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="card flex min-h-0 flex-col overflow-hidden p-0">
          <div className="flex flex-shrink-0 items-center gap-2 border-b border-slate-800 px-3 py-2">
            <Hammer size={14} className="text-slate-400" />
            <span className="text-xs uppercase tracking-wide text-slate-400">
              Source
            </span>
            <div className="ml-auto flex items-center gap-2">
              <button
                className="btn-ghost text-xs"
                onClick={onRevert}
                disabled={!dirty || save.isPending}
              >
                <RotateCcw size={12} /> Revert
              </button>
              <button
                className="btn-secondary text-xs"
                onClick={onSave}
                disabled={!dirty || save.isPending}
              >
                {save.isPending ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Save size={12} />
                )}
                Save
              </button>
              {pdfName ? (
                <button
                  className="btn-primary text-xs"
                  onClick={onSaveAndCompile}
                  disabled={save.isPending || compile.isPending}
                >
                  {compile.isPending ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <Hammer size={12} />
                  )}
                  Save &amp; compile PDF
                </button>
              ) : null}
            </div>
          </div>

          {text.isLoading ? (
            <div className="flex flex-1 items-center justify-center gap-2 text-sm text-slate-400">
              <Loader2 size={16} className="animate-spin" /> Loading source…
            </div>
          ) : text.error ? (
            <div className="flex flex-1 items-center gap-2 p-4 text-sm text-rose-300">
              <AlertTriangle size={16} />{" "}
              {String((text.error as Error)?.message ?? "Failed to load")}
            </div>
          ) : (
            // ``min-h-0`` is the magic that lets ``flex-1`` actually
            // shrink past the editor's natural content height; without
            // it CodeMirror would size to its content and push the
            // outer page into a scrollbar. With it, CodeMirror sits in
            // a bounded box and provides its own internal scroll.
            <div className="min-h-0 flex-1">
              <CodeMirror
                value={draft}
                onChange={(v) => setDraft(v)}
                theme="dark"
                height="100%"
                style={{ height: "100%" }}
                extensions={extensions}
                basicSetup={{
                  lineNumbers: true,
                  highlightActiveLine: true,
                  foldGutter: true,
                  bracketMatching: true,
                  closeBrackets: true,
                  highlightSelectionMatches: false,
                }}
              />
            </div>
          )}

          <StatusFooter
            saveError={save.error as Error | null}
            saveSuccess={save.isSuccess && !dirty}
            compileError={compile.error as Error | null}
            compileSuccess={compile.isSuccess}
            backend={compile.data?.backend}
          />
        </div>

        <div className="card flex min-h-0 flex-col overflow-hidden p-0">
          <div className="flex flex-shrink-0 items-center gap-2 border-b border-slate-800 px-3 py-2">
            <Eye size={14} className="text-slate-400" />
            <span className="text-xs uppercase tracking-wide text-slate-400">
              {pdfName ? `Preview · ${pdfName}` : "Preview"}
            </span>
            {pdfAvailable && pdfName ? (
              <a
                className="ml-auto text-xs text-brand-300 hover:text-brand-200"
                href={api.artifactUrl(jobId, pdfName)}
                target="_blank"
                rel="noreferrer"
              >
                Open in new tab ↗
              </a>
            ) : null}
          </div>
          {pdfName && pdfAvailable ? (
            // The browser's PDF viewer handles its own internal
            // scrolling — we just give the iframe a bounded box.
            <iframe
              key={`${pdfName}-${pdfVersion}`}
              src={`${api.artifactUrl(jobId, pdfName)}?v=${pdfVersion}`}
              className="min-h-0 w-full flex-1 bg-slate-950"
              title={`${pdfName} preview`}
            />
          ) : (
            <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 overflow-auto p-6 text-center text-sm text-slate-400">
              <Hammer size={20} className="text-slate-500" />
              <p className="max-w-xs">
                {pdfName
                  ? "No PDF on disk yet. Click "
                  : "Markdown sources don't compile to PDF directly. Edit the .tex file or "}
                <span className="font-semibold text-slate-200">
                  {pdfName
                    ? "Save & compile PDF"
                    : "use the markdown tab in the artifacts list."}
                </span>
                {pdfName ? " to render this source." : ""}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

interface FooterProps {
  saveError: Error | null;
  saveSuccess: boolean;
  compileError: Error | null;
  compileSuccess: boolean;
  backend?: string;
}

function StatusFooter({
  saveError,
  saveSuccess,
  compileError,
  compileSuccess,
  backend,
}: FooterProps) {
  const hasMessage =
    saveError || saveSuccess || compileError || compileSuccess;
  if (!hasMessage) return null;

  return (
    <div className="flex-shrink-0 border-t border-slate-800 px-3 py-2 text-xs">
      {compileError ? (
        <div className="flex items-start gap-2 text-rose-300" role="alert">
          <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
          <span className="whitespace-pre-line font-mono">
            {compileError.message}
          </span>
        </div>
      ) : compileSuccess ? (
        <div className="flex items-center gap-2 text-brand-300">
          <CheckCircle2 size={14} />
          PDF rebuilt via{" "}
          <span className="font-mono">{backend || "tex_to_pdf"}</span>.
        </div>
      ) : saveError ? (
        <div className="flex items-start gap-2 text-rose-300">
          <AlertTriangle size={14} className="mt-0.5 flex-shrink-0" />
          <span>{saveError.message}</span>
        </div>
      ) : saveSuccess ? (
        <div className="flex items-center gap-2 text-brand-300">
          <CheckCircle2 size={14} /> Saved to disk.
        </div>
      ) : null}
    </div>
  );
}
