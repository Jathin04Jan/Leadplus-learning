/**
 * Tenancy + identity: tenant, workspace, users, mailboxes, CRM mirror tables, territory,
 * announcements and the admin/audit tables that hang off them.
 *
 * Everything else in the seed roots here — a lead, campaign or quote with a tenant_id that
 * points nowhere is exactly the kind of garbage the (FK-less) schema won't catch for us.
 */
import * as E from '../enums.js';
import * as C from '../catalog.js';
import { json, enumList, recipients, isoLocal } from '../format.js';
import { int, pick, chance, pickSome, pickBetween, sparse, between, after, plusDays } from '../rng.js';

const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '');

// ---- tenant --------------------------------------------------------------------------

const mailboxConfig = (type, i) => ({
  azureId: type === 'OUTLOOK' ? `azure-${int(100000, 999999)}` : null,
  googleId: type === 'GMAIL' ? `google-${int(100000, 999999)}` : null,
  googleLabelId: type === 'GMAIL' ? `Label_${int(10, 99)}` : null,
  refreshToken: type === 'SMTP' ? null : `rt_${slugify(type)}_${int(10000, 99999)}`,
  connectedAt: isoLocal(between(300, 30)),
  smtpAppPassword: type === 'SMTP' ? `app-${int(1000, 9999)}-${int(1000, 9999)}` : null,
  awsSESVerificationState: type === 'SES' ? pick(E.AwsSESVerificationState) : null,
});

export const buildTenants = () =>
  C.TENANTS.map((t, i) => {
    const createdAt = between(900, 400);
    const crm = i % 3; // 0 = hubspot, 1 = zoho, 2 = neither — not every tenant syncs a CRM
    const announcementType = pick(E.MailBoxType);

    return {
      name: t.name,
      domain: t.domain,
      // EnumListConverter, not JSON. Tenant 0 runs everything; others buy fewer modules.
      modules: enumList(i === 0 ? E.Module : pickBetween(E.Module, 2, 3)),
      plan_tier: i === 0 ? 'ENTERPRISE' : pick(E.PlanTier),
      owner_id: null, // set once tenant_user rows exist
      announcement_type: announcementType,
      announcement_from_email: `announcements@${t.domain}`,
      announcement_sender_name: `${t.name} Team`,
      announcement_meta_data: json(mailboxConfig(announcementType, i)),
      profile_context: `${t.name} sells into US manufacturing and industrial accounts. ` +
        `Our buyers are operations, engineering and IT leaders modernizing plant systems. ` +
        `We lead with ${pick(C.KEYWORDS)} and proof from comparable-size manufacturers.`,
      mailgun_domain: sparse(i, 0.5, `mg.${t.domain}`),
      cc_recipients: recipients([{ email: `gtm@${t.domain}`, name: 'GTM Team' }]),
      bcc_recipients: sparse(i, 0.5, recipients([{ email: `crm@${t.domain}`, name: 'CRM Archive' }])),
      hubspot_email: crm === 0 ? `ops@${t.domain}` : null,
      hubspot_refresh_token: crm === 0 ? `hs_rt_${int(100000, 999999)}` : null,
      hubspot_user_id: crm === 0 ? String(int(1000000, 9999999)) : null,
      hubspot_connected_at: crm === 0 ? between(300, 60) : null,
      zoho_email: crm === 1 ? `ops@${t.domain}` : null,
      zoho_refresh_token: crm === 1 ? `zh_rt_${int(100000, 999999)}` : null,
      zoho_user_id: crm === 1 ? String(int(1000000, 9999999)) : null,
      zoho_connected_at: crm === 1 ? between(300, 60) : null,
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

// ---- workspace -----------------------------------------------------------------------

const WORKSPACE_NAMES = ['Outbound', 'Enterprise Accounts', 'Field Sales', 'Partnerships'];

export const buildWorkspaces = (tenants, tenantIds) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    const n = ti === 0 ? 3 : 2; // 8 workspaces across 4 tenants
    for (let j = 0; j < n; j++) {
      const createdAt = after(tenants[ti].created_at, 60);
      rows.push({
        tenant_id: tenantId,
        name: `${WORKSPACE_NAMES[j % WORKSPACE_NAMES.length]}${j >= WORKSPACE_NAMES.length ? ` ${j}` : ''}`,
        daily_send_limit: pick([100, 200, 250, 400, 500]),
        owner_id: null, // set once users exist
        cc_recipients: sparse(rows.length, 0.5, recipients([{ email: `cc@${tenants[ti].domain}`, name: 'Shared Inbox' }])),
        bcc_recipients: sparse(rows.length, 0.5, recipients([{ email: `bcc@${tenants[ti].domain}`, name: 'Archive' }])),
        created_by: null,
        updated_by: null,
        created_at: createdAt,
        updated_at: after(createdAt, 120),
      });
    }
  });
  return rows;
};

// ---- tenant_user ---------------------------------------------------------------------

/** bcrypt hash of "demo-password-not-a-secret" — a real-looking hash, no live credential. */
const DEMO_HASH = '$2a$10$N9qo8uLOickgx2ZMRZoMyeIjZAgcfl7p92ldGxad68LJZdL17lhWy';

export const buildUsers = (tenants, tenantIds, workspaces, workspaceIds) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    const tenant = tenants[ti];
    const wsForTenant = workspaceIds.filter((_, wi) => workspaces[wi].tenant_id === tenantId);
    const n = 10; // ~40 users total

    for (let j = 0; j < n; j++) {
      const first = pick(C.FIRST_NAMES);
      const last = pick(C.LAST_NAMES);
      const isOwner = j === 0;
      const isAdmin = j === 1;
      const createdAt = after(tenant.created_at, 120);
      const draft = !isOwner && chance(0.1);
      const verified = isOwner || isAdmin || chance(0.8);

      rows.push({
        tenant_id: tenantId,
        workspace_id: pick(wsForTenant),
        name: `${first} ${last}`,
        email: `${first}.${last}${j}`.toLowerCase() + `@${tenant.domain}`,
        password: DEMO_HASH,
        company: tenant.name,
        // EnumListConverter, not JSON.
        roles: enumList(isOwner ? ['TENANT_OWNER', 'ADMIN'] : isAdmin ? ['ADMIN', 'USER'] : ['USER']),
        status: isOwner || isAdmin ? 'APPROVED' : pick(E.UserStatus),
        active: !draft,
        draft,
        email_verified: verified,
        email_verification_token: verified ? null : `evt_${int(100000, 999999)}`,
        verification_token: sparse(rows.length, 0.4, `vt_${int(1000000, 9999999)}`),
        // IdentityProviderListConverter -> JSON [{"type":..,"personId":..}]
        identity_providers: sparse(
          rows.length, 0.6,
          json([{ type: pick(E.IdentityProviderType), personId: String(int(100000000, 999999999)) }]),
        ),
        phone_number: sparse(rows.length, 0.7, `+1${int(200, 989)}${int(200, 999)}${String(int(0, 9999)).padStart(4, '0')}`),
        last_login_at: sparse(rows.length, 0.85, between(45, 0)),
        created_at: createdAt,
        updated_at: after(createdAt, 90),
      });
    }
  });
  return rows;
};

// ---- workspace_user ------------------------------------------------------------------

export const buildWorkspaceUsers = (users, userIds, workspaces, workspaceIds) => {
  const rows = [];
  users.forEach((user, ui) => {
    // Each user belongs to their home workspace; some also get a second one.
    const home = user.workspace_id;
    const others = workspaceIds.filter((id, wi) => workspaces[wi].tenant_id === user.tenant_id && id !== home);
    const targets = [home, ...(chance(0.35) && others.length ? [pick(others)] : [])];

    targets.forEach((wsId, k) => {
      const status = pick(E.WorkspaceUserStatus);
      rows.push({
        tenant_id: user.tenant_id,
        workspace_id: wsId,
        user_id: userIds[ui],
        workspace_user_role: k > 0 ? 'MEMBER' : user.roles.includes('TENANT_OWNER') ? 'OWNER' : pick(E.WorkspaceUserRole),
        workspace_user_status: k > 0 ? status : 'ACCEPTED',
        invitation_token: sparse(rows.length, 0.5, `inv_${int(1000000, 9999999)}`),
        active: status !== 'REVOKED',
        created_by: userIds[0],
        updated_by: sparse(rows.length, 0.6, userIds[0]),
        created_at: user.created_at,
        updated_at: after(user.created_at, 60),
      });
    });
  });
  return rows;
};

// ---- mailbox -------------------------------------------------------------------------

export const buildMailboxes = (users, userIds, workspaces, workspaceIds) => {
  const rows = [];
  workspaceIds.forEach((wsId, wi) => {
    const ws = workspaces[wi];
    const owners = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.workspace_id === wsId);
    if (!owners.length) return;

    const n = int(1, 2); // ~10 mailboxes across 8 workspaces
    for (let j = 0; j < n; j++) {
      const { u, id } = pick(owners);
      const type = pick(E.MailBoxType);
      const createdAt = after(ws.created_at, 60);
      const expired = chance(0.15);

      rows.push({
        workspace_id: wsId,
        user_id: id,
        type,
        email_address: u.email,
        meta_data: json(mailboxConfig(type, rows.length)),
        emails_sent_today: expired ? 0 : int(0, 120),
        token_expired: expired,
        active: !expired,
        created_at: createdAt,
        updated_at: after(createdAt, 30),
      });
    }
  });
  return rows;
};

// ---- refresh_token -------------------------------------------------------------------

export const buildRefreshTokens = (users, userIds) =>
  users.map((user, i) => {
    const issued = user.last_login_at ?? between(60, 1);
    const revoked = chance(0.2);
    return {
      user_id: userIds[i],
      // Opaque random handle — the real column holds a rotating server-issued token, and
      // nothing about this demo should look like a usable credential.
      token: `rft_${int(1000000000, 9999999999)}${int(1000000000, 9999999999)}`,
      issued_at: issued,
      expires_at: plusDays(issued, 30),
      last_used_at: sparse(i, 0.8, after(issued, 20)),
      refresh_count: int(0, 40),
      revoked,
    };
  });

// ---- tenant_company / tenant_contact (CRM mirror) ------------------------------------
// These mirror records synced FROM the tenant's CRM, so only CRM-connected tenants have
// them and source_type is the CRM enum.

export const buildTenantCompanies = (tenants, tenantIds, companies) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    const crm = tenants[ti].hubspot_email ? 'HUBSPOT' : tenants[ti].zoho_email ? 'ZOHO' : null;
    if (!crm) return;
    const mine = companies.filter((c) => c.tenant_id === tenantId).slice(0, 6);
    mine.forEach((c, j) => {
      const createdAt = after(tenants[ti].created_at, 200);
      rows.push({
        tenant_id: tenantId,
        name: c.name,
        source_type: crm,
        source_id: `${crm.toLowerCase()}_${int(1000000, 9999999)}`,
        created_at: createdAt,
        updated_at: after(createdAt, 60),
      });
    });
  });
  return rows;
};

export const buildTenantContacts = (tenants, tenantIds, tenantCompanies, tenantCompanyIds) => {
  const rows = [];
  tenantCompanies.forEach((tc, i) => {
    const n = int(1, 3);
    for (let j = 0; j < n; j++) {
      const first = pick(C.FIRST_NAMES);
      const last = pick(C.LAST_NAMES);
      const createdAt = after(tc.created_at, 60);
      rows.push({
        tenant_id: tc.tenant_id,
        company_id: tc.source_id, // CRM-side id, mirrors tenant_company.source_id
        first_name: first,
        last_name: last,
        email: `${first}.${last}`.toLowerCase() + `@${slugify(tc.name)}.com`,
        source_type: tc.source_type,
        source_id: `${tc.source_type.toLowerCase()}_c_${int(1000000, 9999999)}`,
        created_at: createdAt,
        updated_at: after(createdAt, 30),
      });
    }
  });
  return rows;
};

// ---- tenant_contact_metadata ---------------------------------------------------------
// Limark's BD/ISR routing overlay. UNIQUE (tenant_id, lead_contact_id), so one row per
// contact at most — take a subset.

const PRIORITIES = ['P1', 'P2', 'P3'];
const TITLE_CATEGORIES = ['Decision Maker', 'Technical Buyer', 'Influencer', 'Operations'];

export const buildContactMetadata = (contacts, contactIds, reps, repIds, count) => {
  const rows = [];
  const seen = new Set();
  for (let i = 0; i < contacts.length && rows.length < count; i++) {
    if (!chance(0.35)) continue;
    const key = `${contacts[i].tenant_id}:${contactIds[i]}`;
    if (seen.has(key)) continue;
    seen.add(key);

    const tenantReps = reps.map((r, ri) => ({ r, id: repIds[ri] })).filter(({ r }) => r.tenant_id === contacts[i].tenant_id);
    if (!tenantReps.length) continue;
    const bd = pick(tenantReps);
    const isr = pick(tenantReps);
    const createdAt = after(contacts[i].created_at, 60);

    rows.push({
      tenant_id: contacts[i].tenant_id,
      lead_contact_id: contactIds[i],
      bd_name: bd.r.name,
      bd_email: bd.r.email,
      bd_phone: sparse(rows.length, 0.8, bd.r.phone),
      isr_name: sparse(rows.length, 0.7, isr.r.name),
      isr_email: sparse(rows.length, 0.7, isr.r.email),
      isr_phone: sparse(rows.length, 0.6, isr.r.phone),
      priority: sparse(rows.length, 0.8, pick(PRIORITIES)),
      title_category: sparse(rows.length, 0.75, pick(TITLE_CATEGORIES)),
      created_at: createdAt,
      updated_at: after(createdAt, 30),
    });
  }
  return rows;
};

// ---- tenant_data_source --------------------------------------------------------------
// Import provenance. UNIQUE (tenant_id, target_entity_type, target_entity_id, source,
// source_label) — dedupe on exactly that key.

export const buildDataSources = (companies, companyIds, contacts, contactIds, imports, importIds, users, userIds) => {
  const rows = [];
  const seen = new Set();
  const add = (tenantId, entityType, entityId, i) => {
    const source = chance(0.7) ? 'APOLLO' : 'MANUAL';
    const label = source === 'APOLLO' ? pick(['apollo-people-search', 'apollo-enrichment']) : pick(['csv-upload', 'manual-entry']);
    const key = `${tenantId}|${entityType}|${entityId}|${source}|${label}`;
    if (seen.has(key)) return;
    seen.add(key);

    const batch = imports.length ? int(0, imports.length - 1) : null;
    rows.push({
      tenant_id: tenantId,
      target_entity_type: entityType,
      target_entity_id: entityId,
      source,
      source_label: label,
      import_batch_id: source === 'MANUAL' && batch !== null ? importIds[batch] : null,
      created_by_id: sparse(rows.length, 0.8, pick(userIds)),
      created_at: between(300, 5),
    });
  };

  companies.forEach((c, i) => chance(0.4) && add(c.tenant_id, 'LEAD_COMPANY', companyIds[i], i));
  contacts.forEach((c, i) => chance(0.15) && add(c.tenant_id, 'LEAD_CONTACT', contactIds[i], i));
  return rows;
};

// ---- tenant_lead_filter --------------------------------------------------------------
// A tenant's saved default filter. employee_ranges is EnumListConverter ("{RANGE_0_500}"),
// every other list here is a native varchar[].

export const buildTenantLeadFilters = (tenantIds, userIds) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    E.LeadType.forEach((type, k) => {
      const i = rows.length;
      const createdAt = between(300, 60);
      rows.push({
        tenant_id: tenantId,
        type,
        cities: sparse(i, 0.7, pickBetween(C.LOCATIONS.map((l) => l[1]), 1, 3)),
        company_cities: sparse(i, 0.65, pickBetween(C.LOCATIONS.map((l) => l[1]), 1, 3)),
        company_countries: sparse(i, 0.75, ['United States']),
        company_names: sparse(i, 0.6, []),
        company_states: sparse(i, 0.7, pickBetween(C.LOCATIONS.map((l) => l[0]), 1, 4)),
        countries: sparse(i, 0.75, ['United States']),
        departments: sparse(i, 0.65, pickBetween(C.DEPARTMENTS, 1, 3)),
        industries: sparse(i, 0.85, ['Manufacturing', ...pickBetween(C.INDUSTRY_NAMES, 0, 2)]),
        employee_ranges: enumList(pickBetween(['RANGE_501_1000', 'RANGE_1001_5000', 'RANGE_5001_10000'], 1, 2)),
        keywords: sparse(i, 0.75, pickBetween(C.KEYWORDS, 1, 3)),
        naics_codes: sparse(i, 0.6, pickSome(C.NAICS, 1)),
        postal_codes: sparse(i, 0.55, []),
        regions: sparse(i, 0.65, pickBetween(C.REGIONS.slice(0, 4), 1, 2)),
        revenue_ranges: sparse(i, 0.6, pickBetween(C.REVENUE_RANGES, 1, 2)),
        seniority: sparse(i, 0.75, pickBetween(C.SENIORITIES, 1, 3)),
        sic_codes: sparse(i, 0.6, pickSome(C.SIC, 1)),
        states: sparse(i, 0.7, pickBetween(C.LOCATIONS.map((l) => l[0]), 1, 4)),
        technologies: sparse(i, 0.8, ['AWS', ...pickBetween(['SAP', 'Snowflake', 'Salesforce'], 1, 2)]),
        titles: sparse(i, 0.6, pickBetween(C.TITLES.map((t) => t[0]), 1, 3)),
        active: chance(0.9),
        created_by: pick(userIds),
        updated_by: sparse(i, 0.6, pick(userIds)),
        created_at: createdAt,
        updated_at: after(createdAt, 90),
      });
    });
  });
  return rows;
};

// ---- tenant_announcement / _contact --------------------------------------------------

const ANNOUNCEMENTS = [
  ['Q3 capacity update', 'Our Q3 lead times have improved across all product lines.'],
  ['New plant automation offering', 'We have launched a new offering for plant automation retrofits.'],
  ['Holiday shipping schedule', 'Please note the adjusted shipping schedule for the holidays.'],
  ['Certification renewal: AS9100D', 'We have renewed our AS9100D certification for another cycle.'],
  ['Trade show: IMTS booth', 'Come find us at IMTS — booth details and scheduling link inside.'],
];

export const buildAnnouncements = (tenants, tenantIds, users, userIds) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    const tenantUsers = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === tenantId);
    ANNOUNCEMENTS.slice(0, ti === 0 ? 5 : 3).forEach(([name, body], j) => {
      const i = rows.length;
      const status = pick(E.TenantAnnouncementStatus);
      const createdAt = between(200, 10);
      rows.push({
        tenant_id: tenantId,
        name,
        subject: `${tenants[ti].name}: ${name}`,
        body: `<p>Hi {{first_name}},</p><p>${body}</p><p>— The ${tenants[ti].name} Team</p>`,
        status,
        launched_at: status === 'DRAFT' ? null : after(createdAt, 20),
        cc_recipients: sparse(i, 0.5, recipients([{ email: `gtm@${tenants[ti].domain}`, name: 'GTM Team' }])),
        bcc_recipients: sparse(i, 0.4, recipients([{ email: `archive@${tenants[ti].domain}`, name: 'Archive' }])),
        attachment_ids: sparse(i, 0.4, [String(int(1, 40))]),
        active: chance(0.9),
        created_by: pick(tenantUsers).id,
        updated_by: sparse(i, 0.7, pick(tenantUsers).id),
        created_at: createdAt,
        updated_at: after(createdAt, 30),
      });
    });
  });
  return rows;
};

export const buildAnnouncementContacts = (announcements, announcementIds, contacts, contactIds, tenantContacts, tenantContactIds, users, userIds) => {
  const rows = [];
  announcements.forEach((a, ai) => {
    if (a.status === 'DRAFT') return; // a draft has not been fanned out to recipients yet
    const n = int(4, 12);
    for (let j = 0; j < n; j++) {
      const i = rows.length;
      const fromLead = chance(0.7);
      let source, sourceId, email, firstName;

      if (fromLead) {
        const k = int(0, contacts.length - 1);
        source = 'LEAD';
        sourceId = contactIds[k];
        email = contacts[k].email;
        firstName = contacts[k].first_name;
      } else if (tenantContacts.length) {
        const k = int(0, tenantContacts.length - 1);
        source = 'CRM';
        sourceId = tenantContactIds[k];
        email = tenantContacts[k].email;
        firstName = tenantContacts[k].first_name;
      } else {
        const k = int(0, contacts.length - 1);
        source = 'LEAD';
        sourceId = contactIds[k];
        email = contacts[k].email;
        firstName = contacts[k].first_name;
      }

      const status = a.status === 'COMPLETED' ? pick(['SENT', 'SENT', 'SENT', 'BOUNCED']) : pick(E.TenantAnnouncementContactStatus);
      rows.push({
        announcement_id: announcementIds[ai],
        source_type: source,
        source_id: sourceId,
        email,
        first_name: sparse(i, 0.9, firstName),
        status,
        sent_at: status === 'SENT' ? after(a.launched_at ?? a.created_at, 2) : null,
        created_by: sparse(i, 0.8, pick(userIds)),
        created_at: a.created_at,
      });
    }
  });
  return rows;
};

// ---- territory_rep / territory_state_assignment --------------------------------------

export const buildTerritoryReps = (tenants, tenantIds) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    const n = 2; // 8 reps across 4 tenants
    for (let j = 0; j < n; j++) {
      const first = pick(C.FIRST_NAMES);
      const last = pick(C.LAST_NAMES);
      const createdAt = between(400, 60);
      rows.push({
        tenant_id: tenantId,
        name: `${first} ${last}`,
        email: `${first}.${last}`.toLowerCase() + `@${tenants[ti].domain}`,
        phone: sparse(rows.length, 0.8, `+1${int(200, 989)}${int(200, 999)}${String(int(0, 9999)).padStart(4, '0')}`),
        created_at: createdAt,
        updated_at: after(createdAt, 60),
      });
    }
  });
  return rows;
};

/**
 * TerritoryService.assignLocation copies `region` and `location_type` verbatim off the
 * matched timezone_mapping row and lowercases location_key — so these are derived from the
 * timezone rows, never chosen independently. UNIQUE (tenant_id, location_key).
 */
export const buildTerritoryAssignments = (tenantIds, timezones, reps, repIds) => {
  const rows = [];
  const states = timezones.filter((t) => t.location_type === 'STATE' && t.region);
  tenantIds.forEach((tenantId) => {
    const tenantReps = repIds.filter((_, ri) => reps[ri].tenant_id === tenantId);
    const assigned = pickSome(states, 10); // ~40 assignments across 4 tenants
    assigned.forEach((tz) => {
      const i = rows.length;
      rows.push({
        tenant_id: tenantId,
        region: tz.region,
        location_key: tz.location_key, // already lowercased by the timezone builder
        location_type: tz.location_type,
        // rep_id is nullable and ON DELETE SET NULL — unassigned locations are a real state.
        rep_id: sparse(i, 0.85, pick(tenantReps)),
        created_at: between(300, 30),
        updated_at: between(30, 0),
      });
    });
  });
  return rows;
};

// ---- timezone_mapping ----------------------------------------------------------------
// No bootstrap/seed for this table exists in the repo (ddl-auto: validate, no Flyway), so
// in prod it is populated out-of-band. location_key is stored lowercased (TerritoryService),
// timezone is an IANA id, and region reuses the RegionMapping vocabulary — the only region
// convention the codebase has.

export const buildTimezones = () => {
  const rows = [];
  C.STATE_TIMEZONES.forEach(([state, tz], i) => {
    rows.push({
      location_key: state.toLowerCase(),
      location_type: 'STATE',
      timezone: tz,
      region: C.regionFor(state, null, null),
      active: true,
      created_at: between(600, 500),
      updated_at: between(200, 100),
    });
  });
  C.COUNTRY_TIMEZONES.forEach(([country, tz]) => {
    rows.push({
      location_key: country.toLowerCase(),
      location_type: 'COUNTRY',
      timezone: tz,
      region: C.regionFor(null, null, country),
      active: true,
      created_at: between(600, 500),
      updated_at: between(200, 100),
    });
  });
  return rows;
};

// ---- user_activity_log ---------------------------------------------------------------

const ACTIVITY_COPY = {
  EMAIL_SENT: (u) => `${u.name} sent an outreach email.`,
  EMAIL_OPENED: () => 'A tracked email was opened by the recipient.',
  CAMPAIGN: (u) => `${u.name} changed the status of a campaign.`,
  CALL: (u) => `${u.name} logged a call with a contact.`,
  NOTE: (u) => `${u.name} added a note to a lead record.`,
  MEETING: (u) => `${u.name} booked a meeting from a reply.`,
};

export const buildActivityLogs = (users, userIds, workspaces, workspaceIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ui = int(0, users.length - 1);
    const user = users[ui];
    const type = pick(E.UserActivityLogType);
    const wsForTenant = workspaceIds.filter((_, wi) => workspaces[wi].tenant_id === user.tenant_id);

    rows.push({
      tenant_id: user.tenant_id,
      workspace_id: sparse(i, 0.9, pick(wsForTenant)),
      user_id: userIds[ui],
      username: sparse(i, 0.95, user.email),
      type,
      // Real logs skew INFO; the occasional WARNING/ERROR is what makes a log look real.
      log_level: chance(0.8) ? 'INFO' : pick(E.LogLevel),
      message: sparse(i, 0.9, ACTIVITY_COPY[type](user)),
      created_by: userIds[ui],
      created_at: between(180, 0),
    });
  }
  return rows;
};

// ---- feedback ------------------------------------------------------------------------

const FEEDBACK_COPY = {
  FEATURE_REQUEST: 'Please add the ability to export a filtered lead list straight to CSV.',
  BUG_REPORT: 'The keyword filter returns companies that do not have the technology when I pick ALL mode.',
  IMPROVEMENT: 'The search results table would be easier to scan if technographics were a column.',
  GENERAL: 'The campaign copilot has saved our team a lot of time this quarter.',
};

export const buildFeedback = (users, userIds, workspaces, workspaceIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ui = int(0, users.length - 1);
    const user = users[ui];
    const type = pick(E.FeedbackType);
    const createdAt = between(200, 1);
    const wsForTenant = workspaceIds.filter((_, wi) => workspaces[wi].tenant_id === user.tenant_id);

    rows.push({
      tenant_id: user.tenant_id,
      workspace_id: sparse(i, 0.85, pick(wsForTenant)),
      user_id: userIds[ui],
      username: sparse(i, 0.9, user.email),
      company_name: sparse(i, 0.8, user.company),
      type,
      status: pick(E.FeedbackStatus),
      // Bug reports skew low, praise skews high — flat random ratings look synthetic.
      rating: sparse(i, 0.8, type === 'BUG_REPORT' ? int(1, 3) : int(3, 5)),
      message: FEEDBACK_COPY[type],
      feedback_date: createdAt,
      created_by: userIds[ui],
      updated_by: sparse(i, 0.5, userIds[ui]),
      created_at: createdAt,
      updated_at: after(createdAt, 20),
    });
  }
  return rows;
};

// ---- agreement -----------------------------------------------------------------------
// Versioned legal text: exactly one `latest` per type.

export const buildAgreements = (userIds) => {
  const rows = [];
  E.AgreementType.forEach((type) => {
    const versions = 3;
    for (let v = 1; v <= versions; v++) {
      rows.push({
        agreement_type: type,
        version: v,
        latest: v === versions,
        content: `https://legal.leadplus.ai/${type.toLowerCase().replace(/_/g, '-')}/v${v}`,
        created_by: pick(userIds),
        created_at: between(900 - v * 200, 800 - v * 200),
      });
    }
  });
  return rows;
};
