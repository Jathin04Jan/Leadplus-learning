/**
 * Standalone search demo — rebuilds the demo database from scratch.
 *
 * Drops and recreates the `public` schema, then applies `schema.sql`, which is a VERBATIM
 * copy of the production schema at
 *   Leadplus-corelabs/leadplus-service/src/main/resources/schema.sql
 * (70 tables, 940 columns). To refresh it, copy that file over this one — don't hand-edit,
 * so the demo DB stays a faithful replica of production.
 *
 * Usage: node setup.js   (or `npm run reset` for setup + seed)
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import pg from 'pg';
import { config } from './config.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const sql = fs.readFileSync(path.join(here, 'schema.sql'), 'utf8');

// This script is destructive. Refuse to run against anything but a *demo* database, so a
// stray DATABASE_URL can never drop the real `leadplus` schema.
const dbName = (config.databaseUrl.split('/').pop() || '').split('?')[0];
if (!/demo/i.test(dbName)) {
  console.error(
    `refusing to drop schema on database "${dbName}" — setup.js only runs against a *demo* database.\n` +
      'Point DATABASE_URL at leadplus_demo (or another db whose name contains "demo").',
  );
  process.exit(1);
}

const client = new pg.Client({ connectionString: config.databaseUrl });
const run = async () => {
  await client.connect();
  await client.query('DROP SCHEMA IF EXISTS public CASCADE');
  await client.query('CREATE SCHEMA public');
  await client.query(sql);
  const { rows: [{ tables }] } = await client.query(
    `SELECT count(*)::int AS tables FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind = 'r'`,
  );
  console.log(`schema applied to ${config.databaseUrl.replace(/:[^:@/]+@/, ':***@')} — ${tables} tables`);
  await client.end();
};
run().catch((e) => {
  console.error(e);
  process.exit(1);
});
