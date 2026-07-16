/**
 * Acceptance checks for the synthetic replica. Run after `npm run seed`.
 *
 *   1. every table has rows
 *   2. no column is 100% NULL
 *   3. every *_id reference resolves to a row that exists
 *   4. the search-demo property still holds (ALL is precise, ANY floods)
 *
 * Check 3 matters most: the production schema has ZERO foreign-key constraints, so nothing
 * in the database will ever complain about a dangling reference. If the seed's build order
 * drifts, this is what catches it.
 *
 * Usage: node verify.js   (exits non-zero on any failure)
 */
import pg from 'pg';
import { config } from './config.js';

/**
 * child.column -> parent table. Only genuine references are listed: `source_id` columns are
 * polymorphic (their meaning depends on a sibling source_type), so they're checked
 * separately below rather than here.
 */
const REFS = {
  'workspace.tenant_id': 'tenant',
  'workspace.owner_id': 'tenant_user',
  'tenant.owner_id': 'tenant_user',
  'tenant_user.tenant_id': 'tenant',
  'tenant_user.workspace_id': 'workspace',
  'workspace_user.tenant_id': 'tenant',
  'workspace_user.workspace_id': 'workspace',
  'workspace_user.user_id': 'tenant_user',
  'mailbox.workspace_id': 'workspace',
  'mailbox.user_id': 'tenant_user',
  'refresh_token.user_id': 'tenant_user',
  'lead_company.tenant_id': 'tenant',
  'lead_company.salesperson_id': 'tenant_user',
  'lead_company_job.lead_company_id': 'lead_company',
  'lead_company_event.lead_company_id': 'lead_company',
  'lead_company_revision.lead_company_id': 'lead_company',
  'lead_company_revision.tenant_id': 'tenant',
  'lead_company_revision.modified_by': 'tenant_user',
  'lead_contact.lead_company_id': 'lead_company',
  'lead_contact.tenant_id': 'tenant',
  'lead_contact.owner_id': 'tenant_user',
  'lead_contact_revision.lead_contact_id': 'lead_contact',
  'lead_contact_revision.tenant_id': 'tenant',
  'lead_contact_revision.modified_by': 'tenant_user',
  'lead_contact_event.contact_id': 'lead_contact',
  'lead_contact_event.tenant_id': 'tenant',
  'lead_contact_event.workspace_id': 'workspace',
  'lead_contact_normalized_title.lead_contact_id': 'lead_contact',
  'lead_note.tenant_id': 'tenant',
  'lead_note.workspace_id': 'workspace',
  'lead_list.tenant_id': 'tenant',
  'lead_list.workspace_id': 'workspace',
  'campaign.tenant_id': 'tenant',
  'campaign.workspace_id': 'workspace',
  'campaign.sending_mailbox_id': 'mailbox',
  'campaign.template_id': 'sequence_template',
  'campaign.created_by': 'tenant_user',
  'campaign_email.campaign_id': 'campaign',
  'campaign_contact.campaign_id': 'campaign',
  'campaign_contact.contact_id': 'lead_contact',
  'campaign_chat_memory.campaign_id': 'campaign',
  'campaign_chat_memory.tenant_id': 'tenant',
  'campaign_chat_memory.workspace_id': 'workspace',
  'contact_email.campaign_id': 'campaign',
  'contact_email.contact_id': 'lead_contact',
  'contact_email.tenant_id': 'tenant',
  'contact_email.workspace_id': 'workspace',
  'contact_outreach_status.contact_id': 'lead_contact',
  'contact_outreach_status.tenant_id': 'tenant',
  'message.campaign_id': 'campaign',
  'message.tenant_id': 'tenant',
  'message.workspace_id': 'workspace',
  'message.user_id': 'tenant_user',
  'email_sequence_template.tenant_id': 'tenant',
  'tenant_company.tenant_id': 'tenant',
  'tenant_contact.tenant_id': 'tenant',
  'tenant_contact_metadata.tenant_id': 'tenant',
  'tenant_contact_metadata.lead_contact_id': 'lead_contact',
  'tenant_data_source.tenant_id': 'tenant',
  'tenant_data_source.import_batch_id': 'lead_file_import',
  'tenant_lead_filter.tenant_id': 'tenant',
  'tenant_announcement.tenant_id': 'tenant',
  'tenant_announcement_contact.announcement_id': 'tenant_announcement',
  'lead_file_import.tenant_id': 'tenant',
  'lead_file_import_record.import_id': 'lead_file_import',
  'territory_rep.tenant_id': 'tenant',
  'territory_state_assignment.tenant_id': 'tenant',
  'territory_state_assignment.rep_id': 'territory_rep',
  'service.service_category_id': 'service_category',
  'specification.specification_category_id': 'specification_category',
  'industry_service_mapping.industry_id': 'industry',
  'industry_service_mapping.service_id': 'service',
  'service_specification.service_id': 'service',
  'service_specification.specification_id': 'specification',
  'question.question_section_id': 'question_section',
  'vendor.tenant_id': 'tenant',
  'vendor.user_id': 'tenant_user',
  'vendor_agreement.vendor_id': 'vendor',
  'vendor_showcase.vendor_id': 'vendor',
  'vendor_showcase.tenant_id': 'tenant',
  'vendor_data_pack.vendor_id': 'vendor',
  'vendor_data_pack.lead_data_pack_id': 'lead_data_pack',
  'vendor_data_pack.tenant_id': 'tenant',
  'quotation.vendor_id': 'vendor',
  'quotation.tenant_id': 'tenant',
  'quotation.workspace_id': 'workspace',
  'request_for_quote.user_id': 'tenant_user',
  'request_for_proposal.user_id': 'tenant_user',
  'collaborator.user_id': 'tenant_user',
  'apollo_company_data.lead_company_id': 'lead_company',
  'apollo_company_data.specification_id': 'apollo_specification',
  'apollo_contact_data.lead_contact_id': 'lead_contact',
  'apollo_contact_data.specification_id': 'apollo_specification',
  'scrape_job.company_id': 'lead_company',
  'attachment_library.workspace_id': 'workspace',
  'feedback.tenant_id': 'tenant',
  'feedback.workspace_id': 'workspace',
  'feedback.user_id': 'tenant_user',
  'user_activity_log.tenant_id': 'tenant',
  'user_activity_log.workspace_id': 'workspace',
  'user_activity_log.user_id': 'tenant_user',
  'lead_search_history.user_id': 'tenant_user',
};

/** Polymorphic source_id columns: [table, idColumn, typeColumn, {typeValue: parentTable}]. */
const POLY = [
  ['attachment', 'source_id', 'source_type', {
    REQUEST_FOR_QUOTE: 'request_for_quote', REQUEST_FOR_PROPOSAL: 'request_for_proposal',
    SHOWCASE: 'vendor_showcase', QUOTATION: 'quotation',
  }],
  ['collaborator', 'source_id', 'source_type', {
    REQUEST_FOR_QUOTE: 'request_for_quote', REQUEST_FOR_PROPOSAL: 'request_for_proposal',
  }],
  ['quotation', 'source_id', 'source_type', {
    REQUEST_FOR_QUOTE: 'request_for_quote', REQUEST_FOR_PROPOSAL: 'request_for_proposal',
  }],
  ['lead_note', 'source_id', 'type', { LEAD_COMPANY: 'lead_company', LEAD_CONTACT: 'lead_contact' }],
  ['email_image', 'source_id', 'source_type', { CAMPAIGN: 'campaign', ANNOUNCEMENT: 'tenant_announcement', DIRECT: 'tenant_user' }],
  ['tenant_announcement_contact', 'source_id', 'source_type', { LEAD: 'lead_contact', CRM: 'tenant_contact' }],
  ['tenant_data_source', 'target_entity_id', 'target_entity_type', { LEAD_COMPANY: 'lead_company', LEAD_CONTACT: 'lead_contact' }],
];

const client = new pg.Client({ connectionString: config.databaseUrl });
const failures = [];
const ok = (msg) => console.log(`  ok    ${msg}`);
const fail = (msg) => { failures.push(msg); console.log(`  FAIL  ${msg}`); };

const run = async () => {
  await client.connect();
  await client.query('ANALYZE');

  // 1. every table has rows
  console.log('\n1. every table has rows');
  const { rows: tables } = await client.query(
    `SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1`,
  );
  let empty = 0;
  for (const { relname } of tables) {
    const { rows: [{ n }] } = await client.query(`SELECT count(*)::int AS n FROM public."${relname}"`);
    if (n === 0) { fail(`${relname} is empty`); empty++; }
  }
  if (!empty) ok(`all ${tables.length} tables populated`);

  // 2. no column is 100% NULL
  console.log('\n2. no column is 100% NULL');
  const { rows: cols } = await client.query(
    `SELECT table_name, column_name FROM information_schema.columns
      WHERE table_schema = 'public' ORDER BY 1, 2`,
  );
  let allNull = 0;
  for (const { table_name, column_name } of cols) {
    const { rows: [{ n }] } = await client.query(`SELECT count("${column_name}")::int AS n FROM public."${table_name}"`);
    if (n === 0) { fail(`${table_name}.${column_name} is 100% NULL`); allNull++; }
  }
  if (!allNull) ok(`all ${cols.length} columns have at least one non-null value`);

  // 3. referential integrity (no FKs exist — this is the only enforcement)
  console.log('\n3. referential integrity');
  let dangling = 0;
  for (const [ref, parent] of Object.entries(REFS)) {
    const [table, column] = ref.split('.');
    const { rows: [{ n }] } = await client.query(
      `SELECT count(*)::int AS n FROM public."${table}" c
        WHERE c."${column}" IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM public."${parent}" p WHERE p.id = c."${column}")`,
    );
    if (n > 0) { fail(`${ref} -> ${parent}: ${n} dangling`); dangling++; }
  }
  for (const [table, idCol, typeCol, map] of POLY) {
    for (const [typeValue, parent] of Object.entries(map)) {
      const { rows: [{ n }] } = await client.query(
        `SELECT count(*)::int AS n FROM public."${table}" c
          WHERE c."${typeCol}" = $1 AND c."${idCol}" IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM public."${parent}" p WHERE p.id = c."${idCol}")`,
        [typeValue],
      );
      if (n > 0) { fail(`${table}.${idCol} (${typeCol}=${typeValue}) -> ${parent}: ${n} dangling`); dangling++; }
    }
  }
  if (!dangling) ok(`all ${Object.keys(REFS).length} references + ${POLY.length} polymorphic references resolve`);

  // 4. the search-demo property
  console.log('\n4. search demo property');
  const { rows: [d] } = await client.query(`
    SELECT count(*)::int AS total,
           count(*) FILTER (WHERE technologies @> ARRAY['SAP','Snowflake','AWS']::varchar[]
                              AND industry = 'Manufacturing')::int AS all_mode,
           count(*) FILTER (WHERE technologies && ARRAY['SAP','Snowflake','AWS']::varchar[])::int AS any_mode,
           count(*) FILTER (WHERE name = 'Sapient Consulting Group'
                              AND NOT technologies @> ARRAY['SAP']::varchar[]
                              AND lower(array_to_string(technologies, ',')) LIKE '%sap%')::int AS near_miss
      FROM lead_company`);
  d.total >= 295 && d.total <= 310 ? ok(`lead_company total = ${d.total}`) : fail(`lead_company total = ${d.total}, want ~301`);
  d.all_mode >= 12 && d.all_mode <= 15 ? ok(`ALL(SAP+Snowflake+AWS) = ${d.all_mode}`) : fail(`ALL = ${d.all_mode}, want 12-15`);
  d.any_mode >= 180 && d.any_mode <= 220 ? ok(`ANY(SAP|Snowflake|AWS) = ${d.any_mode}`) : fail(`ANY = ${d.any_mode}, want ~200`);
  d.near_miss === 1 ? ok('"Sapient Consulting Group" near-miss present (matches %sap% without SAP)') : fail('near-miss row missing');

  // The provenance chain: a company's scraped_technologies must be traceable to a job post.
  const { rows: [{ broken }] } = await client.query(`
    SELECT count(*)::int AS broken FROM lead_company c
     WHERE cardinality(c.scraped_technologies) > 0
       AND NOT EXISTS (
         SELECT 1 FROM lead_company_job j
          WHERE j.lead_company_id = c.id AND j.technologies && c.scraped_technologies
            AND j.description ILIKE '%' || j.technologies[1] || '%')`);
  broken === 0 ? ok('every scraped_technologies roll-up is backed by a job posting that names the tech')
    : fail(`${broken} companies have scraped_technologies with no matching job posting`);

  await client.end();
  console.log(failures.length ? `\n${failures.length} FAILURE(S)\n` : '\nall checks passed\n');
  process.exit(failures.length ? 1 : 0);
};

run().catch((e) => { console.error(e); process.exit(1); });
