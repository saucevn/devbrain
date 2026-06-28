import { getPyramid, type PyramidBlock } from "@/lib/data";

export const dynamic = "force-dynamic";

const RISK: Record<string, string> = {
  low: "border-accent/40 text-accent",
  in_progress: "border-amber/40 text-amber",
  critical: "border-[#ec4634]/50 text-[#ec4634]",
};

function activityLevel(v: number, max: number): number {
  if (max <= 0 || v <= 0) return 0;
  const r = v / max;
  if (r > 0.8) return 5;
  if (r > 0.55) return 4;
  if (r > 0.32) return 3;
  if (r > 0.15) return 2;
  return 1;
}
function maturityLevel(m: number | null): number {
  if (m == null) return 0;
  if (m >= 85) return 2;
  if (m >= 60) return 3;
  if (m >= 40) return 4;
  return 5; // low maturity → hot (needs work)
}

export default async function Pyramid({
  searchParams,
}: {
  searchParams: Promise<{ mode?: string }>;
}) {
  const { mode } = await searchParams;
  const m = mode === "maturity" ? "maturity" : "activity";
  const blocks = await getPyramid();
  const maxC = blocks.reduce((x, b) => Math.max(x, b.commits), 0);
  const layers = [...new Set(blocks.map((b) => b.layer))].sort((a, b) => b - a);

  return (
    <div className="mx-auto max-w-5xl px-5 py-10 sm:px-8">
      <header className="rise flex items-center justify-between gap-4">
        <a href="/" className="font-mono text-sm text-muted transition-colors hover:text-accent">← devbrain</a>
        <div className="flex items-center gap-3 font-mono text-xs">
          <span className="text-muted">heat:</span>
          <a href="/pyramid?mode=activity" className={m === "activity" ? "text-accent" : "text-muted hover:text-fg"}>activity</a>
          <a href="/pyramid?mode=maturity" className={m === "maturity" ? "text-accent" : "text-muted hover:text-fg"}>maturity</a>
        </div>
      </header>
      <h1 className="rise display mt-8 text-5xl"><span className="grad-text">Capability pyramid</span></h1>

      <p className="rise mt-6 text-sm leading-6 text-muted">
        Kim tự tháp năng lực — <span className="text-fg">block do người định nghĩa</span> (layer,
        maturity, risk), tô màu theo <span className="text-fg">{m === "maturity" ? "độ trưởng thành" : "hoạt động git 30d/all-time"}</span> (deterministic, không AI chấm điểm).
      </p>

      <div className="rise mt-8 flex flex-col gap-3">
        {layers.map((layer) => {
          const row = blocks.filter((b) => b.layer === layer);
          return (
            <div key={layer} className="flex items-stretch justify-center gap-3">
              <span className="flex w-8 shrink-0 items-center justify-center font-mono text-[11px] text-muted">L{layer}</span>
              {row.map((b) => {
                const level = m === "maturity" ? maturityLevel(b.maturity) : activityLevel(b.commits, maxC);
                return <Block key={b.key} b={b} level={level} mode={m} />;
              })}
            </div>
          );
        })}
      </div>

      {blocks.length === 0 && (
        <p className="mt-8 font-mono text-sm text-amber">Chưa có block. Seed pyramid_blocks + gán entities.</p>
      )}
    </div>
  );
}

function Block({ b, level, mode }: { b: PyramidBlock; level: number; mode: string }) {
  return (
    <div
      className="flex min-w-[150px] max-w-[230px] flex-1 flex-col gap-2 rounded-md border border-line p-3"
      style={{ backgroundColor: `var(--color-heat-${level})` }}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-mono text-[13px] font-semibold text-fg">{b.name}</span>
        {b.riskClass && (
          <span className={`shrink-0 rounded-sm border px-1 py-0.5 font-mono text-[9px] ${RISK[b.riskClass] ?? "border-line text-muted"}`}>
            {b.riskClass}
          </span>
        )}
      </div>
      <div className="flex items-end justify-between font-mono text-[11px] text-fg/85">
        <span>{b.entityCount} ent · {b.commits} commits</span>
        {b.maturity != null && <span className={mode === "maturity" ? "font-bold text-fg" : ""}>{b.maturity}%</span>}
      </div>
    </div>
  );
}
