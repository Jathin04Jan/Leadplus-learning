/**
 * Standalone search demo — full synthetic replica of the LeadPlus production database.
 *
 * Populates all 70 tables with domain-coherent US manufacturing / industrial GTM data so
 * the demo DB looks and behaves like a real copy: every table has rows, no column is
 * entirely NULL, and every *_id resolves to a row that exists.
 *
 * Three properties this file is responsible for:
 *
 *   DETERMINISM   — one fixed-seed LCG (seed/rng.js) drives every choice, so re-seeding
 *                   reproduces the identical database. Never Math.random().
 *   IDEMPOTENCE   — TRUNCATE ... RESTART IDENTITY across all 70 tables first, so
 *                   `npm run seed` is repeatable rather than additive.
 *   INTEGRITY     — the schema has ZERO foreign keys, so nothing here is checked by the
 *                   database. Build order below IS the dependency graph: each builder is
 *                   handed the real ids of its parents. Reordering it silently produces
 *                   dangling references.
 *
 * The demo property (AWS everywhere -> ANY floods; a 12-row golden set -> ALL is precise;
 * the "Sapient Consulting Group" near-miss) lives in seed/tables/leads.js.
 *
 * Usage: node seed.js   (or `npm run reset` for setup + seed)
 */
import pg from 'pg';
import { config } from './config.js';
import { insertMany, truncateAll } from './seed/db.js';
import { reseed, pick, chance } from './seed/rng.js';
import * as CAT from './seed/catalog.js';
import * as W from './seed/tables/workspace.js';
import * as L from './seed/tables/leads.js';
import * as CP from './seed/tables/campaign.js';
import * as P from './seed/tables/portal.js';
import * as A from './seed/tables/admin.js';

const counts = {};
const step = async (client, table, rows) => {
  const ids = await insertMany(client, table, rows);
  counts[table] = ids.length;
  return ids;
};

const run = async () => {
  const client = new pg.Client({ connectionString: config.databaseUrl });
  await client.connect();
  reseed(1337);

  const tables = await truncateAll(client);
  console.log(`truncated ${tables} tables\n`);

  // ---- 1. tenancy & identity ---------------------------------------------------------
  const tenants = W.buildTenants();
  const tenantIds = await step(client, 'tenant', tenants);

  const workspaces = W.buildWorkspaces(tenants, tenantIds);
  const workspaceIds = await step(client, 'workspace', workspaces);

  const users = W.buildUsers(tenants, tenantIds, workspaces, workspaceIds);
  const userIds = await step(client, 'tenant_user', users);
  // Give the in-memory rows their real ids — downstream builders filter on them.
  users.forEach((u, i) => { u.id = userIds[i]; });
  workspaces.forEach((w, i) => { w.id = workspaceIds[i]; });

  // tenant.owner_id / workspace.owner_id are circular with tenant_user (a tenant's owner is
  // one of its users), so they're backfilled once the users exist rather than guessed.
  for (let i = 0; i < tenantIds.length; i++) {
    const owner = users.find((u) => u.tenant_id === tenantIds[i] && u.roles.includes('TENANT_OWNER'));
    await client.query('UPDATE tenant SET owner_id = $1 WHERE id = $2', [owner.id, tenantIds[i]]);
  }
  for (let i = 0; i < workspaceIds.length; i++) {
    const ws = workspaces[i];
    const tenantUsers = users.filter((u) => u.tenant_id === ws.tenant_id);
    const owner = tenantUsers.find((u) => u.roles.includes('TENANT_OWNER')) ?? tenantUsers[0];
    const editor = pick(tenantUsers);
    await client.query(
      'UPDATE workspace SET owner_id = $1, created_by = $2, updated_by = $3 WHERE id = $4',
      [owner.id, owner.id, chance(0.7) ? editor.id : null, workspaceIds[i]],
    );
  }

  await step(client, 'workspace_user', W.buildWorkspaceUsers(users, userIds, workspaces, workspaceIds));

  const mailboxes = W.buildMailboxes(users, userIds, workspaces, workspaceIds);
  const mailboxIds = await step(client, 'mailbox', mailboxes);

  await step(client, 'refresh_token', W.buildRefreshTokens(users, userIds));

  // ---- 2. territory (timezone_mapping feeds the assignments) -------------------------
  const timezones = W.buildTimezones();
  await step(client, 'timezone_mapping', timezones);

  const reps = W.buildTerritoryReps(tenants, tenantIds);
  const repIds = await step(client, 'territory_rep', reps);

  await step(client, 'territory_state_assignment', W.buildTerritoryAssignments(tenantIds, timezones, reps, repIds));

  // ---- 3. marketplace catalogs -------------------------------------------------------
  const industries = P.buildIndustries(userIds);
  const industryIds = await step(client, 'industry', industries);

  const serviceCategories = P.buildServiceCategories(userIds);
  const serviceCategoryIds = await step(client, 'service_category', serviceCategories);

  const services = P.buildServices(serviceCategoryIds, userIds);
  const serviceIds = await step(client, 'service', services);

  await step(client, 'industry_service_mapping', P.buildIndustryServiceMappings(industryIds, serviceIds));

  const specCategories = P.buildSpecificationCategories(userIds);
  const specCategoryIds = await step(client, 'specification_category', specCategories);

  const specifications = P.buildSpecifications(CAT.SPECIFICATION_CATEGORIES, specCategoryIds, userIds);
  const specificationIds = await step(client, 'specification', specifications);

  await step(client, 'service_specification', P.buildServiceSpecifications(serviceIds, specificationIds));

  const sections = P.buildQuestionSections(userIds);
  const sectionIds = await step(client, 'question_section', sections);

  const questions = P.buildQuestions(sectionIds, industryIds, userIds);
  const questionIds = await step(client, 'question', questions);

  // ---- 4. vendors --------------------------------------------------------------------
  const vendors = P.buildVendors(tenantIds, users, userIds, industryIds, serviceIds, specificationIds, questions, questionIds);
  const vendorIds = await step(client, 'vendor', vendors);

  await step(client, 'vendor_agreement', P.buildVendorAgreements(vendors, vendorIds));

  const showcases = P.buildVendorShowcases(vendors, vendorIds, serviceIds, userIds);
  const showcaseIds = await step(client, 'vendor_showcase', showcases);

  const dataPacks = P.buildLeadDataPacks(industryIds, userIds);
  const dataPackIds = await step(client, 'lead_data_pack', dataPacks);

  await step(client, 'vendor_data_pack', P.buildVendorDataPacks(vendors, vendorIds, dataPackIds, users, userIds));

  // ---- 5. the lead pool (the demo property lives here) --------------------------------
  const companies = L.buildCompanies(tenantIds, users);
  const companyIds = await step(client, 'lead_company', companies);

  const jobs = L.buildJobs(companies, companyIds);
  const jobIds = await step(client, 'lead_company_job', jobs);

  await step(client, 'lead_company_event', L.buildCompanyEvents(companies, companyIds, 500));

  const contacts = L.buildContacts(companies, companyIds, users);
  const contactIds = await step(client, 'lead_contact', contacts);

  await step(client, 'lead_contact_normalized_title', L.buildNormalizedTitles(contacts, contactIds));
  await step(client, 'lead_contact_event', L.buildContactEvents(contacts, contactIds, users, workspaces, 500));
  await step(client, 'lead_company_revision', L.buildCompanyRevisions(companies, companyIds, users, 80));
  await step(client, 'lead_contact_revision', L.buildContactRevisions(contacts, contactIds, users, 80));

  // ---- 6. tenant-side lead overlays --------------------------------------------------
  const tenantCompanies = W.buildTenantCompanies(tenants, tenantIds, companies);
  const tenantCompanyIds = await step(client, 'tenant_company', tenantCompanies);

  const tenantContacts = W.buildTenantContacts(tenants, tenantIds, tenantCompanies, tenantCompanyIds);
  const tenantContactIds = await step(client, 'tenant_contact', tenantContacts);

  await step(client, 'tenant_contact_metadata', W.buildContactMetadata(contacts, contactIds, reps, repIds, 400));
  await step(client, 'tenant_lead_filter', W.buildTenantLeadFilters(tenantIds, userIds));

  // ---- 7. imports (tenant_data_source cites the import batch) -------------------------
  const imports = A.buildImports(tenantIds, users, userIds, 12);
  const importIds = await step(client, 'lead_file_import', imports);

  await step(client, 'lead_file_import_record', A.buildImportRecords(imports, importIds, companies, companyIds, contacts, contactIds, 240));
  await step(client, 'tenant_data_source', W.buildDataSources(companies, companyIds, contacts, contactIds, imports, importIds, users, userIds));

  // ---- 8. sourcing requests, quotes, attachments -------------------------------------
  const rfqs = P.buildRfqs(users, userIds, serviceIds, vendorIds, 30);
  const rfqIds = await step(client, 'request_for_quote', rfqs);

  const rfps = P.buildRfps(users, userIds, serviceIds, specificationIds, 20);
  const rfpIds = await step(client, 'request_for_proposal', rfps);

  const quotations = P.buildQuotations(rfqs, rfqIds, rfps, rfpIds, vendors, vendorIds, workspaces, workspaceIds, userIds, 40);
  const quotationIds = await step(client, 'quotation', quotations);

  await step(client, 'collaborator', P.buildCollaborators(rfqs, rfqIds, rfps, rfpIds, users, userIds));

  // attachment.source_type is the SourceType enum — cover all four values with real ids.
  const attachmentIds = await step(client, 'attachment', P.buildAttachments([
    { sourceType: 'REQUEST_FOR_QUOTE', ids: rfqIds, createdAts: rfqs.map((r) => r.created_at) },
    { sourceType: 'REQUEST_FOR_PROPOSAL', ids: rfpIds, createdAts: rfps.map((r) => r.created_at) },
    { sourceType: 'QUOTATION', ids: quotationIds, createdAts: quotations.map((q) => q.created_at) },
    { sourceType: 'SHOWCASE', ids: showcaseIds, createdAts: showcases.map((s) => s.created_at) },
  ], userIds));

  await step(client, 'attachment_library', P.buildAttachmentLibrary(workspaceIds, userIds));

  // ---- 9. campaigns & outreach -------------------------------------------------------
  const sequenceTemplates = CP.buildSequenceTemplates();
  const sequenceTemplateIds = await step(client, 'sequence_template', sequenceTemplates);
  await step(client, 'email_sequence_template', CP.buildEmailSequenceTemplates(tenants, tenantIds, userIds));

  const campaigns = CP.buildCampaigns(tenants, tenantIds, workspaces, workspaceIds, users, userIds, mailboxes, mailboxIds, companyIds, 20);
  campaigns.forEach((c, i) => { c.template_id = i % 5 === 0 ? null : pick(sequenceTemplateIds); });
  const campaignIds = await step(client, 'campaign', campaigns);

  await step(client, 'campaign_email', CP.buildCampaignEmails(campaigns, campaignIds, userIds, attachmentIds));

  const campaignContacts = CP.buildCampaignContacts(campaigns, campaignIds, contacts, contactIds, mailboxes, 500);
  await step(client, 'campaign_contact', campaignContacts);

  await step(client, 'contact_email', CP.buildContactEmails(campaigns, campaignIds, contacts, contactIds, users, userIds, attachmentIds, 800));
  await step(client, 'contact_outreach_status', CP.buildOutreachStatuses(campaignContacts, contacts, contactIds, 400));
  await step(client, 'campaign_chat_memory', CP.buildChatMemories(campaigns, campaignIds, userIds));
  await step(client, 'message', CP.buildMessages(campaigns, campaignIds, users, userIds, 200));

  // ---- 10. announcements -------------------------------------------------------------
  const announcements = W.buildAnnouncements(tenants, tenantIds, users, userIds);
  const announcementIds = await step(client, 'tenant_announcement', announcements);

  await step(client, 'tenant_announcement_contact', W.buildAnnouncementContacts(
    announcements, announcementIds, contacts, contactIds, tenantContacts, tenantContactIds, users, userIds,
  ));
  await step(client, 'email_image', P.buildEmailImages(campaignIds, announcementIds, userIds));

  // ---- 11. apollo + scraper provenance -----------------------------------------------
  const apolloSpecs = A.buildApolloSpecifications(userIds);
  const apolloSpecIds = await step(client, 'apollo_specification', apolloSpecs);
  await step(client, 'apollo_company_search_specification', A.buildApolloCompanySearchSpecifications(userIds));
  await step(client, 'apollo_company_data', A.buildApolloCompanyData(companies, companyIds, apolloSpecIds, 260));
  await step(client, 'apollo_contact_data', A.buildApolloContactData(contacts, contactIds, companies, apolloSpecIds, 700));
  await step(client, 'scrape_job', A.buildScrapeJobs(companies, companyIds, jobs, jobIds, 220));

  // ---- 12. saved lists, notes, search history ----------------------------------------
  await step(client, 'lead_list', A.buildLeadLists(tenants, tenantIds, workspaces, workspaceIds, users, userIds, companyIds, contactIds));
  await step(client, 'lead_note', A.buildLeadNotes(companies, companyIds, contacts, contactIds, workspaces, workspaceIds, users, userIds, 220));
  await step(client, 'lead_query', A.buildLeadQueries(companies, contacts));
  await step(client, 'lead_search_history', A.buildSearchHistories(users, userIds, 60));

  // ---- 13. admin & audit -------------------------------------------------------------
  await step(client, 'user_activity_log', W.buildActivityLogs(users, userIds, workspaces, workspaceIds, 300));
  await step(client, 'feedback', W.buildFeedback(users, userIds, workspaces, workspaceIds, 60));
  await step(client, 'agreement', W.buildAgreements(userIds));
  await step(client, 'prompt_specification', CP.buildPromptSpecifications(userIds));
  await step(client, 'fact', CP.buildFacts());

  // ---- report ------------------------------------------------------------------------
  await client.query('ANALYZE');

  const names = Object.keys(counts).sort();
  console.log(`seeded ${names.length} tables:\n`);
  for (const t of names) console.log(`  ${t.padEnd(42)} ${String(counts[t]).padStart(6)}`);

  const { rows: [demo] } = await client.query(`
    SELECT count(*) AS total,
           count(*) FILTER (WHERE industry = 'Manufacturing') AS manufacturing,
           count(*) FILTER (
             WHERE technologies @> ARRAY['SAP','Snowflake','AWS']::varchar[]
               AND industry = 'Manufacturing'
           ) AS all_sap_snow_aws,
           count(*) FILTER (
             WHERE technologies && ARRAY['SAP','Snowflake','AWS']::varchar[]
           ) AS any_sap_snow_aws,
           count(*) FILTER (
             WHERE lower(array_to_string(technologies, ',')) LIKE '%sap%'
           ) AS substring_sap
      FROM lead_company`);
  console.log('\nsearch demo property:');
  console.log(`  lead_company total .......................... ${demo.total}`);
  console.log(`  industry = Manufacturing .................... ${demo.manufacturing}`);
  console.log(`  ALL(SAP + Snowflake + AWS) + Manufacturing .. ${demo.all_sap_snow_aws}`);
  console.log(`  ANY(SAP | Snowflake | AWS) .................. ${demo.any_sap_snow_aws}`);
  console.log(`  substring '%sap%' (incl. the Sapient near-miss) ${demo.substring_sap}`);

  await client.end();
};

run().catch((e) => {
  console.error(e);
  process.exit(1);
});
