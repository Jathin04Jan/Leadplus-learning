/**
 * Standalone search demo — applies schema.sql to the configured database.
 * Usage: node setup.js
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import pg from 'pg';
import { config } from './config.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const sql = fs.readFileSync(path.join(here, 'schema.sql'), 'utf8');

const client = new pg.Client({ connectionString: config.databaseUrl });
const run = async () => {
  await client.connect();
  await client.query(sql);
  console.log('schema applied to', config.databaseUrl.replace(/:[^:@/]+@/, ':***@'));
  await client.end();
};
run().catch((e) => { console.error(e); process.exit(1); });
