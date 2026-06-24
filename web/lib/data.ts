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
