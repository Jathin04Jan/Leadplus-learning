/**
 * Admin, ingestion and search-history tables: Apollo specs + raw response cache, the
 * scraper job log, CSV import batches, saved lists/notes/queries and search history.
 *
 * Two size traps live here, both real bugs in the schema rather than seed choices:
 *   - apollo_specification.person_titles / person_seniorities and all three
 *     apollo_company_search_specification list columns are varchar(255) but hold a JSON
 *     array (StringListConverter). A long list genuinely overflows and throws at insert in
 *     prod. We keep the lists short so the rows fit — the same constraint prod lives under.
 */
import * as E from '../enums.js';
import * as C from '../catalog.js';
import { json, enumList } from '../format.js';
import { int, pick, chance, pickSome, pickBetween, sparse, between, after } from '../rng.js';

// ---- apollo_specification / apollo_company_search_specification ----------------------

export const buildApolloSpecifications = (userIds) =>
  [
    { titles: ['VP of Manufacturing', 'Plant Manager'], seniorities: ['vp', 'director'] },
    { titles: ['IT Director', 'ERP Program Manager'], seniorities: ['director', 'manager'] },
    { titles: ['Chief Technology Officer'], seniorities: ['c_suite'] },
    { titles: ['Supply Chain Manager'], seniorities: ['manager', 'senior'] },
    { titles: ['Quality Assurance Manager'], seniorities: ['manager'] },
  ].map((s, i) => ({
    // StringListConverter -> JSON array, into a varchar(255) column. Lists stay short.
    person_titles: json(s.titles),
    person_seniorities: json(s.seniorities),
    person_title_enabled: chance(0.8),
    person_seniority_enabled: chance(0.75),
    created_by: pick(userIds),
    created_at: between(500, 100),
  }));

export const buildApolloCompanySearchSpecifications = (userIds) =>
  [
    { ranges: ['501,1000', '1001,5000'], tags: ['manufacturing', 'industrial'], uids: ['sap', 'aws'] },
    { ranges: ['1001,5000', '5001,10000'], tags: ['smart_factory'], uids: ['snowflake', 'aws'] },
    { ranges: ['0,500'], tags: ['machining', 'fabrication'], uids: ['netsuite'] },
    { ranges: ['10001,1000000'], tags: ['aerospace'], uids: ['oracle', 'azure'] },
  ].map((s, i) => ({
    // Apollo's organization-search API takes ranges as "min,max" strings, not our labels.
    employee_ranges: json(s.ranges),
    keyword_tags: json(s.tags),
    technology_uids: json(s.uids),
    employee_range_enabled: chance(0.85),
    keyword_tag_enabled: chance(0.8),
    technology_uid_enabled: chance(0.75),
    created_by: pick(userIds),
    created_at: between(500, 100),
  }));

// ---- apollo_company_data / apollo_contact_data ---------------------------------------
// `data` is an opaque raw-Apollo JSON string (the entity stores organizationNode.toString()
// with no converter), so we reproduce Apollo's response shape, not our own DTOs.

const apolloOrgBlob = (company) => ({
  id: `org_${Math.abs(int(1e9, 9e9)).toString(16)}`,
  name: company.name,
  website_url: company.website_url,
  primary_domain: company.domain,
  linkedin_url: company.linkedin_url,
  industry: company.industry.toLowerCase(),
  keywords: company.keywords,
  estimated_num_employees: Number(company.employee_count),
  annual_revenue: Number(company.revenue_usd),
  publicly_traded_symbol: company.publicly_traded_symbol,
  primary_phone: { number: company.phone_number, source: 'Owler' },
  city: company.hq_city,
  state: company.hq_state,
  country: company.hq_country,
  postal_code: company.postal_code,
  technology_names: company.technologies,
  current_technologies: company.technologies.map((name) => ({
    uid: name.toLowerCase().replace(/[^a-z0-9]+/g, '_'),
    name,
    category: /AWS|Azure|Google Cloud/.test(name) ? 'Hosting'
      : /SAP|Oracle|NetSuite|Dynamics|Infor|Epicor/.test(name) ? 'Enterprise Resource Planning'
        : /Snowflake|Databricks|Redshift|BigQuery|Teradata/.test(name) ? 'Data Warehousing' : 'Other',
  })),
});

const apolloPersonBlob = (contact, company) => ({
  id: contact.apollo_id ?? `per_${Math.abs(int(1e9, 9e9)).toString(16)}`,
  first_name: contact.first_name,
  last_name: contact.last_name,
  name: contact.full_name,
  linkedin_url: contact.linkedin_url,
  title: contact.title,
  seniority: contact.seniority,
  departments: [contact.department],
  email: contact.email,
  // The boolean that ends up (stringified) in lead_contact.email_status.
  has_email: contact.email_status === 'true',
  email_status: contact.email_status === 'true' ? 'verified' : 'unavailable',
  city: contact.location_city,
  state: contact.location_state,
  country: contact.location_country,
  organization: { id: `org_${Math.abs(int(1e9, 9e9)).toString(16)}`, name: company?.name, primary_domain: contact.company_domain },
});

export const buildApolloCompanyData = (companies, companyIds, specIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, companies.length - 1);
    // Only Apollo-sourced companies have an Apollo response cached.
    if (companies[ci].source !== 'apollo') continue;
    rows.push({
      lead_company_id: companyIds[ci],
      specification_id: sparse(rows.length, 0.7, pick(specIds)),
      type: pick(['ORGANIZATION_ENRICHMENT', 'ORGANIZATION_SEARCH']),
      data: json(apolloOrgBlob(companies[ci])),
      fetched_at: after(companies[ci].created_at, 30),
    });
  }
  return rows;
};

export const buildApolloContactData = (contacts, contactIds, companies, specIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, contacts.length - 1);
    if (!contacts[ci].apollo_enriched) continue;
    const company = companies.find((c) => c.domain === contacts[ci].company_domain);
    rows.push({
      lead_contact_id: contactIds[ci],
      specification_id: sparse(rows.length, 0.7, pick(specIds)),
      type: pick(['PEOPLE_SEARCH', 'SINGLE_ENRICHMENT', 'BULK_ENRICHMENT']),
      data: json(apolloPersonBlob(contacts[ci], company)),
      fetched_at: after(contacts[ci].created_at, 30),
    });
  }
  return rows;
};

// ---- scrape_job ----------------------------------------------------------------------

export const buildScrapeJobs = (companies, companyIds, jobs, jobIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, companies.length - 1);
    const sourceType = chance(0.6) ? 'COMPANY_WEBSITE' : 'COMPANY_JOB';
    const status = chance(0.75) ? 'COMPLETED' : pick(E.ScrapeJobStatus);
    const scheduled = between(200, 1);
    // A COMPANY_JOB scrape points at the posting it parsed; a website scrape at the company.
    const sourceId = sourceType === 'COMPANY_JOB' && jobIds.length ? pick(jobIds) : companyIds[ci];

    rows.push({
      company_id: companyIds[ci],
      source_type: sourceType,
      source_id: sourceId,
      scraper_job_id: sparse(i, 0.9, `sj_${Math.abs(int(1e8, 9e8)).toString(36)}`),
      status,
      scheduled_at: scheduled,
      completed_at: status === 'PENDING' ? null : after(scheduled, 1),
      created_at: scheduled,
      updated_at: after(scheduled, 2),
    });
  }
  return rows;
};

// ---- lead_list -----------------------------------------------------------------------

const LIST_NAMES = [
  'Golden Set — SAP + Snowflake + AWS', 'Midwest Manufacturing', 'Target Accounts FY26',
  'Hiring Signal — Automation', 'Aerospace Suppliers', 'Lapsed Accounts',
  'Enterprise Named', 'Do Not Contact — Legal Hold', 'IMTS Booth Scans',
  'Snowflake Adopters', 'Tier-1 Automotive', 'Quality Leaders',
];

export const buildLeadLists = (tenants, tenantIds, workspaces, workspaceIds, users, userIds, companyIds, contactIds) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    const wsForTenant = workspaceIds.filter((_, wi) => workspaces[wi].tenant_id === tenantId);
    const tenantUsers = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === tenantId);
    const n = ti === 0 ? 5 : 3; // 14 lists

    for (let j = 0; j < n; j++) {
      const i = rows.length;
      const type = chance(0.5) ? 'LEAD_COMPANY' : 'LEAD_CONTACT';
      const createdAt = between(300, 10);
      rows.push({
        tenant_id: tenantId,
        workspace_id: pick(wsForTenant),
        name: LIST_NAMES[i % LIST_NAMES.length],
        type,
        // varchar[] of stringified ids of whichever entity `type` names.
        source_ids: (type === 'LEAD_COMPANY' ? pickSome(companyIds, int(5, 30)) : pickSome(contactIds, int(5, 40))).map(String),
        active: chance(0.92),
        created_by: pick(tenantUsers).id,
        updated_by: sparse(i, 0.6, pick(tenantUsers).id),
        created_at: createdAt,
        updated_at: after(createdAt, 60),
      });
    }
  });
  return rows;
};

// ---- lead_note -----------------------------------------------------------------------

const COMPANY_NOTES = [
  'Plant tour scheduled — bring the automation retrofit deck.',
  'They evaluated us two years ago and went with an incumbent. New CIO since.',
  'Procurement consolidating suppliers this year; we are on the shortlist.',
  'Capacity constrained at the main plant — this is the wedge.',
];
const CONTACT_NOTES = [
  'Prefers a technical conversation first; send the architecture one-pager.',
  'Owns the budget but defers to the plant manager on timing.',
  'Met at IMTS. Asked for references from similar-size manufacturers.',
  'Do not call before 10am local — early shift walkthroughs.',
];

export const buildLeadNotes = (companies, companyIds, contacts, contactIds, workspaces, workspaceIds, users, userIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const onCompany = chance(0.5);
    const type = onCompany ? 'LEAD_COMPANY' : 'LEAD_CONTACT';
    const idx = onCompany ? int(0, companies.length - 1) : int(0, contacts.length - 1);
    const parent = onCompany ? companies[idx] : contacts[idx];
    const sourceId = onCompany ? companyIds[idx] : contactIds[idx];
    const tenantUsers = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === parent.tenant_id);
    const wsForTenant = workspaceIds.filter((_, wi) => workspaces[wi].tenant_id === parent.tenant_id);
    const createdAt = after(parent.created_at, 90);

    rows.push({
      type,
      source_id: sourceId,
      note: pick(onCompany ? COMPANY_NOTES : CONTACT_NOTES),
      tenant_id: parent.tenant_id,
      workspace_id: sparse(i, 0.85, pick(wsForTenant)),
      active: chance(0.93),
      created_by: pick(tenantUsers).id,
      updated_by: sparse(i, 0.5, pick(tenantUsers).id),
      created_at: createdAt,
      updated_at: after(createdAt, 30),
    });
  }
  return rows;
};

// ---- lead_query ----------------------------------------------------------------------
// The distinct-value cache behind the search filter dropdowns: one row per (type, value).

export const buildLeadQueries = (companies, contacts) => {
  const rows = [];
  const add = (type, values) =>
    [...new Set(values.filter(Boolean))].forEach((value) =>
      rows.push({ type, value, created_at: between(400, 100) }),
    );

  add('COMPANY_INDUSTRY', companies.map((c) => c.industry));
  add('COMPANY_REGION', companies.map((c) => c.region));
  add('COMPANY_COUNTRY', companies.map((c) => c.hq_country));
  add('COMPANY_STATE', companies.map((c) => c.hq_state));
  add('COMPANY_CITY', companies.map((c) => c.hq_city));
  add('CONTACT_SENIORITY', contacts.map((c) => c.seniority));
  add('CONTACT_TITLE', contacts.map((c) => c.title));
  return rows;
};

// ---- lead_search_history -------------------------------------------------------------
// Includes the two searches the demo is built around, so the history shows the ALL-vs-ANY
// contrast with the result counts the pool actually returns.

export const buildSearchHistories = (users, userIds, count) => {
  const rows = [];

  // A saved search records the whole filter panel, so most facets are present even when the
  // user left them empty — an unused facet is an empty array, not a NULL.
  const base = (i) => ({
    cities: sparse(i, 0.7, pickBetween(C.LOCATIONS.map((l) => l[1]), 1, 3)),
    company_cities: sparse(i, 0.6, pickBetween(C.LOCATIONS.map((l) => l[1]), 1, 3)),
    company_countries: sparse(i, 0.75, ['United States']),
    company_names: sparse(i, 0.55, []),
    company_states: sparse(i, 0.7, pickBetween(C.LOCATIONS.map((l) => l[0]), 1, 4)),
    contact_names: sparse(i, 0.55, []),
    countries: sparse(i, 0.75, ['United States']),
    departments: sparse(i, 0.65, pickBetween(C.DEPARTMENTS, 1, 3)),
    industries: sparse(i, 0.85, ['Manufacturing']),
    // EnumListConverter: constant NAMES, not the "0-500" labels used in JSON blobs.
    employee_ranges: enumList(pickBetween(['RANGE_0_500', 'RANGE_501_1000', 'RANGE_1001_5000', 'RANGE_5001_10000'], 1, 2)),
    keywords: sparse(i, 0.7, pickBetween(C.KEYWORDS, 1, 3)),
    naics_codes: sparse(i, 0.55, pickSome(C.NAICS, 1)),
    postal_codes: sparse(i, 0.55, []),
    regions: sparse(i, 0.65, pickBetween(C.REGIONS.slice(0, 4), 1, 2)),
    revenue_ranges: sparse(i, 0.6, pickBetween(C.REVENUE_RANGES, 1, 2)),
    seniority: sparse(i, 0.7, pickBetween(C.SENIORITIES, 1, 3)),
    sic_codes: sparse(i, 0.55, pickSome(C.SIC, 1)),
    states: sparse(i, 0.7, pickBetween(C.LOCATIONS.map((l) => l[0]), 1, 4)),
    titles: sparse(i, 0.65, pickBetween(C.TITLES.map((t) => t[0]), 1, 3)),
  });

  // The two headline searches, pinned first so they always exist and always look right.
  const pinned = [
    { title: 'Manufacturing + SAP, Snowflake, AWS (ALL)', mode: 'ALL', technologies: ['SAP', 'Snowflake', 'AWS'], result_count: 12 },
    { title: 'SAP, Snowflake or AWS (ANY)', mode: 'ANY', technologies: ['SAP', 'Snowflake', 'AWS'], result_count: 201 },
  ];

  pinned.forEach((p, i) => {
    const createdAt = between(30, 1);
    rows.push({
      ...base(i),
      title: p.title,
      type: 'LEAD_COMPANY',
      keyword_match_mode: p.mode,
      technologies: p.technologies,
      industries: ['Manufacturing'],
      result_count: p.result_count,
      user_id: userIds[i % userIds.length],
      created_at: createdAt,
      updated_at: after(createdAt, 2),
    });
  });

  for (let i = pinned.length; i < count; i++) {
    const mode = chance(0.5) ? 'ALL' : 'ANY';
    const technologies = mode === 'ALL' ? pickBetween(['SAP', 'Snowflake', 'AWS', 'Salesforce'], 2, 3) : pickBetween(C.ALL_TECH, 1, 3);
    const createdAt = between(120, 0);

    rows.push({
      ...base(i),
      title: sparse(i, 0.7, `${technologies.join(mode === 'ALL' ? ' + ' : ' or ')} (${mode})`),
      type: pick(E.LeadType),
      keyword_match_mode: mode,
      technologies,
      // ALL narrows hard, ANY floods — the counts must reflect that or the history lies.
      result_count: sparse(i, 0.9, mode === 'ALL' ? int(4, 40) : int(120, 260)),
      user_id: pick(userIds),
      created_at: createdAt,
      updated_at: after(createdAt, 2),
    });
  }
  return rows;
};

// ---- lead_file_import / lead_file_import_record --------------------------------------

const IMPORT_FILES = [
  ['imts-2026-booth-scans.csv', 'CONTACT_CREATE', 'LEAD_CONTACT'],
  ['apollo-export-manufacturing.csv', 'CONTACT_ENRICHMENT', 'LEAD_CONTACT'],
  ['target-accounts-fy26.csv', 'COMPANY_CREATE', 'LEAD_COMPANY'],
  ['zoominfo-company-refresh.csv', 'COMPANY_ENRICHMENT', 'LEAD_COMPANY'],
  ['partner-referrals.csv', 'CONTACT_CREATE', 'LEAD_CONTACT'],
  ['crm-backfill.csv', 'CONTACT_ENRICHMENT', 'LEAD_CONTACT'],
];

const FIELD_MAPS = {
  LEAD_CONTACT: { 'First Name': 'firstName', 'Last Name': 'lastName', Email: 'email', Title: 'title', Company: 'companyDomain', 'Phone': 'phonee164' },
  LEAD_COMPANY: { 'Company Name': 'name', Domain: 'domain', Industry: 'industry', 'HQ State': 'hqState', Employees: 'employeeCount', Technologies: 'technologies' },
};

export const buildImports = (tenantIds, users, userIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const [file, importType, targetEntity] = IMPORT_FILES[i % IMPORT_FILES.length];
    const tenantId = tenantIds[i % tenantIds.length];
    const tenantUsers = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === tenantId);
    const uploader = pick(tenantUsers);
    const total = int(20, 400);
    const status = chance(0.6) ? 'COMPLETED' : pick(E.ImportStatus);

    // Counts must add up: processed = inserted + updated + skipped + failed.
    const done = ['COMPLETED', 'PARTIAL_SUCCESS', 'ROLLED_BACK'].includes(status);
    const processed = status === 'PENDING' ? 0 : done ? total : int(1, total);
    const failed = status === 'FAILED' ? processed : Math.round(processed * (status === 'PARTIAL_SUCCESS' ? 0.15 : 0.03));
    const skipped = Math.round((processed - failed) * 0.08);
    const inserted = /CREATE/.test(importType) ? Math.round((processed - failed - skipped) * 0.85) : Math.round((processed - failed - skipped) * 0.2);
    const updated = processed - failed - skipped - inserted;
    const started = between(200, 2);

    rows.push({
      tenant_id: tenantId,
      original_file_name: file,
      file_storage_key: `imports/${tenantId}/${Math.abs(int(1e8, 9e8)).toString(36)}/${file}`,
      // EnumListConverter, not JSON.
      import_types: enumList([importType]),
      target_entity: targetEntity,
      // Plain String field, no converter — raw JSON the app writes itself.
      mapped_fields_json: json(FIELD_MAPS[targetEntity]),
      total_records: total,
      processed_records: processed,
      inserted_records: inserted,
      updated_records: updated,
      skipped_records: skipped,
      failed_records: failed,
      status,
      // varchar, not bigint — the column really is a string id.
      uploaded_by_user_id: String(uploader.id),
      source_label: sparse(i, 0.75, pick(['csv-upload', 'imts-2026', 'apollo-export', 'partner-referral'])),
      started_at: status === 'PENDING' ? null : started,
      completed_at: done || status === 'FAILED' ? after(started, 1) : null,
      created_at: started,
      updated_at: after(started, 2),
    });
  }
  return rows;
};

export const buildImportRecords = (imports, importIds, companies, companyIds, contacts, contactIds, count) => {
  const rows = [];
  const perImport = Math.max(1, Math.floor(count / imports.length));

  imports.forEach((imp, ii) => {
    const isContact = imp.target_entity === 'LEAD_CONTACT';
    const n = Math.min(perImport, imp.processed_records || 1);

    for (let k = 0; k < n; k++) {
      const i = rows.length;
      const idx = isContact ? int(0, contacts.length - 1) : int(0, companies.length - 1);
      const entity = isContact ? contacts[idx] : companies[idx];
      const entityId = isContact ? contactIds[idx] : companyIds[idx];
      const status = imp.status === 'FAILED' ? 'FAILED' : pick(E.RecordStatus);
      const failed = status === 'FAILED';

      const raw = isContact
        ? { 'First Name': entity.first_name, 'Last Name': entity.last_name, Email: failed ? 'not-an-email' : entity.email, Title: entity.title, Company: entity.company_domain, Phone: entity.phonee164 }
        : { 'Company Name': entity.name, Domain: failed ? '' : entity.domain, Industry: entity.industry, 'HQ State': entity.hq_state, Employees: entity.employee_count, Technologies: (entity.technologies ?? []).join(', ') };

      const normalized = isContact
        ? { firstName: entity.first_name, lastName: entity.last_name, email: entity.email, title: entity.title, companyDomain: entity.company_domain, phonee164: entity.phonee164 }
        : { name: entity.name, domain: entity.domain, industry: entity.industry, hqState: entity.hq_state, employeeCount: entity.employee_count, technologies: entity.technologies };

      rows.push({
        import_id: importIds[ii],
        record_number: k + 1,
        // All three are plain String fields holding app-written raw JSON.
        raw_payload_json: json(raw),
        normalized_payload_json: failed ? null : json(normalized),
        match_key: sparse(i, 0.9, isContact ? entity.email : entity.domain),
        resolved_import_type: sparse(i, 0.9, imp.import_types.replace(/[{}]/g, '')),
        status,
        target_entity_id: ['INSERTED', 'UPDATED', 'NO_CHANGE'].includes(status) ? entityId : null,
        error_code: failed ? pick(['VALIDATION_ERROR', 'DUPLICATE_KEY', 'MISSING_REQUIRED_FIELD', 'DOMAIN_UNRESOLVABLE']) : null,
        error_message: failed ? pick(['Email is not a valid address.', 'A record with this key already exists in the tenant.', 'Required field is blank.', 'Domain could not be resolved to a company.']) : null,
        changes_json: status === 'UPDATED' ? json({ title: { from: 'Plant Supervisor', to: entity.title ?? 'Plant Manager' } }) : null,
        processed_at: after(imp.started_at ?? imp.created_at, 1),
        created_at: imp.created_at,
      });
    }
  });
  return rows;
};
