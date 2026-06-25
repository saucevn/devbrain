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
    <div className="mx-auto max-w-4xl px-5 py-10 sm:px-8">
      <header className="rise flex items-center justify-between gap-4 border-b border-line pb-6">
        <a href="/" className="font-mono text-sm text-muted hover:text-fg">← devbrain</a>
        <span className="font-mono text-xs text-muted">semantic search · Gemini@1536</span>
      </header>

      <form action="/search" method="get" className="rise mt-8 flex gap-2">
        <input
          name="q"
          defaultValue={query}
          autoFocus
          placeholder="Hỏi gì cũng được — vd: quyết định embedding provider, plan Phase 2…"
          className="flex-1 rounded-md border border-line bg-panel px-4 py-2.5 font-mono text-sm text-fg placeholder:text-muted/60 outline-none focus:border-accent"
        />
        <button className="rounded-md border border-accent bg-accent/10 px-4 py-2.5 font-mono text-sm text-accent hover:bg-accent/20">
          search
        </button>
      </form>

      {query && (
        <p className="mt-4 font-mono text-xs text-muted">
          {hits.length} kết quả cho “{query}”
        </p>
      )}

      <ul className="rise mt-4 flex flex-col gap-3">
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
  return (
    <li className="overflow-hidden rounded-lg border border-line bg-panel">
      <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-2">
        <span className="truncate font-mono text-[13px] text-fg">
          {h.scope === "pr" && h.scopeRef ? `PR #${h.scopeRef} · ` : ""}{h.title ?? "(no title)"}
        </span>
        <span className="shrink-0 font-mono text-xs text-accent tabular-nums">{pct}%</span>
      </div>
      <p className="px-4 py-3 text-sm leading-6 text-muted">{h.content.slice(0, 240)}</p>
      <div className="flex items-center justify-between gap-3 px-4 pb-3 font-mono text-[11px] text-muted">
        <span>{h.occurredAt ?? ""}</span>
        {h.sourceUrl && (
          <a href={h.sourceUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            open source ↗
          </a>
        )}
      </div>
    </li>
  );
}
