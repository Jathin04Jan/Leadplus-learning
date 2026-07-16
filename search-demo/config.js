// Standalone search demo — configuration.
// Everything comes from the environment so that swapping the synthetic pool for a real
// `leadplus_dev` copy is a config change, not a code change.
// Copy .env.example -> .env and adjust. .env is gitignored; never commit credentials.

import 'dotenv/config';

export const config = {
  databaseUrl:
    process.env.DATABASE_URL ||
    'postgresql://leadplus:leadplus@localhost:5432/leadplus_demo',
  port: Number(process.env.PORT || 4000),
};
