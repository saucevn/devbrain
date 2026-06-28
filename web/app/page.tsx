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

const NAV = [
  ["/search", "search"],
  ["/entities", "entities"],
  ["/pyramid", "pyramid"],
  ["/roadmap", "roadmap"],
  ["/graph", "graph"],
  ["/changelog", "changelog"],
  ["/stale", "stale"],
];

const EXPLORE = [
  ["/search", "Semantic search", "ask the brain — VN/EN, with citations"],
  ["/changelog", "Changelog", "AI narrative diary, newest first"],
  ["/graph", "Knowledge graph", "subgraph-by-query around any entity"],
  ["/pyramid", "Pyramid heatmap", "capability layers + deterministic heat"],
  ["/roadmap", "Roadmap", "milestones by year"],
  ["/stale", "Stale-doc watch", "knowledge-drift, deterministic"],
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
    <div className="mx-auto max-w-6xl px-5 py-12 sm:px-8">
      {/* ---- hero ---- */}
      <header className="rise relative">
        <div className="pointer-events-none absolute -top-24 left-1/4 h-64 w-64 rounded-full bg-accent/20 blur-[120px]" aria-hidden />
        <nav className="relative flex flex-wrap items-center justify-end gap-x-4 gap-y-2 font-mono text-xs">
          {NAV.map(([href, label]) => (
            <a key={href} href={href} className="text-muted transition-colors hover:text-accent">
              {label}
            </a>
          ))}
        </nav>

        <div className="relative mt-8 flex items-center gap-3">
          <span className="pulse h-2.5 w-2.5 rounded-full bg-accent" />
          <span className="font-mono text-xs uppercase tracking-[0.3em] text-muted">live · event-sourced</span>
        </div>
        <h1 className="display mt-3 text-6xl sm:text-7xl">
          <span className="grad-text">devbrain</span>
        </h1>
        <p className="prose-serif mt-5 max-w-2xl text-xl text-muted">
          A living changelog for the team. <span className="italic text-fg">Events</span> are the immutable
          source of truth; the heat and co-change below are deterministic projections rebuilt from git
          history — <span className="text-fg">no AI scoring, ever</span>.
        </p>

        {projects.length > 1 && (
          <div className="mt-6 flex flex-wrap items-center gap-2 font-mono text-xs">
            <span className="text-muted">project</span>
            <a href="/" className={chip(!project)}>all</a>
            {projects.map((p) => (
              <a key={p} href={`/?project=${encodeURIComponent(p)}`} className={chip(project === p)}>
                {p}
              </a>
            ))}
          </div>
        )}

        <dl className="stagger mt-8 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="events" value={summary.events} />
          <Stat label="entities" value={summary.entities} />
          <Stat label="edges" value={summary.edges} />
          <Stat label="last activity" text={summary.lastEvent ? summary.lastEvent.slice(0, 10) : "—"} />
        </dl>
        <div className="rule-brand mt-10" />
      </header>

      {empty && (
        <p className="rise mt-8 rounded-lg panel-glass px-4 py-3 font-mono text-sm text-amber">
          No data yet. Run <span className="text-fg">python pipeline/backfill.py &lt;repo&gt;</span> to seed events.
        </p>
      )}

      <section className="rise mt-12" style={{ animationDelay: "60ms" }}>
        <SectionTitle index="01" title="Activity heat" hint="commits per file · all-time · precursor to the pyramid" />
        <div className="mt-6 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {hot.map((f) => (
            <HeatCell key={f.key} f={f} level={heatLevel(f.commits, maxCommits)} />
          ))}
        </div>
      </section>

      <section className="rise mt-14" style={{ animationDelay: "120ms" }}>
        <SectionTitle index="02" title="Co-change" hint="files that change together · weight = co-occurrences" />
        <ul className="mt-6 flex flex-col divide-y divide-line overflow-hidden rounded-xl panel-glass">
          {co.map((c, i) => (
            <CoRow key={i} c={c} max={maxWeight} />
          ))}
        </ul>
      </section>

      <section className="rise mt-14 mb-10" style={{ animationDelay: "180ms" }}>
        <SectionTitle index="03" title="Explore the brain" hint="every view, live" />
        <div className="stagger mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {EXPLORE.map(([href, label, desc]) => (
            <a key={href} href={href} className="lift group flex flex-col gap-1 rounded-xl panel-glass px-4 py-4">
              <div className="flex items-center justify-between">
                <span className="display text-lg text-fg">{label}</span>
                <span className="font-mono text-accent transition-transform group-hover:translate-x-0.5">→</span>
              </div>
              <span className="font-mono text-[11px] text-muted">{desc}</span>
            </a>
          ))}
        </div>
      </section>

      <footer className="border-t border-line py-6 font-mono text-[11px] leading-5 text-muted">
        events → projector (cursor + logic_version) → metrics_daily · entity_edges · narratives → this view
      </footer>
    </div>
  );
}

function chip(active: boolean): string {
  return active
    ? "rounded-full border border-accent/50 bg-accent/10 px-2.5 py-0.5 text-accent"
    : "rounded-full border border-line px-2.5 py-0.5 text-muted hover:text-fg";
}

function Stat({ label, value, text }: { label: string; value?: number; text?: string }) {
  return (
    <div className="rounded-xl panel-glass px-4 py-4">
      <div className="font-mono text-3xl font-semibold tabular-nums text-fg">
        {text ?? value?.toLocaleString()}
      </div>
      <div className="mt-1 font-mono text-[11px] uppercase tracking-wider text-muted">{label}</div>
    </div>
  );
}

function SectionTitle({ index, title, hint }: { index: string; title: string; hint: string }) {
  return (
    <div className="flex items-end justify-between gap-4 border-b border-line pb-2.5">
      <h2 className="flex items-baseline gap-3">
        <span className="font-mono text-xs text-accent">{index}</span>
        <span className="display text-2xl text-fg">{title}</span>
      </h2>
      <span className="hidden font-mono text-[11px] text-muted sm:block">{hint}</span>
    </div>
  );
}

function HeatCell({ f, level }: { f: HotFile; level: number }) {
  return (
    <div
      className="group flex flex-col justify-between gap-3 overflow-hidden rounded-lg border border-line p-3 transition-transform duration-200 hover:-translate-y-0.5"
      style={{ backgroundColor: `var(--color-heat-${level})` }}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="truncate font-mono text-[13px] text-fg" title={f.key}>{f.name}</span>
        {f.active30d && (
          <span className="shrink-0 rounded-sm bg-black/30 px-1 py-0.5 font-mono text-[9px] text-accent">30d</span>
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
