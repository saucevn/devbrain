"use client";

// Root error boundary: lỗi server chưa bắt render trang này thay vì trang
// 500 thô của Next (dev mode lộ stack).
export default function Error({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="mx-auto max-w-3xl px-5 py-24 text-center">
      <h1 className="display text-3xl text-fg">Có lỗi xảy ra</h1>
      <p className="mt-3 font-mono text-sm text-muted">Trang gặp lỗi tạm thời — thử lại.</p>
      <button
        onClick={reset}
        className="mt-6 rounded-lg border border-accent/50 bg-accent/10 px-5 py-2 font-mono text-sm text-accent transition-colors hover:bg-accent/20"
      >
        retry →
      </button>
    </div>
  );
}
