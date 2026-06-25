import { getRoadmap, type RoadmapItem } from "@/lib/data";

export const dynamic = "force-dynamic";

const STATUS: Record<string, string> = {
  planned: "border-line text-muted",
  in_progress: "border-amber/40 text-amber",
  shipped: "border-accent/40 text-accent",
};

export default async function Roadmap() {
  const items = await getRoadmap();
  const years = [...new Set(items.map((i) => i.year))].sort((a, b) => b - a);

  return (
    <div className="mx-auto max-w-3xl px-5 py-10 sm:px-8">
      <header className="rise flex items-center justify-between gap-4 border-b border-line pb-6">
        <a href="/" className="font-mono text-sm text-muted hover:text-fg">← devbrain</a>
        <span className="font-mono text-xs text-muted">roadmap · GitHub milestones</span>
      </header>

      <p className="rise mt-6 text-sm leading-6 text-muted">
        Dòng thời gian từ <span className="text-fg">entity_status_history</span> (milestone → status
        transition), nhóm theo năm. Mỗi mốc kèm link <span className="text-fg">nguồn</span> để verify.
      </p>

      <div className="rise mt-8 flex flex-col gap-8">
        {years.map((year) => (
          <section key={year}>
            <h2 className="mb-3 font-mono text-lg font-bold text-fg">{year}</h2>
            <ul className="flex flex-col gap-2 border-l border-line pl-5">
              {items.filter((i) => i.year === year).map((it, idx) => <Item key={idx} it={it} />)}
            </ul>
          </section>
        ))}
      </div>

      {items.length === 0 && (
        <p className="mt-8 font-mono text-sm text-amber">
          Chưa có mốc roadmap. Tạo/đổi GitHub milestone (webhook event `milestone`) → status projector ghi vào đây.
        </p>
      )}
    </div>
  );
}

function Item({ it }: { it: RoadmapItem }) {
  return (
    <li className="relative flex items-center justify-between gap-3 rounded-md border border-line bg-panel px-3 py-2">
      <span className="absolute -left-[23px] h-2 w-2 rounded-full bg-accent" aria-hidden />
      <span className="min-w-0 truncate font-mono text-[13px] text-fg">{it.entity}</span>
      <div className="flex shrink-0 items-center gap-2 font-mono text-[11px]">
        <span className={`rounded-sm border px-1.5 py-0.5 ${STATUS[it.status] ?? "border-line text-muted"}`}>{it.status}</span>
        <span className="text-muted">{it.changedAt}</span>
        {it.evidenceUrl && <a href={it.evidenceUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">↗</a>}
      </div>
    </li>
  );
}
