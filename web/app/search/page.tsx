import { searchBrain, type SearchHit } from "@/lib/data";

export const dynamic = "force-dynamic";

export default async function Search({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  const query = (q || "").trim();
  const hits = query ? await searchBrain(query, 12) : [];

  return (
    <div className="mx-auto max-w-4xl px-5 py-12 sm:px-8">
      <header className="rise flex items-center justify-between gap-4">
        <a href="/" className="font-mono text-sm text-muted transition-colors hover:text-accent">← devbrain</a>
        <span className="font-mono text-xs text-muted">semantic search · Gemini@1536</span>
      </header>

      <h1 className="rise display mt-8 text-5xl"><span className="grad-text">Ask the brain</span></h1>

      <form action="/search" method="get" className="rise mt-6 flex gap-2">
        <input
          name="q"
          defaultValue={query}
          autoFocus
          placeholder="Hỏi gì cũng được — vd: quyết định embedding provider, plan Phase 2…"
          className="flex-1 rounded-lg panel-glass px-4 py-3 font-mono text-sm text-fg placeholder:text-muted/60 outline-none focus:border-accent/60"
        />
        <button className="rounded-lg border border-accent/50 bg-accent/10 px-5 font-mono text-sm text-accent transition-colors hover:bg-accent/20">
          search →
        </button>
      </form>

      {query && (
        <p className="mt-4 font-mono text-xs text-muted">{hits.length} kết quả cho “{query}”</p>
      )}

      <ul className="stagger mt-5 flex flex-col gap-3">
        {hits.map((h, i) => <Hit key={i} h={h} />)}
      </ul>

      {query && hits.length === 0 && (
        <p className="mt-8 font-mono text-sm text-amber">
          Chưa có kết quả. Corpus embeddings còn mỏng — sẽ giàu dần khi PR merge.
        </p>
      )}
    </div>
  );
}

function Hit({ h }: { h: SearchHit }) {
  const pct = Math.round(h.similarity * 100);
  const pr = h.scope === "pr" && h.scopeRef ? `PR #${String(h.scopeRef).split("#").pop()} · ` : "";
  return (
    <li className="lift overflow-hidden rounded-xl panel-glass">
      <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
        <span className="display truncate text-[15px] text-fg">{pr}{h.title ?? "(no title)"}</span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-accent">{pct}%</span>
      </div>
      <p className="prose-serif px-4 py-3 text-[15px] text-muted">{h.content.slice(0, 240)}</p>
      <div className="flex items-center justify-between gap-3 px-4 pb-3 font-mono text-[11px] text-muted">
        <span>{h.occurredAt ?? ""}</span>
        {h.sourceUrl && (
          <a href={h.sourceUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">open source ↗</a>
        )}
      </div>
    </li>
  );
}
