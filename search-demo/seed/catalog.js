/**
 * Domain vocabulary for the synthetic pool: US manufacturing / industrial GTM.
 *
 * Kept in one place so the same technology names flow through every layer that mentions
 * them — Apollo technographics, scraper roll-ups, job postings, campaign copy, search
 * history — the way they would in a real pool sourced from those systems.
 */

// ---- technographics ------------------------------------------------------------------
// Apollo-style (lead_company.technologies)
export const ERP = ['SAP', 'SAP S/4HANA', 'Oracle ERP', 'NetSuite', 'Microsoft Dynamics 365', 'Infor CloudSuite', 'Epicor Kinetic'];
export const CLOUD = ['AWS', 'Microsoft Azure', 'Google Cloud Platform'];
export const DATA = ['Snowflake', 'Databricks', 'Redshift', 'BigQuery', 'Teradata'];
export const CRM_TECH = ['Salesforce', 'HubSpot', 'Zoho CRM', 'Microsoft Dynamics CRM'];

// Scraper-style (lead_company.scraped_technologies) — parsed out of job postings
export const SCRAPED_TECH = ['Kubernetes', 'Docker', 'Kafka', 'Terraform', 'Airflow', 'Spark', 'dbt', 'PostgreSQL', 'Python', 'Java'];
export const SCRAPED_TOOLS = ['Jira', 'Confluence', 'ServiceNow', 'Tableau', 'Power BI', 'Looker', 'Workday', 'GitHub Actions'];
export const SCRAPED_SERVICES = ['Managed Hosting', 'Systems Integration', 'ERP Implementation', 'Data Migration', 'Cloud Modernization', 'MES Integration', 'Predictive Maintenance'];

export const KEYWORDS = [
  'digital transformation', 'smart factory', 'industry 4.0', 'predictive maintenance',
  'supply chain visibility', 'IIoT', 'lean manufacturing', 'plant automation',
  'quality management', 'warehouse automation', 'contract manufacturing', 'CNC machining',
];

/** Every technology string the pool can mention, for search-history / filter fixtures. */
export const ALL_TECH = [...ERP, ...CLOUD, ...DATA, ...CRM_TECH, ...SCRAPED_TECH];

// ---- companies -----------------------------------------------------------------------
export const PREFIX = [
  'Vertex', 'Ironclad', 'Northwind', 'Apex', 'Blue Ridge', 'Summit', 'Cascade', 'Meridian',
  'Granite', 'Copperline', 'Halcyon', 'Redstone', 'Silverpeak', 'Atlas', 'Beacon', 'Crestwood',
  'Dynamo', 'Everforge', 'Falcon', 'Gearworks', 'Harborline', 'Ridgeline', 'Kestrel', 'Lodestar',
  'Monarch', 'Nimbus', 'Orbital', 'Pinnacle', 'Quarry', 'Sterling', 'Titan', 'Union',
];
export const SUFFIX = [
  'Industries', 'Manufacturing', 'Works', 'Dynamics', 'Systems', 'Fabrication',
  'Components', 'Technologies', 'Group', 'Precision', 'Machining', 'Materials',
];

export const INDUSTRIES = [
  ...Array(10).fill('Manufacturing'), // weighted — this is the ICP
  'Industrial Automation', 'Automotive', 'Aerospace & Defense', 'Pharmaceuticals',
  'Chemicals', 'Logistics & Supply Chain', 'Energy & Utilities', 'Consumer Goods',
  'Electronics', 'Food & Beverage', 'Retail', 'Healthcare',
];

/** Distinct industry names (no ICP weighting) — for the `industry` catalog table. */
export const INDUSTRY_NAMES = [...new Set(INDUSTRIES)];

export const SEGMENTS = ['Enterprise', 'Mid-Market', 'SMB'];

/**
 * lead_company.employee_range is @Enumerated(EnumType.STRING) on EmployeeRange, so the
 * column stores the CONSTANT NAME (RANGE_0_500), not the "0-500" @JsonValue label — the
 * label only appears on the JSON API surface. Storing labels here would break the app on
 * read. `employee_count` is a free string, kept inside the bucket.
 * See EmployeeRange.java + LeadCompanyBase.java.
 */
export const EMPLOYEE_RANGES = [
  ['RANGE_0_500', '120'], ['RANGE_0_500', '380'], ['RANGE_501_1000', '760'],
  ['RANGE_1001_5000', '2400'], ['RANGE_5001_10000', '7200'], ['RANGE_10001_PLUS', '18000'],
];

export const NAICS = ['332710', '333517', '336411', '325211', '334513', '336390', '332999', '333248'];
export const SIC = ['3599', '3541', '3728', '2821', '3823', '3714', '3469', '3556'];

// ---- geography -----------------------------------------------------------------------

/**
 * Transcribed VERBATIM from RegionMapping.SALES_REGIONS
 * (application/common/utils/RegionMapping.java). These are the app's only region values —
 * note they are NOT US Census divisions (Colorado is Midwest here, Georgia is EastCoast),
 * so they must be copied, not guessed. `lead_company.region` is always derived through
 * this map by LeadCompanyService, overwriting whatever the caller sent.
 */
export const SALES_REGIONS = {
  WestCoast: ['California', 'Oregon', 'Washington', 'Alaska', 'Hawaii', 'Nevada', 'Utah', 'Arizona', 'Los Angeles'],
  EastCoast: ['New York', 'New Jersey', 'Massachusetts', 'Pennsylvania', 'Connecticut', 'Maryland', 'Virginia',
    'North Carolina', 'South Carolina', 'Georgia', 'Florida', 'Delaware', 'District of Columbia', 'New Hampshire'],
  Midwest: ['Illinois', 'Ohio', 'Michigan', 'Minnesota', 'Wisconsin', 'Missouri', 'Indiana', 'Iowa', 'Kansas',
    'Nebraska', 'North Dakota', 'South Dakota', 'Colorado'],
  South: ['Texas', 'Oklahoma', 'Arkansas', 'Louisiana', 'Tennessee', 'Kentucky', 'Alabama', 'Mississippi', 'New Braunfels'],
  Canada: ['Ontario', 'Quebec', 'British Columbia', 'Alberta', 'Manitoba', 'Saskatchewan', 'Nova Scotia',
    'New Brunswick', 'Newfoundland And Labrador', 'Prince Edward Island', 'Northwest Territories', 'Yukon', 'Nunavut', 'Canada'],
  Mexico: ['Aguascalientes', 'Baja California', 'Baja California Sur', 'Campeche', 'Chiapas', 'Chihuahua', 'Coahuila',
    'Colima', 'Durango', 'Guanajuato', 'Guerrero', 'Hidalgo', 'Jalisco', 'Mexico State', 'Michoacán', 'Morelos',
    'Nayarit', 'Nuevo León', 'Oaxaca', 'Puebla', 'Querétaro', 'Quintana Roo', 'San Luis Potosí', 'Sinaloa', 'Sonora',
    'Tabasco', 'Tamaulipas', 'Tlaxcala', 'Veracruz', 'Yucatán', 'Zacatecas'],
  APAC: ['Sri Lanka', 'Singapore', 'Japan', 'Malaysia', 'Australia', 'Hong Kong', 'Taiwan', 'South Korea', 'New Zealand'],
  Europe: ['Portugal', 'Germany', 'Spain', 'France', 'Denmark', 'Netherlands', 'Sweden', 'Belgium', 'Switzerland',
    'United Kingdom', 'Ireland', 'Italy', 'Austria', 'Croatia', 'Norway', 'Israel'],
  MiddleEast: ['Saudi Arabia', 'United Arab Emirates', 'Israel'],
  LATAM: ['Brazil'],
  US: ['United States'],
};

export const REGIONS = Object.keys(SALES_REGIONS);

/**
 * Mirrors LeadCompanyService.getRegionByCityOrStateOrCountry: state first, then city, then
 * country; null when nothing matches. Seeding through the same resolution the app uses is
 * what keeps `region` consistent with `hq_state` on every row.
 */
export const regionFor = (state, city, country) => {
  for (const key of [state, city, country]) {
    if (!key) continue;
    const hit = Object.entries(SALES_REGIONS).find(([, members]) =>
      members.some((m) => m.toLowerCase() === String(key).toLowerCase()),
    );
    if (hit) return hit[0];
  }
  return null;
};

/** [state, city, postal, IANA timezone] — region is derived via regionFor(). */
export const LOCATIONS = [
  ['California', 'San Jose', '95131', 'America/Los_Angeles'],
  ['California', 'Fresno', '93721', 'America/Los_Angeles'],
  ['Washington', 'Seattle', '98108', 'America/Los_Angeles'],
  ['Oregon', 'Portland', '97210', 'America/Los_Angeles'],
  ['Arizona', 'Phoenix', '85034', 'America/Phoenix'],
  ['Colorado', 'Denver', '80216', 'America/Denver'],
  ['Utah', 'Salt Lake City', '84104', 'America/Denver'],
  ['Texas', 'Houston', '77032', 'America/Chicago'],
  ['Texas', 'Dallas', '75207', 'America/Chicago'],
  ['Georgia', 'Atlanta', '30318', 'America/New_York'],
  ['North Carolina', 'Charlotte', '28206', 'America/New_York'],
  ['Tennessee', 'Nashville', '37210', 'America/Chicago'],
  ['Florida', 'Tampa', '33619', 'America/New_York'],
  ['Alabama', 'Birmingham', '35211', 'America/Chicago'],
  ['Michigan', 'Detroit', '48210', 'America/New_York'],
  ['Ohio', 'Cleveland', '44115', 'America/New_York'],
  ['Illinois', 'Chicago', '60632', 'America/Chicago'],
  ['Indiana', 'Indianapolis', '46225', 'America/Indiana/Indianapolis'],
  ['Wisconsin', 'Milwaukee', '53204', 'America/Chicago'],
  ['Minnesota', 'Minneapolis', '55413', 'America/Chicago'],
  ['Missouri', 'St. Louis', '63118', 'America/Chicago'],
  ['Pennsylvania', 'Pittsburgh', '15222', 'America/New_York'],
  ['New York', 'Buffalo', '14206', 'America/New_York'],
  ['Massachusetts', 'Worcester', '01604', 'America/New_York'],
  ['New Jersey', 'Newark', '07105', 'America/New_York'],
  ['Connecticut', 'Hartford', '06103', 'America/New_York'],
];

/** All 50 states + DC -> IANA timezone, for the timezone_mapping catalog. */
export const STATE_TIMEZONES = [
  ['Alabama', 'America/Chicago'], ['Alaska', 'America/Anchorage'],
  ['Arizona', 'America/Phoenix'], ['Arkansas', 'America/Chicago'],
  ['California', 'America/Los_Angeles'], ['Colorado', 'America/Denver'],
  ['Connecticut', 'America/New_York'], ['Delaware', 'America/New_York'],
  ['District of Columbia', 'America/New_York'], ['Florida', 'America/New_York'],
  ['Georgia', 'America/New_York'], ['Hawaii', 'Pacific/Honolulu'],
  ['Idaho', 'America/Boise'], ['Illinois', 'America/Chicago'],
  ['Indiana', 'America/Indiana/Indianapolis'], ['Iowa', 'America/Chicago'],
  ['Kansas', 'America/Chicago'], ['Kentucky', 'America/New_York'],
  ['Louisiana', 'America/Chicago'], ['Maine', 'America/New_York'],
  ['Maryland', 'America/New_York'], ['Massachusetts', 'America/New_York'],
  ['Michigan', 'America/New_York'], ['Minnesota', 'America/Chicago'],
  ['Mississippi', 'America/Chicago'], ['Missouri', 'America/Chicago'],
  ['Montana', 'America/Denver'], ['Nebraska', 'America/Chicago'],
  ['Nevada', 'America/Los_Angeles'], ['New Hampshire', 'America/New_York'],
  ['New Jersey', 'America/New_York'], ['New Mexico', 'America/Denver'],
  ['New York', 'America/New_York'], ['North Carolina', 'America/New_York'],
  ['North Dakota', 'America/Chicago'], ['Ohio', 'America/New_York'],
  ['Oklahoma', 'America/Chicago'], ['Oregon', 'America/Los_Angeles'],
  ['Pennsylvania', 'America/New_York'], ['Rhode Island', 'America/New_York'],
  ['South Carolina', 'America/New_York'], ['South Dakota', 'America/Chicago'],
  ['Tennessee', 'America/Chicago'], ['Texas', 'America/Chicago'],
  ['Utah', 'America/Denver'], ['Vermont', 'America/New_York'],
  ['Virginia', 'America/New_York'], ['Washington', 'America/Los_Angeles'],
  ['West Virginia', 'America/New_York'], ['Wisconsin', 'America/Chicago'],
  ['Wyoming', 'America/Denver'],
];

/** Country-level rows, so timezone_mapping carries both LocationTypes (STATE + COUNTRY). */
export const COUNTRY_TIMEZONES = [
  ['United States', 'America/New_York'], ['Canada', 'America/Toronto'],
  ['Mexico', 'America/Mexico_City'], ['United Kingdom', 'Europe/London'],
  ['Germany', 'Europe/Berlin'], ['Japan', 'Asia/Tokyo'], ['Singapore', 'Asia/Singapore'],
];

// ---- people --------------------------------------------------------------------------
export const FIRST_NAMES = [
  'James', 'Maria', 'Robert', 'Linda', 'Michael', 'Patricia', 'David', 'Jennifer',
  'William', 'Susan', 'Richard', 'Karen', 'Joseph', 'Nancy', 'Thomas', 'Lisa',
  'Charles', 'Betty', 'Daniel', 'Sandra', 'Matthew', 'Ashley', 'Anthony', 'Kimberly',
  'Mark', 'Emily', 'Donald', 'Michelle', 'Steven', 'Carol', 'Paul', 'Amanda',
  'Andrew', 'Melissa', 'Joshua', 'Deborah', 'Kenneth', 'Stephanie', 'Kevin', 'Rebecca',
  'Brian', 'Laura', 'George', 'Sharon', 'Timothy', 'Cynthia', 'Ronald', 'Kathleen',
  'Priya', 'Rajesh', 'Wei', 'Ana', 'Carlos', 'Hiroshi', 'Fatima', 'Dmitri',
];
export const LAST_NAMES = [
  'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis',
  'Rodriguez', 'Martinez', 'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson', 'Thomas',
  'Taylor', 'Moore', 'Jackson', 'Martin', 'Lee', 'Perez', 'Thompson', 'White',
  'Harris', 'Sanchez', 'Clark', 'Ramirez', 'Lewis', 'Robinson', 'Walker', 'Young',
  'Allen', 'King', 'Wright', 'Scott', 'Torres', 'Nguyen', 'Hill', 'Flores',
  'Green', 'Adams', 'Nelson', 'Baker', 'Hall', 'Rivera', 'Campbell', 'Mitchell',
  'Patel', 'Kumar', 'Chen', 'Okafor', 'Novak', 'Ivanov', 'Silva', 'Kowalski',
];

/** [title, department, seniority, canonical_title, normalized tokens] */
export const TITLES = [
  ['Chief Executive Officer', 'Executive', 'C_LEVEL', 'CEO', ['chief', 'executive', 'officer']],
  ['Chief Technology Officer', 'Engineering', 'C_LEVEL', 'CTO', ['chief', 'technology', 'officer']],
  ['Chief Information Officer', 'Information Technology', 'C_LEVEL', 'CIO', ['chief', 'information', 'officer']],
  ['Chief Operating Officer', 'Operations', 'C_LEVEL', 'COO', ['chief', 'operating', 'officer']],
  ['VP of Manufacturing', 'Operations', 'VP', 'VP Manufacturing', ['vp', 'manufacturing']],
  ['VP of Engineering', 'Engineering', 'VP', 'VP Engineering', ['vp', 'engineering']],
  ['VP of Supply Chain', 'Supply Chain', 'VP', 'VP Supply Chain', ['vp', 'supply', 'chain']],
  ['VP of Information Technology', 'Information Technology', 'VP', 'VP IT', ['vp', 'information', 'technology']],
  ['Director of Operations', 'Operations', 'DIRECTOR', 'Director Operations', ['director', 'operations']],
  ['Director of Plant Engineering', 'Engineering', 'DIRECTOR', 'Director Engineering', ['director', 'plant', 'engineering']],
  ['Director of Procurement', 'Procurement', 'DIRECTOR', 'Director Procurement', ['director', 'procurement']],
  ['IT Director', 'Information Technology', 'DIRECTOR', 'Director IT', ['it', 'director']],
  ['Plant Manager', 'Operations', 'MANAGER', 'Plant Manager', ['plant', 'manager']],
  ['Manufacturing Engineering Manager', 'Engineering', 'MANAGER', 'Engineering Manager', ['manufacturing', 'engineering', 'manager']],
  ['Quality Assurance Manager', 'Quality', 'MANAGER', 'QA Manager', ['quality', 'assurance', 'manager']],
  ['Supply Chain Manager', 'Supply Chain', 'MANAGER', 'Supply Chain Manager', ['supply', 'chain', 'manager']],
  ['ERP Program Manager', 'Information Technology', 'MANAGER', 'Program Manager', ['erp', 'program', 'manager']],
  ['Data Platform Lead', 'Information Technology', 'MANAGER', 'Data Lead', ['data', 'platform', 'lead']],
  ['Senior Process Engineer', 'Engineering', 'SENIOR', 'Process Engineer', ['senior', 'process', 'engineer']],
  ['Automation Engineer', 'Engineering', 'ENTRY', 'Automation Engineer', ['automation', 'engineer']],
  ['Procurement Specialist', 'Procurement', 'ENTRY', 'Procurement Specialist', ['procurement', 'specialist']],
  ['Maintenance Supervisor', 'Operations', 'MANAGER', 'Maintenance Supervisor', ['maintenance', 'supervisor']],
];

/** Apollo `seniority` facet values (lowercase in Apollo's API, which is what gets stored). */
export const SENIORITIES = ['c_suite', 'vp', 'director', 'manager', 'senior', 'entry', 'owner', 'partner'];
export const DEPARTMENTS = [...new Set(TITLES.map((t) => t[1]))];

/** Common abbreviation expansions the title normalizer records. */
export const TITLE_ABBREVIATIONS = {
  CEO: 'Chief Executive Officer', CTO: 'Chief Technology Officer',
  CIO: 'Chief Information Officer', COO: 'Chief Operating Officer',
  VP: 'Vice President', IT: 'Information Technology',
  QA: 'Quality Assurance', ERP: 'Enterprise Resource Planning',
};

// ---- tenants / workspaces ------------------------------------------------------------
export const TENANTS = [
  { name: 'Limark Industrial', domain: 'limark.com' },
  { name: 'Corelabs GTM', domain: 'corelabs.io' },
  { name: 'Northstar Sourcing', domain: 'northstarsourcing.com' },
  { name: 'Vantage Supply Partners', domain: 'vantagesupply.com' },
];

// ---- marketplace catalogs ------------------------------------------------------------
export const SERVICE_CATEGORIES = [
  'Machining & Fabrication', 'Tooling & Molding', 'Finishing & Coating',
  'Assembly & Integration', 'Testing & Inspection', 'Logistics & Fulfillment',
];
/** [service, category index] */
export const SERVICES = [
  ['CNC Milling', 0], ['CNC Turning', 0], ['Sheet Metal Fabrication', 0], ['Waterjet Cutting', 0],
  ['Laser Cutting', 0], ['Welding', 0],
  ['Injection Molding', 1], ['Die Casting', 1], ['Tool & Die Making', 1], ['Thermoforming', 1],
  ['Powder Coating', 2], ['Anodizing', 2], ['Electroplating', 2], ['Passivation', 2],
  ['Contract Assembly', 3], ['Cable Harness Assembly', 3], ['PCB Assembly', 3],
  ['Systems Integration', 3],
  ['CMM Inspection', 4], ['Non-Destructive Testing', 4], ['Materials Testing', 4],
  ['Environmental Testing', 4],
  ['Kitting & Packaging', 5], ['Warehousing', 5], ['Freight Forwarding', 5],
];
export const SPECIFICATION_CATEGORIES = [
  ['Material', 'MATERIAL'], ['Tolerance', 'TOLERANCE'], ['Certification', 'CERTIFICATION'],
  ['Finish', 'FINISH'], ['Volume', 'VOLUME'], ['Lead Time', 'LEAD_TIME'],
];
/** [specification, category index] */
export const SPECIFICATIONS = [
  ['Aluminum 6061', 0], ['Stainless Steel 304', 0], ['Stainless Steel 316L', 0], ['Titanium Grade 5', 0],
  ['ABS', 0], ['Polycarbonate', 0], ['Inconel 718', 0],
  ['±0.001 in', 1], ['±0.005 in', 1], ['±0.010 in', 1],
  ['ISO 9001', 2], ['AS9100', 2], ['IATF 16949', 2], ['ITAR Registered', 2], ['ISO 13485', 2],
  ['Bead Blast', 3], ['Brushed', 3], ['Mirror Polish', 3], ['As Machined', 3],
  ['Prototype (1-10)', 4], ['Low Volume (11-500)', 4], ['Production (500+)', 4],
  ['Standard (4-6 weeks)', 5], ['Expedited (1-2 weeks)', 5], ['Rush (< 1 week)', 5],
];

export const VENDOR_NAMES = [
  'Precision Edge Machining', 'Great Lakes Tool & Die', 'Cascade Metal Works',
  'Ironbound Fabrication', 'Summit Coating Solutions', 'Delta Assembly Services',
  'Keystone Inspection Labs', 'Rivergate Logistics', 'Foundry Row Castings',
  'Blackhawk CNC', 'Sierra Composites', 'Harbor Point Plastics',
  'Anvil & Arc Welding', 'Northfield Metrology', 'Crossroads Kitting',
];

export const CERTIFICATIONS = ['ISO 9001:2015', 'AS9100D', 'IATF 16949:2016', 'ISO 13485:2016', 'ITAR Registered', 'NADCAP', 'ISO 14001'];
export const LANGUAGES = ['English', 'Spanish', 'Mandarin', 'German', 'French', 'Vietnamese'];
export const COMPANY_SIZES = ['1-10', '11-50', '51-200', '201-500', '501-1000', '1000+'];
export const ANNUAL_REVENUES = ['< $1M', '$1M - $5M', '$5M - $10M', '$10M - $50M', '$50M - $100M', '$100M+'];
export const HOURLY_RATES = ['$50 - $75', '$75 - $100', '$100 - $150', '$150 - $200', '$200+'];
export const PROJECT_SIZES = ['$1,000+', '$5,000+', '$10,000+', '$25,000+', '$50,000+'];
export const REVENUE_RANGES = ['0-1M', '1M-10M', '10M-50M', '50M-100M', '100M-500M', '500M+'];
