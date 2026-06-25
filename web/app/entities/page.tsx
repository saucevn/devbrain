import { getProposedEntities, type ProposedEntity } from "@/lib/data";
import { confirmEntity } from "@/lib/actions";

export const dynamic = "force-dynamic";

export default async function Entities() {
  const ents = await getProposedEntities(50);
  return (
    <div className="mx-auto max-w-4xl px-5 py-10 sm:px-8">
      <header className="rise flex items-center justify-between gap-4 border-b border-line pb-6">
        <a href="/" className="font-mono text-sm text-muted hover:text-fg">← devbrain</a>
        <span className="font-mono text-xs text-muted">entity confirm · human-owned</span>
      </header>

      <p className="rise mt-6 text-sm leading-6 text-muted">
        Entity do AI đề xuất (<span className="text-fg">proposed</span>), neo vào path/issue thật qua{" "}
        <span className="font-mono text-accent">guard_canonical_key</span>. Confirm/rename → ghi field
        người sở hữu; replay projector <span className="text-fg">không</span> ghi đè (golden rule #5).
      </p>

      <ul className="rise mt-6 flex flex-col gap-3">
        {ents.map((e) => <Row key={e.id} e={e} />)}
      </ul>

      {ents.length === 0 && (
        <p className="mt-8 font-mono text-sm text-accent">
          Hết entity proposed — tất cả đã confirmed/rejected. ✓
        </p>
      )}
    </div>
  );
}

function Row({ e }: { e: ProposedEntity }) {
  return (
    <li className="rounded-lg border border-line bg-panel p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <span className="rounded-sm bg-black/30 px-1.5 py-0.5 font-mono text-[10px] uppercase text-accent">
          {e.kind}
        </span>
        <span className="font-mono text-[11px] text-muted">
          {e.touches} touch{e.touches === 1 ? "" : "es"} · {e.lastSeen ?? ""}
        </span>
      </div>
      <div className="mb-3 truncate font-mono text-[13px] text-fg" title={e.canonicalKey}>
        {e.canonicalKey}
      </div>
      <form action={confirmEntity} className="flex flex-wrap items-center gap-2">
        <input type="hidden" name="id" value={e.id} />
        <input
          name="display_name"
          defaultValue={e.displayName}
          className="min-w-0 flex-1 rounded-md border border-line bg-bg px-3 py-1.5 font-mono text-[13px] text-fg outline-none focus:border-accent"
        />
        <button
          name="action"
          value="confirm"
          className="rounded-md border border-accent bg-accent/10 px-3 py-1.5 font-mono text-xs text-accent hover:bg-accent/20"
        >
          confirm
        </button>
        <button
          name="action"
          value="reject"
          className="rounded-md border border-line px-3 py-1.5 font-mono text-xs text-muted hover:text-fg"
        >
          reject
        </button>
      </form>
    </li>
  );
}
