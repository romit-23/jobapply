import { useEffect, useRef } from "react";

const FOCUSABLE =
  'input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

/** Keyboard + focus behaviour for a modal dialog.
 *
 *  - moves focus into the dialog when it opens (first form field, else
 *    the first focusable element) and restores it on close;
 *  - closes on Escape unless `canClose` is false (e.g. a request is
 *    in flight);
 *  - keeps Tab / Shift+Tab inside the dialog.
 *
 *  Attach the returned ref to the dialog panel and give that element
 *  `role="dialog"`, `aria-modal="true"` and `aria-labelledby`.
 */
export function useDialog<T extends HTMLElement>(
  open: boolean,
  onClose: () => void,
  canClose = true,
) {
  const ref = useRef<T>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const canCloseRef = useRef(canClose);
  canCloseRef.current = canClose;

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const panel = ref.current;
    const items = () =>
      panel ? Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)) : [];
    const first =
      panel?.querySelector<HTMLElement>(
        "input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
      ) ?? items()[0];
    first?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (canCloseRef.current) {
          e.preventDefault();
          onCloseRef.current();
        }
        return;
      }
      if (e.key !== "Tab") return;
      const list = items();
      if (!list.length) return;
      const firstEl = list[0];
      const lastEl = list[list.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previouslyFocused?.focus?.();
    };
  }, [open]);

  return ref;
}
