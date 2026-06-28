import { getStaleDocs, type StaleDoc } from "@/lib/data";

export const dynamic = "force-dynamic";

// A doc is flagged "possibly stale" when its related code churned recently but
// the doc itself hasn't been touched in a while. The age axis lights up once
// real doc-update events (Lark/import) flow; until then this ranks docs by the
// churn pressure around them (knowledge-drift risk). NO AI scoring (golden #3).
const STALE_AGE_DAYS = 14;

export default async function Stale() {
  const docs = await getStaleDocs();

  return (
    <div className="mx-auto max-w-3xl px-5 py-10 sm:px-8">
      <header className="rise flex items-center justify-between gap-4">
        <a href="/" className="font-mono text-sm text-muted transition-colors hover:text-accent">← devbrain</a>
        <span className="font-mono text-xs text-muted">knowledge-drift · stale-doc watch</span>
      </header>
      <h1 className="rise display mt-8 text-5xl"><span className="grad-text">Stale-doc watch</span></h1>

      <p className="rise mt-6 text-sm leading-6 text-muted">
        Tín hiệu <span className="text-fg">deterministic</span> (không AI chấm điểm): doc mà{" "}
        <span className="text-fg">code liên quan</span> (co-change / documented_by) churn mạnh trong 30
        ngày → rủi ro <span className="text-fg">lỗi thời</span>. Cờ ⚠ bật khi doc cũ hơn {STALE_AGE_DAYS} ngày
        mà code vẫn churn (đầy đủ khi có doc-update events từ Lark/import).
      </p>

      <div className="rise mt-8 flex flex-col gap-2">
        <div className="grid grid-cols-[1fr_auto_auto_auto] gap-3 px-3 font-mono text-[11px] text-muted">
          <span>doc</span><span>churn 30d</span><span>neighbors</span><span>age</span>
        </div>
        {docs.map((d, i) => <Row key={i} d={d} />)}
      </div>

      {docs.length === 0 && (
        <p className="mt-8 font-mono text-sm text-amber">
          Chưa có tín hiệu. Cần metrics churn (git-backfill repo) + doc entities có link tới code.
        </p>
      )}
    </div>
  );
}

function Row({ d }: { d: StaleDoc }) {
  const stale = d.docAgeDays > STALE_AGE_DAYS;
  return (
    <div className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-3 rounded-md border border-line bg-panel px-3 py-2 font-mono text-[12px]">
      <span className="min-w-0 truncate text-fg" title={d.key}>
        {stale && <span className="mr-1 text-amber" title="possibly stale">⚠</span>}
        {d.doc}
      </span>
      <span className="text-right text-accent">{d.neighborChurn30d.toLocaleString()}</span>
      <span className="text-right text-muted">{d.neighbors}</span>
      <span className={`text-right ${stale ? "text-amber" : "text-muted"}`}>{d.docAgeDays}d</span>
    </div>
  );
}
