import { getSubgraph, type GraphNode } from "@/lib/data";

export const dynamic = "force-dynamic";

// Edge color by relation (SVG strokes use currentColor + a text-* class).
const EDGE_COLOR: Record<string, string> = {
  co_changed_with: "text-muted",
  depends_on: "text-accent",
  documented_by: "text-amber",
  implements: "text-fg",
};

const nodeFill = (n: GraphNode): string =>
  n.seed ? "var(--color-accent, #6ee7b7)"
    : n.kind === "doc" || n.kind === "epic" ? "var(--color-amber, #fbbf24)"
    : "var(--color-fg, #e5e7eb)";

const short = (s: string, n = 22) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

export default async function Graph({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const { q } = await searchParams;
  const g = await getSubgraph(q?.trim() || undefined);

  // Radial layout: seed at center, neighbors on a ring.
  const W = 760, H = 540, cx = W / 2, cy = H / 2, R = 210;
  const others = g.nodes.filter((n) => !n.seed);
  const pos = new Map<string, { x: number; y: number }>();
  if (g.center) pos.set(g.center, { x: cx, y: cy });
  others.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(others.length, 1) - Math.PI / 2;
    pos.set(n.id, { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a) });
  });

  return (
    <div className="mx-auto max-w-4xl px-5 py-10 sm:px-8">
      <header className="rise flex items-center justify-between gap-4">
        <a href="/" className="font-mono text-sm text-muted transition-colors hover:text-accent">← devbrain</a>
        <span className="font-mono text-xs text-muted">knowledge graph · subgraph-by-query</span>
      </header>
      <h1 className="rise display mt-8 text-5xl"><span className="grad-text">Knowledge graph</span></h1>

      <form action="/graph" method="get" className="rise mt-6 flex gap-2">
        <input
          name="q"
          defaultValue={q ?? ""}
          placeholder="seed entity — file path, basename, or name (vd: posting_engine.py)"
          className="flex-1 rounded-md border border-line bg-panel px-3 py-2 font-mono text-sm text-fg outline-none placeholder:text-muted focus:border-accent/50"
        />
        <button className="rounded-md border border-accent/40 bg-panel px-4 font-mono text-sm text-accent hover:border-accent">graph →</button>
      </form>

      {g.center ? (
        <>
          <p className="rise mt-4 font-mono text-xs text-muted">
            seed: <span className="text-fg">{g.centerName}</span> · {others.length} neighbors ·
            click a node to re-center
          </p>
          <div className="rise mt-4 overflow-x-auto rounded-lg border border-line bg-panel">
            <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full">
              {g.edges.map((e, i) => {
                const s = pos.get(e.source), t = pos.get(e.target);
                if (!s || !t) return null;
                return (
                  <line
                    key={i}
                    x1={s.x} y1={s.y} x2={t.x} y2={t.y}
                    stroke="currentColor" strokeWidth={Math.min(1 + e.weight, 4)}
                    className={`${EDGE_COLOR[e.type] ?? "text-muted"} opacity-50`}
                  />
                );
              })}
              {g.nodes.map((n) => {
                const p = pos.get(n.id);
                if (!p) return null;
                return (
                  <a key={n.id} href={`/graph?q=${encodeURIComponent(n.key)}`}>
                    <circle cx={p.x} cy={p.y} r={n.seed ? 9 : 6} fill={nodeFill(n)} />
                    <text
                      x={p.x} y={p.y - 11}
                      textAnchor="middle"
                      className="fill-fg font-mono"
                      fontSize={n.seed ? 12 : 10}
                    >
                      {short(n.name)}
                    </text>
                  </a>
                );
              })}
            </svg>
          </div>

          <div className="rise mt-3 flex flex-wrap gap-3 font-mono text-[11px] text-muted">
            <span className="text-accent">— depends_on</span>
            <span className="text-amber">— documented_by</span>
            <span className="text-fg">— implements</span>
            <span className="text-muted">— co_changed_with</span>
          </div>
        </>
      ) : (
        <p className="mt-8 font-mono text-sm text-amber">
          Chưa có entity/edge nào để dựng graph. Chạy backfill + AI lane (PR narrative) để sinh entity & quan hệ.
        </p>
      )}
    </div>
  );
}
