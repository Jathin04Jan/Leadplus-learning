/**
 * The lead pool: lead_company + its provenance chain (jobs, events, revisions) and
 * lead_contact + title normalization.
 *
 * This module carries the demo's whole point, so its shape is deliberate, not incidental:
 *
 *   - AWS is everywhere (~55-65% of companies), so an ANY/OR keyword search floods (~200).
 *   - A known GOLDEN set (12) is Manufacturing AND has SAP + Snowflake + AWS, so an ALL/AND
 *     search for "SAP, Snowflake, AWS" returns a precise ~12-15.
 *   - "Sapient Consulting Group" is a deliberate near-miss: its technographics include
 *     "Sapient Cloud Suite", which matches a naive `%sap%` substring filter without the
 *     company using SAP at all.
 *
 * Technographics are spread across BOTH sources the real pool has: the Apollo column
 * (`technologies`) and the scraper columns (`scraped_technologies` / `_tools` / `_services`),
 * plus `keywords`. The scraper columns are a roll-up of what the job-posting parser found,
 * so `lead_company_job` rows are generated FROM the parent's scraped_* arrays and their
 * `description` prose actually names those technologies. That provenance chain has to line
 * up or the demo's "where did this signal come from?" drill-down looks fabricated.
 */
import * as E from '../enums.js';
import * as C from '../catalog.js';
import { rnd, int, pick, chance, pickSome, pickBetween, sparse, sparseFn, between, after } from '../rng.js';

export const TOTAL_COMPANIES = 300;
export const GOLDEN = 12; // Manufacturing + SAP + Snowflake + AWS — the precise ALL-search answer

const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '');

// ---- lead_company --------------------------------------------------------------------

const makeCompany = (i, golden, tenantIds, userIds, usedDomains) => {
  let name = `${pick(C.PREFIX)} ${pick(C.SUFFIX)}`;
  let domain = `${slugify(name)}.com`;
  // (domain, tenant_id) is UNIQUE NULLS NOT DISTINCT — disambiguate collisions.
  let n = 2;
  while (usedDomains.has(domain)) {
    name = `${name.split(' — ')[0]} — ${['East', 'West', 'North', 'South', 'Central'][n - 2] || `Div ${n}`}`;
    domain = `${slugify(name)}.com`;
    n++;
  }
  usedDomains.add(domain);

  const [state, city, postal] = pick(C.LOCATIONS);
  // region is derived exactly as LeadCompanyService does — never free-chosen.
  const region = C.regionFor(state, city, 'United States');
  const [range, count] = pick(C.EMPLOYEE_RANGES);

  let technologies = [];
  if (golden) {
    // The golden set: exactly the ALL-search target.
    technologies = ['SAP', 'Snowflake', 'AWS', ...pickSome(C.CRM_TECH, 1)];
  } else {
    if (chance(0.35)) technologies.push(pick(C.ERP));
    if (chance(0.58)) technologies.push('AWS'); // AWS is everywhere -> ANY floods
    if (chance(0.35)) technologies.push(pick(C.CLOUD));
    if (chance(0.28)) technologies.push(pick(C.DATA));
    if (chance(0.4)) technologies.push(pick(C.CRM_TECH));
  }

  const createdAt = between(540, 30);
  const salesperson = chance(0.55) ? pick(userIds) : null;
  const scrapedTech = chance(0.6) ? pickBetween(C.SCRAPED_TECH, 1, 4) : [];

  return {
    name,
    domain,
    website_url: `https://www.${domain}`,
    industry: golden ? 'Manufacturing' : pick(C.INDUSTRIES),
    hq_city: city,
    hq_state: state,
    hq_country: 'United States',
    region,
    postal_code: sparse(i, 0.8, postal),
    territory: sparse(i, 0.6, region),
    employee_range: range,
    employee_count: count,
    revenue_usd: Math.round((rnd() * 900 + 5) * 100) / 100 * 1_000_000,
    revenue_usd_amount: null, // set below, kept consistent with revenue_usd
    keywords: pickBetween(C.KEYWORDS, 0, 4),
    technologies: [...new Set(technologies)],
    scraped_technologies: scrapedTech,
    scraped_tools: chance(0.5) ? pickBetween(C.SCRAPED_TOOLS, 1, 3) : [],
    scraped_services: chance(0.4) ? pickBetween(C.SCRAPED_SERVICES, 1, 3) : [],
    naics_codes: chance(0.7) ? pickSome(C.NAICS, 1) : [],
    sic_codes: chance(0.65) ? pickSome(C.SIC, 1) : [],
    segments: [pick(C.SEGMENTS)],
    active: true,
    exclusion: chance(0.04),
    is_target_account: golden || chance(0.2),
    score: golden ? int(70, 99) : int(0, 100),
    lead_company_status: pick(E.LeadCompanyStatus),
    // Free string, NOT the DataSource enum: the Apollo mapper writes lowercase "apollo"
    // while manual entry writes "MANUAL". The casing really is inconsistent in prod
    // (ApolloLeadCompanyService.java:217 vs TenantLeadRecordService.java:94) — copied
    // rather than tidied, so search behaves the way it does against the real pool.
    source: chance(0.75) ? 'apollo' : 'MANUAL',
    created_at: createdAt,
    updated_at: after(createdAt, 60),
    account_summary: sparseFn(i, 0.6, () => ''), // filled in below (needs the final row)
    icp_tag: sparse(i, 0.5, golden ? 'ICP_CORE' : pick(['ICP_CORE', 'ICP_ADJACENT', 'ICP_WATCH'])),
    linkedin_url: sparse(i, 0.85, `https://www.linkedin.com/company/${slugify(name)}`),
    twitter_url: sparse(i, 0.3, `https://twitter.com/${slugify(name).slice(0, 15)}`),
    facebook_url: sparse(i, 0.25, `https://www.facebook.com/${slugify(name)}`),
    logo_url: sparse(i, 0.7, `https://logo.clearbit.com/${domain}`),
    phone_number: sparse(i, 0.75, `+1${int(200, 989)}${int(200, 999)}${String(int(0, 9999)).padStart(4, '0')}`),
    publicly_traded_symbol: sparse(i, 0.08, name.split(' ')[0].slice(0, 4).toUpperCase()),
    salesperson_id: salesperson,
    salesperson_name: null, // resolved by the caller, which knows user names
    salesperson_assign_at: salesperson ? after(createdAt, 90) : null,
    zoho_account_id: sparse(i, 0.3, `zcrm_${int(100000, 999999)}`),
    notes: sparse(i, 0.55, ''),
    tenant_id: null, // set by caller
  };
};

const summaryFor = (c) => {
  const bits = [];
  bits.push(`${c.name} is a ${c.employee_range}-employee ${c.industry.toLowerCase()} company headquartered in ${c.hq_city}, ${c.hq_state}.`);
  if (c.technologies.length) bits.push(`Known technographics include ${c.technologies.join(', ')}.`);
  if (c.scraped_technologies.length) bits.push(`Recent job postings reference ${c.scraped_technologies.join(', ')}.`);
  if (c.keywords.length) bits.push(`Public messaging emphasizes ${c.keywords.join(' and ')}.`);
  return bits.join(' ');
};

export const buildCompanies = (tenantIds, users) => {
  const userIds = users.map((u) => u.id);
  const usedDomains = new Set();
  const rows = [];

  for (let i = 0; i < TOTAL_COMPANIES; i++) {
    const c = makeCompany(i, i < GOLDEN, tenantIds, userIds, usedDomains);
    rows.push(c);
  }

  // The deliberate near-miss: matches `%sap%` by substring without using SAP.
  const nearMiss = makeCompany(TOTAL_COMPANIES, false, tenantIds, userIds, usedDomains);
  Object.assign(nearMiss, {
    name: 'Sapient Consulting Group',
    domain: 'sapientconsulting.com',
    website_url: 'https://www.sapientconsulting.com',
    linkedin_url: 'https://www.linkedin.com/company/sapientconsulting',
    industry: 'Manufacturing',
    technologies: ['Sapient Cloud Suite', 'AWS', 'Snowflake'], // "Sap"ient — but NOT SAP
    is_target_account: false,
  });
  usedDomains.add('sapientconsulting.com');
  rows.push(nearMiss);

  // Finish the rows that need cross-field consistency.
  rows.forEach((c, i) => {
    c.tenant_id = tenantIds[i % tenantIds.length];
    c.revenue_usd_amount = Math.round(Number(c.revenue_usd));
    if (c.salesperson_id) {
      c.salesperson_name = users.find((u) => u.id === c.salesperson_id)?.name ?? null;
    }
    if (c.account_summary !== null) c.account_summary = summaryFor(c);
    if (c.notes !== null) {
      c.notes = pick([
        `Met at IMTS — evaluating ${c.technologies[0] ?? 'new tooling'} rollout next FY.`,
        'Referred by an existing account. Warm intro available through the VP of Ops.',
        'Procurement freeze until end of quarter; revisit after budget reset.',
        `Expanding the ${c.hq_city} plant — capacity constraints are the stated pain.`,
      ]);
    }
  });

  return rows;
};

// ---- lead_company_job ----------------------------------------------------------------
// Generated FROM the parent's scraped_* arrays: the description prose names the exact
// technologies that rolled up into the company row. That's the provenance chain.

const JOB_TITLES = [
  ['Senior Platform Engineer', 'Engineering', 'FULL_TIME'],
  ['Manufacturing Systems Engineer', 'Engineering', 'FULL_TIME'],
  ['Data Engineer', 'Information Technology', 'FULL_TIME'],
  ['ERP Analyst', 'Information Technology', 'FULL_TIME'],
  ['Automation Technician', 'Operations', 'FULL_TIME'],
  ['Process Improvement Lead', 'Operations', 'FULL_TIME'],
  ['Quality Engineer', 'Quality', 'FULL_TIME'],
  ['Supply Chain Analyst', 'Supply Chain', 'FULL_TIME'],
  ['Maintenance Planner', 'Operations', 'CONTRACT'],
  ['Controls Engineer Intern', 'Engineering', 'INTERNSHIP'],
];

const BENEFITS = ['401(k) matching', 'Medical, dental & vision', 'Paid parental leave', 'Tuition reimbursement', 'Relocation assistance', 'Annual bonus', 'On-site gym'];

const describeJob = (title, company, type, tech, tools, services, years) => {
  const p = [];
  p.push(`${company.name} is hiring a ${title} to join our ${company.hq_city}, ${company.hq_state} team.`);
  p.push(
    `You will work alongside operations and IT to modernize the systems running our production floor. ` +
      `Our stack runs on ${company.technologies.slice(0, 3).join(', ') || 'a mix of on-prem and cloud systems'}, and we are actively investing in the platform.`,
  );
  if (tech.length) {
    // With one technology, "X required, X a plus" reads like a template. Only name a
    // secondary technology when there actually is one.
    const required = `Hands-on experience with ${tech[0]} is required`;
    const plus = tech.length > 1 ? `; exposure to ${tech[tech.length - 1]} is a strong plus` : '';
    p.push(`Day to day you will build and operate services using ${tech.join(', ')}. ${required}${plus}.`);
  }
  if (tools.length) p.push(`Our teams coordinate in ${tools.join(' and ')}, and we expect you to be comfortable working in the open.`);
  if (services.length) p.push(`This role supports our ${services.join(' and ')} initiatives across the plant network.`);
  p.push(
    type === 'INTERNSHIP'
      ? 'Requirements: currently pursuing a bachelor\'s degree in a technical field, coursework in controls or manufacturing systems, and availability for a full-time summer term.'
      : `Requirements: bachelor's degree in a technical field, ${years}+ years of relevant experience, and a track record of shipping in a regulated manufacturing environment.`,
  );
  return p.join('\n\n');
};

export const buildJobs = (companies, companyIds) => {
  const rows = [];
  let i = 0;

  companies.forEach((company, ci) => {
    const companyId = companyIds[ci];
    // Companies the scraper found signal at post more jobs — that's why they have signal.
    const hasSignal = company.scraped_technologies.length > 0;
    const n = hasSignal ? int(1, 4) : chance(0.35) ? 1 : 0;

    for (let j = 0; j < n; j++) {
      const [title, department, type] = pick(JOB_TITLES);
      // Draw from the parent's roll-up so job -> company stays consistent.
      const tech = company.scraped_technologies.length
        ? pickBetween(company.scraped_technologies, 1, company.scraped_technologies.length)
        : pickSome(C.SCRAPED_TECH, 1);
      const tools = company.scraped_tools.length ? pickBetween(company.scraped_tools, 1, company.scraped_tools.length) : [];
      const services = company.scraped_services.length ? pickBetween(company.scraped_services, 1, company.scraped_services.length) : [];
      const posted = between(180, 2);
      const slug = `${slugify(title)}-${companyId}-${j}`;
      // An internship asking for 3+ years is the kind of tell that makes a pool look
      // generated, so experience tracks the employment type.
      const years = type === 'INTERNSHIP' ? 0 : type === 'CONTRACT' ? int(2, 6) : int(3, 10);

      rows.push({
        lead_company_id: companyId,
        title,
        department: sparse(i, 0.9, department),
        type: sparse(i, 0.85, type),
        location: `${company.hq_city}, ${company.hq_state}`,
        description: describeJob(title, company, type, tech, tools, services, years),
        // The parsed arrays — a subset of the parent's roll-up, plus the ERP the JD names.
        technologies: tech,
        tools,
        services,
        skills: [...new Set([...tech, ...pickSome(['Root cause analysis', 'SPC', 'Lean', 'Six Sigma', 'CAD'], int(1, 2))])],
        requirements: [
          type === 'INTERNSHIP' ? 'Currently pursuing a technical degree' : `${years}+ years experience`,
          "Bachelor's degree in a technical field",
          ...(tech.length ? [`Hands-on ${tech[0]}`] : []),
        ],
        benefits: pickBetween(BENEFITS, 2, 4),
        job_url: `https://careers.${company.domain}/jobs/${slug}`,
        apply_url: sparse(i, 0.8, `https://careers.${company.domain}/jobs/${slug}/apply`),
        posted_date: posted,
        active: chance(0.85),
        created_at: posted,
        updated_at: after(posted, 20),
      });
      i++;
    }
  });

  return rows;
};

// ---- lead_company_event --------------------------------------------------------------

const EVENT_COPY = {
  NEWS: (c) => [`${c.name} announces expansion of its ${c.hq_city} facility`, `${c.name} said the expansion will add capacity and headcount over the next 18 months.`],
  FUNDING: (c) => [`${c.name} raises $${int(5, 120)}M growth round`, `The round will fund automation and ${c.keywords[0] ?? 'digital transformation'} programs.`],
  HIRING: (c) => [`${c.name} is scaling its engineering team`, `Job postings reference ${c.scraped_technologies.join(', ') || 'platform modernization'}, suggesting active investment.`],
  PRODUCT: (c) => [`${c.name} launches a new product line`, `The launch targets ${c.industry.toLowerCase()} customers and expands the addressable footprint.`],
  PARTNERSHIP: (c) => [`${c.name} partners on plant-floor integration`, `The partnership covers ${c.scraped_services[0] ?? 'systems integration'} across multiple sites.`],
  OTHER: (c) => [`${c.name} named to a regional manufacturing index`, `Recognition cited operational performance and workforce growth.`],
};

export const buildCompanyEvents = (companies, companyIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, companies.length - 1);
    const company = companies[ci];
    const type = pick(E.EventType);
    const [title, summary] = EVENT_COPY[type](company);
    const published = between(400, 1);

    rows.push({
      lead_company_id: companyIds[ci],
      type,
      title,
      summary: sparse(i, 0.85, summary),
      source: pick(['Business Wire', 'PR Newswire', 'Manufacturing Dive', 'IndustryWeek', 'Company Blog', 'LinkedIn']),
      url: sparse(i, 0.9, `https://news.example.com/${slugify(company.name)}/${i}`),
      published_at: published,
      detected_at: after(published, 5),
      sentiment: sparse(i, 0.7, int(-2, 5)),
      unique_hash: `evt_${slugify(company.name).slice(0, 12)}_${i}`,
      active: chance(0.92),
    });
  }
  return rows;
};

// ---- lead_contact --------------------------------------------------------------------

// `consent_status`, `persona_match` and `icp_tag` are free strings with NO writer setting a
// literal anywhere in the backend — they arrive by DTO/CSV pass-through only. These are
// plausible conventions, not values the code constrains.
const CONSENT_STATUSES = ['GRANTED', 'PENDING', 'WITHDRAWN'];
const PERSONA_MATCHES = ['ECONOMIC_BUYER', 'TECHNICAL_EVALUATOR', 'CHAMPION', 'INFLUENCER', 'GATEKEEPER'];

export const buildContacts = (companies, companyIds, users, perCompany = [2, 6]) => {
  const rows = [];
  const userIds = users.map((u) => u.id);
  const usedEmails = new Set();
  let i = 0;

  companies.forEach((company, ci) => {
    const n = int(perCompany[0], perCompany[1]);
    for (let j = 0; j < n; j++) {
      const first = pick(C.FIRST_NAMES);
      const last = pick(C.LAST_NAMES);
      const [title, department, seniorityConst, , tokens] = pick(C.TITLES);

      let local = `${first}.${last}`.toLowerCase();
      let email = `${local}@${company.domain}`;
      let k = 2;
      // (email, tenant_id) is UNIQUE NULLS NOT DISTINCT.
      while (usedEmails.has(email)) {
        email = `${local}${k}@${company.domain}`;
        k++;
      }
      usedEmails.add(email);

      const createdAt = after(company.created_at, 45);
      const apolloEnriched = chance(0.7);

      rows.push({
        lead_company_id: companyIds[ci],
        company_domain: company.domain,
        tenant_id: company.tenant_id,
        first_name: first,
        last_name: last,
        full_name: `${first} ${last}`,
        first_name_normalized: first.toLowerCase(),
        last_name_normalized: last.toLowerCase(),
        email,
        title,
        department: sparse(i, 0.9, department),
        seniority: sparse(i, 0.9, seniorityConst.toLowerCase().replace('c_level', 'c_suite')),
        normalized_title_tokens: tokens,
        segments: company.segments,
        active: true,
        exclusion: chance(0.03),
        apollo_enriched: apolloEnriched,
        do_not_contact: chance(0.06),
        // data_source IS the DataSource enum; source is a free string ("apollo"/"MANUAL").
        data_source: apolloEnriched ? 'APOLLO' : 'MANUAL',
        source: apolloEnriched ? 'apollo' : 'MANUAL',
        apollo_id: apolloEnriched ? `${int(10, 99)}${slugify(last)}${int(1000, 9999)}` : null,
        // LeadContactMapper populates email_status from Apollo's `has_email` BOOLEAN via
        // asText(), so the real column holds the strings "true"/"false" — not
        // "verified"/"bounced" as the name suggests. Faithful to prod, deliberately.
        email_status: sparse(i, 0.85, apolloEnriched ? (chance(0.8) ? 'true' : 'false') : 'false'),
        consent_status: sparse(i, 0.6, pick(CONSENT_STATUSES)),
        persona_match: sparse(i, 0.55, pick(PERSONA_MATCHES)),
        persona_score: sparse(i, 0.65, int(0, 100)),
        linkedin_url: sparse(i, 0.8, `https://www.linkedin.com/in/${local.replace('.', '-')}-${int(100, 999)}`),
        location_city: sparse(i, 0.85, company.hq_city),
        location_state: sparse(i, 0.85, company.hq_state),
        location_country: sparse(i, 0.9, 'United States'),
        location_zip: sparse(i, 0.6, company.postal_code ?? '00000'),
        phonee164: sparse(i, 0.5, `+1${int(200, 989)}${int(200, 999)}${String(int(0, 9999)).padStart(4, '0')}`),
        owner_id: sparse(i, 0.6, pick(userIds)),
        notes: sparse(i, 0.55, pick([
          'Prefers email over calls. Responded well to the plant-automation angle.',
          'Owns the ERP budget line. Loop in before any commercial conversation.',
          'Out on leave until next quarter — assistant is the right first touch.',
          'Asked for a case study from a comparable-size manufacturer.',
        ])),
        created_at: createdAt,
        updated_at: after(createdAt, 60),
      });
      i++;
    }
  });

  return rows;
};

// ---- lead_contact_event --------------------------------------------------------------
// type -> category -> source_type must agree; the UI groups the timeline by category.

const EVENT_SHAPE = {
  CAMPAIGN_INITIATED: ['CAMPAIGN', 'CAMPAIGN', (c) => `Contact enrolled in a campaign sequence.`],
  CAMPAIGN_EMAIL_SENT: ['CAMPAIGN', 'CAMPAIGN_EMAIL', (c) => `Sequence step email delivered to ${c.email}.`],
  CAMPAIGN_EMAIL_REPLIED: ['CAMPAIGN', 'CAMPAIGN_CONTACT', (c) => `${c.full_name} replied to a sequence email.`],
  EMAIL_SENT: ['EMAIL', 'CONTACT_EMAIL', (c) => `One-off email sent to ${c.email}.`],
  EMAIL_OPENED: ['EMAIL', 'CONTACT_EMAIL', (c) => `${c.full_name} opened an email.`],
  NOTE_ADDED: ['NOTE', 'LEAD_NOTE', () => 'A note was added to this contact.'],
  NOTE_UPDATED: ['NOTE', 'LEAD_NOTE', () => 'A note on this contact was updated.'],
  NOTE_DELETED: ['NOTE', 'LEAD_NOTE', () => 'A note on this contact was deleted.'],
};

export const buildContactEvents = (contacts, contactIds, users, workspaces, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, contacts.length - 1);
    const contact = contacts[ci];
    const type = pick(E.LeadContactEventType);
    const [category, sourceType, describe] = EVENT_SHAPE[type];
    const user = pick(users);
    const at = after(contact.created_at, 120);

    rows.push({
      contact_id: contactIds[ci],
      type,
      category,
      source_type: sourceType,
      source_id: int(1, 50), // points at the campaign/note that raised it
      description: sparse(i, 0.85, describe(contact)),
      event_by: sparse(i, 0.8, user.email),
      event_at: at,
      created_at: at,
      active: chance(0.95),
      tenant_id: contact.tenant_id,
      workspace_id: sparse(i, 0.9, pick(workspaces.filter((w) => w.tenant_id === contact.tenant_id))?.id ?? workspaces[0].id),
    });
  }
  return rows;
};

// ---- lead_company_revision / lead_contact_revision -----------------------------------
// The revision tables are the @MappedSuperclass snapshot of their parent plus
// revision_action / reason / modified_by, so a revision row is literally the entity's
// column set at a point in time. Generating them from the live row keeps them honest.

const REVISION_REASON = {
  CREATE: 'Initial import from source.',
  UPDATE: 'Enrichment refresh updated firmographics.',
  DELETE: 'Soft-deleted — duplicate of an existing record.',
  RESTORE: 'Restored after review; the delete was a false positive.',
};

export const buildCompanyRevisions = (companies, companyIds, users, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, companies.length - 1);
    const c = companies[ci];
    const action = pick(E.RevisionAction);
    // Drop the columns the revision table doesn't carry, snapshot the rest.
    const { updated_at, updated_by, created_at, ...snapshot } = c;

    rows.push({
      ...snapshot,
      lead_company_id: companyIds[ci],
      revision_action: action,
      reason: sparse(i, 0.7, REVISION_REASON[action]),
      modified_by: sparse(i, 0.85, pick(users).id),
      active: action !== 'DELETE',
      created_at: after(c.created_at, 200),
    });
  }
  return rows;
};

export const buildContactRevisions = (contacts, contactIds, users, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, contacts.length - 1);
    const c = contacts[ci];
    const action = pick(E.RevisionAction);
    // lead_contact_revision has no lead_company_id — it keeps company_domain only.
    const { updated_at, created_at, lead_company_id, ...snapshot } = c;

    rows.push({
      ...snapshot,
      lead_contact_id: contactIds[ci],
      revision_action: action,
      reason: sparse(i, 0.7, REVISION_REASON[action]),
      modified_by: sparse(i, 0.85, pick(users).id),
      active: action !== 'DELETE',
      created_at: after(c.created_at, 200),
    });
  }
  return rows;
};

// ---- lead_contact_normalized_title ---------------------------------------------------

export const buildNormalizedTitles = (contacts, contactIds) =>
  contacts.map((contact, i) => {
    const entry = C.TITLES.find((t) => t[0] === contact.title);
    const [original, , seniorityConst, canonical, tokens] = entry;
    // TitleAbbreviationListConverter -> JSON array of {shortForm, fullForm} objects.
    const abbrevs = Object.entries(C.TITLE_ABBREVIATIONS)
      .filter(([k]) => canonical.includes(k) || original.includes(k))
      .map(([shortForm, fullForm]) => ({ shortForm, fullForm }));

    return {
      lead_contact_id: contactIds[i],
      original_title: original,
      canonical_title: canonical,
      seniority: seniorityConst.toLowerCase().replace('c_level', 'c_suite'),
      keywords: tokens,
      normalized_titles: [...new Set([canonical.toLowerCase(), original.toLowerCase(), tokens.join(' ')])],
      title_abbreviations: sparse(i, 0.6, JSON.stringify(abbrevs.length ? abbrevs : [{ shortForm: 'VP', fullForm: 'Vice President' }])),
      created_at: contact.created_at,
    };
  });
