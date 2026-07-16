/**
 * Standalone search demo — synthetic data generator.
 *
 * Generates a realistic lead pool so the search capability can be demoed before the real
 * `leadplus_dev` copy is available. Deterministic (fixed-seed PRNG) so every run produces
 * the same pool and demos are repeatable.
 *
 * Data is shaped so the ANY-vs-ALL distinction is actually visible:
 *   - AWS is everywhere (~55%), so an ANY search floods.
 *   - Only a small, known set is Manufacturing + SAP + Snowflake + AWS, so an ALL search
 *     for "SAP, Snowflake, AWS" returns a precise, demo-able answer.
 * It also spreads technologies across the Apollo column (`technologies`) and the scraper
 * columns (`scraped_*`), mirroring how the real pool is populated by two different sources.
 *
 * Usage: node seed.js
 */
import pg from 'pg';
import { config } from './config.js';

// ---- deterministic PRNG (LCG) so the pool is identical on every seed ------------------
let _s = 1337;
const rnd = () => ((_s = (_s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
const pick = (arr) => arr[Math.floor(rnd() * arr.length)];
const chance = (p) => rnd() < p;
const pickSome = (arr, n) => {
  const pool = [...arr];
  const out = [];
  for (let i = 0; i < n && pool.length; i++) out.push(...pool.splice(Math.floor(rnd() * pool.length), 1));
  return out;
};

const INDUSTRIES = [
  ...Array(10).fill('Manufacturing'),      // weighted — this is the ICP
  'Industrial Automation', 'Automotive', 'Aerospace & Defense', 'Pharmaceuticals',
  'Chemicals', 'Logistics & Supply Chain', 'Energy & Utilities', 'Consumer Goods',
  'Electronics', 'Food & Beverage', 'Retail', 'Healthcare',
];

// Apollo-style technographics
const ERP        = ['SAP', 'SAP S/4HANA', 'Oracle ERP', 'NetSuite', 'Microsoft Dynamics 365', 'Infor'];
const CLOUD      = ['AWS', 'Microsoft Azure', 'Google Cloud Platform'];
const DATA       = ['Snowflake', 'Databricks', 'Redshift', 'BigQuery', 'Teradata'];
const CRM        = ['Salesforce', 'HubSpot', 'Zoho CRM', 'Microsoft Dynamics CRM'];
// scraper-style technographics
const SCRAPED_TECH     = ['Kubernetes', 'Docker', 'Kafka', 'Terraform', 'Airflow', 'Spark', 'dbt'];
const SCRAPED_TOOLS    = ['Jira', 'Confluence', 'ServiceNow', 'Tableau', 'Power BI', 'Looker', 'Workday'];
const SCRAPED_SERVICES = ['Managed Hosting', 'Systems Integration', 'ERP Implementation',
                          'Data Migration', 'Cloud Modernization', 'MES Integration'];
const KEYWORDS = ['digital transformation', 'smart factory', 'industry 4.0', 'predictive maintenance',
                  'supply chain visibility', 'IIoT', 'lean manufacturing', 'plant automation',
                  'quality management', 'warehouse automation'];

const PREFIX = ['Vertex', 'Ironclad', 'Northwind', 'Apex', 'Blue Ridge', 'Summit', 'Cascade', 'Meridian',
  'Granite', 'Copperline', 'Halcyon', 'Redstone', 'Silverpeak', 'Atlas', 'Beacon', 'Crestwood',
  'Dynamo', 'Everforge', 'Falcon', 'Gearworks', 'Harborline', 'Ridgeline', 'Kestrel', 'Lodestar',
  'Monarch', 'Nimbus', 'Orbital', 'Pinnacle', 'Quarry', 'Sterling', 'Titan', 'Union'];
const SUFFIX = ['Industries', 'Manufacturing', 'Works', 'Dynamics', 'Systems', 'Fabrication',
  'Components', 'Technologies', 'Group', 'Precision', 'Machining', 'Materials'];
const STATES = [['California','San Jose'],['Texas','Houston'],['Michigan','Detroit'],['Ohio','Cleveland'],
  ['Illinois','Chicago'],['Pennsylvania','Pittsburgh'],['Georgia','Atlanta'],['Indiana','Indianapolis'],
  ['Wisconsin','Milwaukee'],['North Carolina','Charlotte'],['Tennessee','Nashville'],['Arizona','Phoenix']];
const RANGES = [['1-10','8'],['11-50','32'],['51-200','140'],['201-500','380'],
                ['501-1000','760'],['1001-5000','2400'],['5001-10000','7200'],['10001+','18000']];

const TOTAL = 300;
const GOLDEN = 12; // Manufacturing + SAP + Snowflake + AWS — the precise ALL-search answer

function makeCompany(i, golden) {
  const name = `${pick(PREFIX)} ${pick(SUFFIX)}`;
  const domain = name.toLowerCase().replace(/[^a-z0-9]+/g, '') + '.com';
  const [state, city] = pick(STATES);
  const [range, count] = pick(RANGES);

  let technologies = [];
  if (golden) {
    technologies = ['SAP', 'Snowflake', 'AWS', ...pickSome(CRM, 1)];
  } else {
    if (chance(0.35)) technologies.push(pick(ERP));
    if (chance(0.55)) technologies.push('AWS');           // AWS is everywhere -> ANY floods
    if (chance(0.35)) technologies.push(pick(CLOUD));
    if (chance(0.28)) technologies.push(pick(DATA));
    if (chance(0.40)) technologies.push(pick(CRM));
  }

  return {
    name,
    domain,
    website_url: `https://www.${domain}`,
    industry: golden ? 'Manufacturing' : pick(INDUSTRIES),
    hq_city: city,
    hq_state: state,
    hq_country: 'United States',
    employee_range: range,
    employee_count: count,
    revenue_usd: Math.round((rnd() * 900 + 5) * 100) / 100 * 1_000_000,
    keywords: pickSome(KEYWORDS, Math.floor(rnd() * 4)),
    technologies: [...new Set(technologies)],
    scraped_technologies: chance(0.55) ? pickSome(SCRAPED_TECH, 1 + Math.floor(rnd() * 3)) : [],
    scraped_tools: chance(0.45) ? pickSome(SCRAPED_TOOLS, 1 + Math.floor(rnd() * 2)) : [],
    scraped_services: chance(0.35) ? pickSome(SCRAPED_SERVICES, 1 + Math.floor(rnd() * 2)) : [],
    segments: chance(0.5) ? ['Enterprise'] : ['Mid-Market'],
    active: true,
    // NOT NULL in the real table with no default, so the seed must supply them
    is_target_account: chance(0.2),
    score: Math.floor(rnd() * 100),
  };
}

const rows = [];
for (let i = 0; i < TOTAL; i++) rows.push(makeCompany(i, i < GOLDEN));

// A deliberate near-miss so substring matching is demonstrable: "SAP" also matches "Sapient".
rows.push({
  ...makeCompany(999, false),
  name: 'Sapient Consulting Group',
  domain: 'sapientconsulting.com',
  website_url: 'https://www.sapientconsulting.com',
  industry: 'Manufacturing',
  technologies: ['Sapient Cloud Suite', 'AWS', 'Snowflake'], // contains "Sap" but is NOT SAP
});

const { Client } = pg;
const client = new Client({ connectionString: config.databaseUrl });

// The table has all 45 real columns; the seed populates the ones a freshly-sourced pool
// would actually have. The rest (salesperson_*, zoho_account_id, icp_tag, ...) stay NULL,
// which is also what they look like in the real pool before enrichment/CRM sync.
const COLS = ['name','domain','website_url','industry','hq_city','hq_state','hq_country',
  'employee_range','employee_count','revenue_usd','keywords','technologies',
  'scraped_technologies','scraped_tools','scraped_services','segments','active',
  'is_target_account','score'];

const run = async () => {
  await client.connect();
  await client.query('TRUNCATE lead_company RESTART IDENTITY');
  for (const r of rows) {
    await client.query(
      `INSERT INTO lead_company (${COLS.join(',')}) VALUES (${COLS.map((_, i) => `$${i + 1}`).join(',')})`,
      COLS.map((c) => r[c]),
    );
  }
  const { rows: [stat] } = await client.query(`
    SELECT count(*) AS total,
           count(*) FILTER (WHERE industry = 'Manufacturing') AS manufacturing,
           count(*) FILTER (WHERE lower(array_to_string(technologies, ',')) LIKE '%sap%') AS sap,
           count(*) FILTER (WHERE lower(array_to_string(technologies, ',')) LIKE '%aws%') AS aws,
           count(*) FILTER (WHERE lower(array_to_string(technologies, ',')) LIKE '%snowflake%') AS snowflake
      FROM lead_company`);
  console.log('seeded lead_company:', stat);
  await client.end();
};

run().catch((e) => { console.error(e); process.exit(1); });
