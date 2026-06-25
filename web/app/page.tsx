import {
  getSummary,
  getHotFiles,
  getCoChanges,
  getProjects,
  type HotFile,
  type CoChange,
} from "@/lib/data";

// Query Postgres per request (db isn't reachable at docker build time).
export const dynamic = "force-dynamic";

const VIEWS = [
  { key: "search", label: "Semantic search", phase: "P2" },
  { key: "pyramid", label: "Pyramid heatmap", phase: "P3" },
  { key: "roadmap", label: "Roadmap", phase: "P3" },
  { key: "graph", label: "Knowledge graph", phase: "P4" },
];

function heatLevel(commits: number, max: number): number {
  if (max <= 0 || commits <= 0) return 0;
  const r = commits / max;
  if (r > 0.8) return 5;
  if (r > 0.55) return 4;
  if (r > 0.32) return 3;
  if (r > 0.15) return 2;
  return 1;
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ project?: string }>;
}) {
  const { project } = await searchParams;
  const [summary, projects, hot, co] = await Promise.all([
    getSummary(),
    getProjects(),
    getHotFiles(28, project),
    getCoChanges(16, project),
  ]);
  const maxCommits = hot.reduce((m, f) => Math.max(m, f.commits), 0);
  const maxWeight = co.reduce((m, c) => Math.max(m, c.weight), 0);
  const empty = summary.events === 0;

  return (
    <div className="mx-auto max-w-6xl px-5 py-10 sm:px-8">
      <header className="rise flex flex-col gap-6 border-b border-line pb-8">
        <div className="flex items-baseline justify-between gap-4">
          <div className="flex items-center gap-3">
            <span className="h-2.5 w-2.5 rounded-full bg-accent shadow-[0_0_12px_2px_var(--color-accent)]" />
            <h1 className="font-mono text-2xl font-bold tracking-tight">devbrain</h1>
          </div>
          <div className="flex items-center gap-4"><a href="/search" className="font-mono text-xs text-accent hover:underline">⌕ search</a><a href="/entities" className="font-mono text-xs text-accent hover:underline">entities</a><a href="/pyramid" className="font-mono text-xs text-accent hover:underline">pyramid</a><a href="/roadmap" className="font-mono text-xs text-accent hover:underline">roadmap</a><span className="font-mono text-xs text-muted">deterministic backbone · $0 LLM</span></div>
        </div>
        <p className="max-w-2xl text-sm leading-6 text-muted">
          A living changelog for the team — <span className="text-fg">events</span> are the immutable
          source of truth; the heat and co-change below are deterministic projections rebuilt from git
          history. No AI scoring, ever.
        </p>
        {projects.length > 1 && (
          <div className="flex flex-wrap items-center gap-2 font-mono text-xs">
            <span className="text-muted">project:</span>
            <a href="/" className={!project ? "text-accent" : "text-muted hover:text-fg"}>all</a>
            {projects.map((p) => (
              <a
                key={p}
                href={`/?project=${encodeURIComponent(p)}`}
                className={project === p ? "text-accent" : "text-muted hover:text-fg"}
              >
                {p}
              </a>
            ))}
          </div>
        )}
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="events" value={summary.events} />
          <Stat label="entities" value={summary.entities} />
          <Stat label="co-change edges" value={summary.edges} />
          <Stat label="last activity" text={summary.lastEvent ? summary.lastEvent.slice(0, 10) : "—"} />
        </dl>
      </header>

      {empty && (
        <p className="rise mt-8 rounded-md border border-line bg-panel px-4 py-3 font-mono text-sm text-amber">
          No data yet. Run{" "}
          <span className="text-fg">python pipeline/backfill.py &lt;repo&gt;</span> to seed events.
        </p>
      )}

      <section className="rise mt-10" style={{ animationDelay: "60ms" }}>
        <SectionTitle index="01" title="Activity heat" hint="commits per file · all-time · precursor to the pyramid" />
        <div className="mt-5 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {hot.map((f) => (
            <HeatCell key={f.key} f={f} level={heatLevel(f.commits, maxCommits)} />
          ))}
        </div>
      </section>

      <section className="rise mt-12" style={{ animationDelay: "120ms" }}>
        <SectionTitle index="02" title="Co-change" hint="files that change together · weight = co-occurrences · precursor to the graph" />
        <ul className="mt-5 flex flex-col divide-y divide-line overflow-hidden rounded-lg border border-line bg-panel">
          {co.map((c, i) => (
            <CoRow key={i} c={c} max={maxWeight} />
          ))}
        </ul>
      </section>

      <section className="rise mt-12 mb-8" style={{ animationDelay: "180ms" }}>
        <SectionTitle index="03" title="Planned views" hint="unlocked in later phases" />
        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {VIEWS.map((v) => (
            <div
              key={v.key}
              className="flex items-center justify-between rounded-md border border-dashed border-line bg-panel/40 px-3 py-3"
            >
              <span className="text-sm text-muted">{v.label}</span>
              <span className="font-mono text-[10px] text-muted/70">{v.phase}</span>
            </div>
          ))}
        </div>
      </section>

      <footer className="border-t border-line py-6 font-mono text-[11px] leading-5 text-muted">
        events → projector (cursor + logic_version) → metrics_daily · entity_edges → this view
      </footer>
    </div>
  );
}

function Stat({ label, value, text }: { label: string; value?: number; text?: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-4 py-3">
      <div className="font-mono text-2xl font-semibold tabular-nums text-fg">
        {text ?? value?.toLocaleString()}
      </div>
      <div className="mt-1 font-mono text-[11px] uppercase tracking-wider text-muted">{label}</div>
    </div>
  );
}

function SectionTitle({ index, title, hint }: { index: string; title: string; hint: string }) {
  return (
    <div className="flex items-end justify-between gap-4 border-b border-line pb-2">
      <h2 className="flex items-baseline gap-3">
        <span className="font-mono text-xs text-accent">{index}</span>
        <span className="text-lg font-semibold">{title}</span>
      </h2>
      <span className="hidden font-mono text-[11px] text-muted sm:block">{hint}</span>
    </div>
  );
}

function HeatCell({ f, level }: { f: HotFile; level: number }) {
  return (
    <div
      className="group flex flex-col justify-between gap-3 overflow-hidden rounded-md border border-line p-3 transition-transform duration-200 hover:-translate-y-0.5"
      style={{ backgroundColor: `var(--color-heat-${level})` }}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="truncate font-mono text-[13px] text-fg" title={f.key}>
          {f.name}
        </span>
        {f.active30d && (
          <span className="shrink-0 rounded-sm bg-black/30 px-1 py-0.5 font-mono text-[9px] text-accent">
            30d
          </span>
        )}
      </div>
      <div className="flex items-end justify-between font-mono text-[11px] text-fg/80">
        <span className="text-base font-bold tabular-nums text-fg">{f.commits}</span>
        <span>±{f.churn.toLocaleString()}</span>
      </div>
    </div>
  );
}

function CoRow({ c, max }: { c: CoChange; max: number }) {
  const pct = max > 0 ? Math.round((c.weight / max) * 100) : 0;
  return (
    <li className="relative flex items-center justify-between gap-4 px-4 py-2.5">
      <span className="absolute inset-y-0 left-0 bg-accent/10" style={{ width: `${pct}%` }} aria-hidden />
      <span className="relative z-10 truncate font-mono text-[13px]">
        <span className="text-fg">{c.a}</span>
        <span className="mx-2 text-muted">↔</span>
        <span className="text-fg">{c.b}</span>
      </span>
      <span className="relative z-10 shrink-0 font-mono text-sm tabular-nums text-accent">{c.weight}</span>
    </li>
  );
}
