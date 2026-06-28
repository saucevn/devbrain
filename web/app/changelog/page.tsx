import { getChangelog, type ChangelogEntry } from "@/lib/data";

export const dynamic = "force-dynamic";

export default async function Changelog() {
  const entries = await getChangelog();

  return (
    <div className="mx-auto max-w-3xl px-5 py-12 sm:px-8">
      <header className="rise flex items-center justify-between gap-4">
        <a href="/" className="font-mono text-sm text-muted transition-colors hover:text-accent">← devbrain</a>
        <span className="font-mono text-xs text-muted">AI narrative diary</span>
      </header>

      <h1 className="rise display mt-8 text-5xl">
        <span className="grad-text">Changelog</span>
      </h1>
      <p className="rise prose-serif mt-4 max-w-2xl text-lg text-muted">
        Nhật ký sống của team — mỗi PR merge được tóm tắt thành một mục, mới nhất trước.
        Mỗi mục <span className="italic text-fg">kèm nguồn</span> để verify.
      </p>
      <div className="rule-brand mt-8" />

      <div className="stagger mt-8 flex flex-col gap-4">
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
    <article className="lift rounded-xl panel-glass px-5 py-4">
      <div className="flex items-center justify-between gap-3 font-mono text-[11px]">
        <div className="flex items-center gap-2.5">
          {e.date && <span className="italic text-muted" style={{ fontFamily: "var(--font-display)" }}>{e.date}</span>}
          {e.project && <span className="text-muted">{e.project}</span>}
          {e.pr && <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-accent">{e.pr}</span>}
        </div>
        {e.sourceUrl && (
          <a href={e.sourceUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">source ↗</a>
        )}
      </div>

      {e.title && <h2 className="display mt-2 text-xl text-fg">{e.title}</h2>}
      {e.summary && <p className="prose-serif mt-1.5 text-[15px] text-muted">{e.summary}</p>}

      {e.highlights.length > 0 && (
        <ul className="mt-3 flex flex-col gap-1.5 border-l border-line pl-3">
          {e.highlights.map((h, i) => (
            <li key={i} className="flex gap-2 text-[13px] leading-6 text-muted">
              <span className="select-none text-accent">—</span>
              <span>{h}</span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
