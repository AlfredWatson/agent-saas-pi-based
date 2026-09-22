import { useEffect, useState, type FormEvent, type PropsWithChildren, type ReactNode } from "react";
import { X } from "lucide-react";
import { errorCode, readableError } from "./lib/errors";

export function Modal({ open, title, onClose, children }: PropsWithChildren<{ open: boolean; title: string; onClose: () => void }>) {
  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [open, onClose]);
  if (!open) return null;
  return <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/70 p-4" role="dialog" aria-modal="true" aria-label={title}>
    <section className="panel max-h-[90vh] w-full max-w-xl overflow-auto p-5 shadow-2xl">
      <header className="mb-4 flex items-center justify-between gap-3"><h2 className="text-lg font-semibold">{title}</h2><button className="btn p-2" onClick={onClose} aria-label="关闭"><X size={17} /></button></header>
      {children}
    </section>
  </div>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  const code = errorCode(error);
  return <div className="rounded-md border border-rose-800/70 bg-rose-950/40 p-3 text-sm text-rose-100">
    <p>{readableError(error)}</p>{code && <p className="mt-1 font-mono text-xs text-rose-300">{code}</p>}
  </div>;
}

export function Empty({ children }: PropsWithChildren) { return <div className="panel grid min-h-40 place-items-center p-6 text-center text-sm muted">{children}</div>; }

export function ConfirmButton({ label, confirmText, onConfirm, className = "btn-danger" }: { label: ReactNode; confirmText: string; onConfirm: () => void; className?: string }) {
  const [armed, setArmed] = useState(false);
  return <button className={`btn ${className}`} onClick={() => armed ? (setArmed(false), onConfirm()) : setArmed(true)}>{armed ? confirmText : label}</button>;
}

export function Form({ onSubmit, children }: PropsWithChildren<{ onSubmit: (event: FormEvent<HTMLFormElement>) => void }>) {
  return <form className="space-y-4" onSubmit={onSubmit}>{children}</form>;
}
