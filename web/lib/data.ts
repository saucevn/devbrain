import { Pool } from "pg";

// Reuse one pool across HMR reloads in dev.
const g = globalThis as unknown as { _pgPool?: Pool };
const pool =
  g._pgPool ??
  new Pool({ connectionString: process.env.DATABASE_URL, max: 5 });
if (process.env.NODE_ENV !== "production") g._pgPool = pool;

export type Summary = {
  events: number;
  entities: number;
  edges: number;
  lastEvent: string | null;
};
export type HotFile = {
  key: string;
  name: string;
  commits: number;
  churn: number;
  lastDay: string | null;
  active30d: boolean;
};
export type CoChange = { a: string; b: string; weight: number };

async function safe<T>(fn: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    console.error("[data] query failed:", e);
    return fallback;
  }
}

export function getSummary(): Promise<Summary> {
  return safe(
    async () => {
      const { rows } = await pool.query(
        `select
           (select count(*) from events)::int        as events,
           (select count(*) from entities)::int      as entities,
           (select count(*) from entity_edges)::int  as edges,
           (select max(occurred_at) from events)     as last_event`
      );
      const r = rows[0];
      return {
        events: r.events,
        entities: r.entities,
        edges: r.edges,
        lastEvent: r.last_event ? new Date(r.last_event).toISOString() : null,
      };
    },
    { events: 0, entities: 0, edges: 0, lastEvent: null }
  );
}

export function getHotFiles(limit = 28): Promise<HotFile[]> {
  return safe(async () => {
    const { rows } = await pool.query(
      `select e.canonical_key                       as key,
              e.display_name                        as name,
              sum(m.commit_count)::int              as commits,
              sum(m.lines_added + m.lines_removed)::int as churn,
              max(m.day)                            as last_day,
              bool_or(m.day >= current_date - 30)   as active30d
       from entities e
       join metrics_daily m on m.entity_id = e.id
       where e.entity_kind = 'file'
       group by e.id
       order by commits desc, churn desc
       limit $1`,
      [limit]
    );
    return rows.map((r) => ({
      key: r.key,
      name: r.name,
      commits: r.commits,
      churn: r.churn,
      lastDay: r.last_day ? new Date(r.last_day).toISOString().slice(0, 10) : null,
      active30d: r.active30d,
    }));
  }, []);
}

export function getCoChanges(limit = 16): Promise<CoChange[]> {
  return safe(async () => {
    const { rows } = await pool.query(
      `select ef.display_name as a, et.display_name as b, ed.weight::int as weight
       from entity_edges ed
       join entities ef on ef.id = ed.from_entity
       join entities et on et.id = ed.to_entity
       where ed.edge_type = 'co_changed_with'
       order by ed.weight desc, a, b
       limit $1`,
      [limit]
    );
    return rows.map((r) => ({ a: r.a, b: r.b, weight: r.weight }));
  }, []);
}


// ---- Phase 2B: semantic search -------------------------------------------
async function embedQuery(text: string): Promise<number[]> {
  const key = process.env.GEMINI_API_KEY;
  const res = await fetch(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key=" + key,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "models/gemini-embedding-001",
        content: { parts: [{ text }] },
        taskType: "RETRIEVAL_QUERY",
        outputDimensionality: 1536,
      }),
    }
  );
  if (!res.ok) throw new Error("gemini embed " + res.status);
  const j = await res.json();
  const v: number[] = j.embedding.values;
  const n = Math.sqrt(v.reduce((s, x) => s + x * x, 0));
  return n > 0 ? v.map((x) => x / n) : v;
}

export type SearchHit = {
  content: string;
  similarity: number;
  title: string | null;
  scope: string | null;
  scopeRef: string | null;
  sourceUrl: string | null;
  occurredAt: string | null;
};

export async function searchBrain(q: string, limit = 12): Promise<SearchHit[]> {
  return safe(async () => {
    const vec = await embedQuery(q);
    const lit = "[" + vec.join(",") + "]";
    const { rows } = await pool.query(
      `select e.content,
              1 - (e.embedding <=> $1::vector) as similarity,
              e.occurred_at,
              n.title, n.scope, n.scope_ref,
              (select source_url from events where id = any(n.source_event_ids) limit 1) as source_url
       from embeddings e
       left join narratives n on n.id = e.source_id and e.source_kind = 'pr_summary'
       order by e.embedding <=> $1::vector
       limit $2`,
      [lit, limit]
    );
    return rows.map((r) => ({
      content: r.content,
      similarity: Number(r.similarity),
      title: r.title,
      scope: r.scope,
      scopeRef: r.scope_ref,
      sourceUrl: r.source_url,
      occurredAt: r.occurred_at ? new Date(r.occurred_at).toISOString().slice(0, 10) : null,
    }));
  }, []);
}


// ---- Phase 2C: entity confirm ---------------------------------------------
export type ProposedEntity = {
  id: string;
  kind: string;
  canonicalKey: string;
  displayName: string;
  touches: number;
  lastSeen: string | null;
};

export function getProposedEntities(limit = 50): Promise<ProposedEntity[]> {
  return safe(async () => {
    const { rows } = await pool.query(
      `select e.id, e.entity_kind, e.canonical_key, e.display_name, e.last_seen_at,
              count(c.id)::int as touches
       from entities e
       left join contributions c on c.entity_id = e.id
       where e.resolution_status = 'proposed'
       group by e.id
       order by e.last_seen_at desc
       limit $1`,
      [limit]
    );
    return rows.map((r) => ({
      id: r.id,
      kind: r.entity_kind,
      canonicalKey: r.canonical_key,
      displayName: r.display_name,
      touches: r.touches,
      lastSeen: r.last_seen_at ? new Date(r.last_seen_at).toISOString().slice(0, 10) : null,
    }));
  }, []);
}

// Human-owned write: confirm/reject + optional rename. canonical_key is the
// AI's anchor and is NOT editable here (golden rule #5).
export async function confirmEntityDb(
  id: string,
  action: "confirm" | "reject",
  displayName?: string
): Promise<void> {
  const status = action === "reject" ? "rejected" : "confirmed";
  const name = displayName?.trim();
  if (name) {
    await pool.query(
      "update entities set resolution_status = $2, display_name = $3 where id = $1",
      [id, status, name]
    );
  } else {
    await pool.query("update entities set resolution_status = $2 where id = $1", [id, status]);
  }
}


// ---- Phase 3: pyramid (curated blocks + deterministic heat) ---------------
export type PyramidBlock = {
  key: string;
  name: string;
  layer: number;
  position: number;
  maturity: number | null;
  riskClass: string | null;
  description: string | null;
  entityCount: number;
  commits: number;
  churn: number;
};

export function getPyramid(): Promise<PyramidBlock[]> {
  return safe(async () => {
    const { rows } = await pool.query(
      `select b.key, b.name, b.layer, b.position, b.maturity, b.risk_class, b.description,
              count(distinct e.id)::int as entity_count,
              coalesce(sum(m.commit_count), 0)::int as commits,
              coalesce(sum(m.lines_added + m.lines_removed), 0)::int as churn
       from pyramid_blocks b
       left join entities e on e.pyramid_block = b.key and e.merged_into is null
       left join metrics_daily m on m.entity_id = e.id
       group by b.id
       order by b.layer desc, b.position`
    );
    return rows.map((r) => ({
      key: r.key, name: r.name, layer: r.layer, position: r.position,
      maturity: r.maturity, riskClass: r.risk_class, description: r.description,
      entityCount: r.entity_count, commits: r.commits, churn: r.churn,
    }));
  }, []);
}
