import { getChangelog, type ChangelogEntry } from "@/lib/data";

export const dynamic = "force-dynamic";

export default async function Changelog() {
  const entries = await getChangelog();

  return (
    <div className="mx-auto max-w-3xl px-5 py-10 sm:px-8">
      <header className="rise flex items-center justify-between gap-4 border-b border-line pb-6">
        <a href="/" className="font-mono text-sm text-muted hover:text-fg">← devbrain</a>
        <span className="font-mono text-xs text-muted">changelog · AI narrative diary</span>
      </header>

      <p className="rise mt-6 text-sm leading-6 text-muted">
        Nhật ký sống của team: mỗi PR merge được tóm tắt (Haiku) thành một mục, mới nhất trước.
        Mỗi mục kèm link <span className="text-fg">nguồn</span> để verify.
      </p>

      <div className="rise mt-8 flex flex-col gap-4">
        {entries.map((e, i) => <Entry key={i} e={e} />)}
      </div>

      {entries.length === 0 && (
        <p className="mt-8 font-mono text-sm text-amber">
          Chưa có narrative. Backfill PR (`backfill_prs.py`) hoặc merge PR (webhook) → AI lane sinh mục changelog.
        </p>
      )}
    </div>
  );
}

function Entry({ e }: { e: ChangelogEntry }) {
  return (
    <article className="rounded-lg border border-line bg-panel px-4 py-3">
      <div className="flex items-center justify-between gap-3 font-mono text-[11px] text-muted">
        <div className="flex items-center gap-2">
          {e.date && <span>{e.date}</span>}
          {e.project && <span className="text-fg">{e.project}</span>}
          {e.pr && <span className="text-accent">{e.pr}</span>}
        </div>
        {e.sourceUrl && (
          <a href={e.sourceUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            source ↗
          </a>
        )}
      </div>

      {e.title && <h2 className="mt-1.5 font-mono text-sm font-semibold text-fg">{e.title}</h2>}
      {e.summary && <p className="mt-1 text-[13px] leading-6 text-muted">{e.summary}</p>}

      {e.highlights.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1">
          {e.highlights.map((h, i) => (
            <li key={i} className="flex gap-2 text-[12px] leading-5 text-muted">
              <span className="select-none text-accent">·</span>
              <span>{h}</span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
