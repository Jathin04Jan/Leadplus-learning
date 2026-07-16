/**
 * Insert helpers. The schema has no FK constraints, so the seed is responsible for
 * referential integrity: every insert returns the generated ids, and callers thread those
 * ids into dependent tables.
 */

/** Chunk size for multi-row INSERTs — keeps us well under Postgres' 65535 param cap. */
const CHUNK = 500;

/**
 * Insert `rows` (array of plain objects) into `table` and return the generated ids in
 * order. Column list is taken from the first row, so every row must have the same keys.
 */
export const insertMany = async (client, table, rows) => {
  if (!rows.length) throw new Error(`insertMany(${table}): refusing to insert 0 rows — every table must be populated`);
  const cols = Object.keys(rows[0]);
  const ids = [];

  for (let start = 0; start < rows.length; start += CHUNK) {
    const chunk = rows.slice(start, start + CHUNK);
    const params = [];
    const tuples = chunk.map((row) => {
      const ph = cols.map((c) => {
        params.push(row[c] === undefined ? null : row[c]);
        return `$${params.length}`;
      });
      return `(${ph.join(',')})`;
    });
    const sql = `INSERT INTO ${table} (${cols.map((c) => `"${c}"`).join(',')})
                 VALUES ${tuples.join(',')} RETURNING id`;
    const res = await client.query(sql, params);
    ids.push(...res.rows.map((r) => Number(r.id)));
  }
  return ids;
};

/** Wipe every table in `public` and restart identity sequences, so seeding is idempotent. */
export const truncateAll = async (client) => {
  const { rows } = await client.query(
    `SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1`,
  );
  const tables = rows.map((r) => `public."${r.relname}"`).join(', ');
  await client.query(`TRUNCATE ${tables} RESTART IDENTITY CASCADE`);
  return rows.length;
};
