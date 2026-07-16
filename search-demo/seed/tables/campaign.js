/**
 * Campaigns, outreach and the AI/chat surface.
 *
 * The JSON blobs here are the fiddliest part of the replica: several `text` columns hold
 * Jackson-serialized DTOs whose exact field names the app reads back. Shapes are copied
 * from the converters in domain/common/ — see format.js for the three encodings.
 *
 * One trap worth naming: EmployeeRange serializes as its @JsonValue LABEL ("0-500") inside
 * these JSON blobs, but as its constant NAME ("RANGE_0_500") in @Enumerated columns and in
 * EnumListConverter columns. Same enum, two on-disk forms, depending on the path.
 */
import * as E from '../enums.js';
import * as C from '../catalog.js';
import { json, recipients, isoLocal } from '../format.js';
import { int, pick, chance, pickSome, pickBetween, sparse, between, after, plusDays, NOW } from '../rng.js';

const DAYS = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY'];

/** EmployeeRange @JsonValue labels — the JSON-side form. */
const RANGE_LABELS = ['0-500', '501-1000', '1001-5000', '5001-10000', '10001+'];

const locationFilter = (states) => ({
  cities: pickBetween(C.LOCATIONS.map((l) => l[1]), 0, 3),
  states,
  countries: ['United States'],
  regions: pickBetween(C.REGIONS.slice(0, 4), 0, 2),
});

/** TargetingCriteria (TargetingCriteriaConverter). Also used by message + chat memory. */
const targetingCriteria = (conversationId, companyIds, contactIds) => {
  const states = pickBetween(C.LOCATIONS.map((l) => l[0]), 1, 3);
  return {
    conversationId,
    searchQuery: pick([
      'manufacturing companies in the midwest running SAP',
      'plant operations leaders at mid-market manufacturers using AWS',
      'IT directors at industrial companies with Snowflake',
      'aerospace suppliers with AS9100 hiring automation engineers',
    ]),
    employeeRanges: pickBetween(RANGE_LABELS, 1, 2), // labels, not RANGE_* names
    companyLocationFilter: locationFilter(states),
    companyExcludeLocationFilter: { cities: [], states: [], countries: [], regions: [] },
    companyIds,
    contactLocationFilter: locationFilter(states),
    jobTitles: pickBetween(C.TITLES.map((t) => t[0]), 1, 4),
    contactIds,
    totalCompanies: companyIds.length,
    totalContacts: contactIds.length,
  };
};

/** LeadFilterCriteria (LeadFilterCriteriaConverter) — campaign.lead_filter. ~30 fields. */
const leadFilterCriteria = (companyIds, matchMode) => ({
  companyIds,
  contactNames: [],
  cities: pickBetween(C.LOCATIONS.map((l) => l[1]), 0, 2),
  states: pickBetween(C.LOCATIONS.map((l) => l[0]), 0, 3),
  countries: ['United States'],
  companyNames: [],
  companyCities: [],
  companyStates: pickBetween(C.LOCATIONS.map((l) => l[0]), 0, 3),
  companyCountries: ['United States'],
  regions: pickBetween(C.REGIONS.slice(0, 4), 0, 2),
  keywords: pickBetween(C.KEYWORDS, 0, 3),
  keywordMatchMode: matchMode,
  industries: ['Manufacturing'],
  employeeRanges: pickBetween(RANGE_LABELS, 1, 2),
  revenueRanges: pickBetween(C.REVENUE_RANGES, 0, 2),
  technologies: matchMode === 'ALL' ? ['SAP', 'Snowflake', 'AWS'] : pickBetween(C.ALL_TECH, 1, 3),
  toolsServices: pickBetween([...C.SCRAPED_TOOLS, ...C.SCRAPED_SERVICES], 0, 2),
  titles: pickBetween(C.TITLES.map((t) => t[0]), 0, 3),
  seniority: pickBetween(C.SENIORITIES, 0, 3),
  departments: pickBetween(C.DEPARTMENTS, 0, 2),
  postalCodes: [],
  sicCodes: pickSome(C.SIC, chance(0.3) ? 1 : 0),
  naicsCodes: pickSome(C.NAICS, chance(0.3) ? 1 : 0),
  contactSegments: pickBetween(C.SEGMENTS, 0, 2),
  bdNames: [],
  isrNames: [],
  priorities: [],
  titleCategories: [],
  aggregateTechSearch: chance(0.3),
  campaignEligibleOnly: chance(0.6),
  excludeContactIds: [],
});

/**
 * LeadFilter (LeadFilterConverter) — campaign_chat_memory.lead_filter. Same column NAME as
 * campaign.lead_filter but a structurally different, smaller DTO: no companyIds,
 * contactNames, keywordMatchMode, contactSegments, bdNames, isrNames, priorities,
 * titleCategories, aggregateTechSearch, campaignEligibleOnly or excludeContactIds.
 * Conversion between the two is lossy in the app; the seed must not conflate them.
 */
const leadFilter = () => {
  const full = leadFilterCriteria([], 'ANY');
  const {
    companyIds, contactNames, keywordMatchMode, contactSegments, bdNames, isrNames,
    priorities, titleCategories, aggregateTechSearch, campaignEligibleOnly, excludeContactIds,
    ...rest
  } = full;
  return rest;
};

// ---- campaign ------------------------------------------------------------------------

const CAMPAIGN_NAMES = [
  'Q3 Midwest Manufacturing Push', 'SAP Modernization Play', 'Snowflake Data Platform Outreach',
  'Aerospace Supplier Expansion', 'Plant Automation Retrofit', 'IMTS Follow-up',
  'Cloud Migration — Mid-Market', 'Predictive Maintenance Pilot', 'Tier-1 Automotive Sweep',
  'Food & Beverage Compliance', 'Southeast Territory Build', 'Energy Sector Warm-up',
  'Quality Systems Upgrade', 'Supply Chain Visibility', 'West Coast Electronics',
  'ERP Replacement Signals', 'Hiring-Signal Retarget', 'Lapsed Account Revival',
  'Enterprise Named Accounts', 'Net-New Logo Blitz',
];

export const buildCampaigns = (tenants, tenantIds, workspaces, workspaceIds, users, userIds, mailboxes, mailboxIds, companyIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const wi = i % workspaceIds.length;
    const ws = workspaces[wi];
    const tenantId = ws.tenant_id;
    const tenantUsers = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === tenantId);
    const wsMailboxes = mailboxIds.filter((_, mi) => mailboxes[mi].workspace_id === workspaceIds[wi]);
    const status = pick(E.CampaignStatus);
    const createdAt = between(200, 10);
    const creator = pick(tenantUsers);
    const matchMode = chance(0.5) ? 'ALL' : 'ANY';
    const targets = pickSome(companyIds, int(5, 25));

    rows.push({
      tenant_id: tenantId,
      workspace_id: workspaceIds[wi],
      name: CAMPAIGN_NAMES[i % CAMPAIGN_NAMES.length] + (i >= CAMPAIGN_NAMES.length ? ` ${i}` : ''),
      industry: sparse(i, 0.8, pick(['Manufacturing', ...C.INDUSTRY_NAMES])),
      status,
      // Only a campaign that actually went out has a launch date / approver.
      launched_at: ['RUNNING', 'PAUSED', 'COMPLETED'].includes(status) ? after(createdAt, 20) : null,
      approved_by: ['APPROVED', 'RUNNING', 'PAUSED', 'COMPLETED'].includes(status) ? pick(tenantUsers).u.email : null,
      scheduled_start: status === 'DRAFT' ? null : after(createdAt, 25),
      sending_mailbox_id: wsMailboxes.length ? pick(wsMailboxes) : null,
      sending_window: json({
        windowStart: pick(['08:00:00', '09:00:00', '10:00:00']),
        windowEnd: pick(['16:00:00', '17:00:00', '18:00:00']),
        sendingDays: chance(0.7) ? DAYS : pickBetween(DAYS, 2, 4),
      }),
      targeting_criteria: sparse(i, 0.85, json(targetingCriteria(`conv_${1000 + i}`, targets, []))),
      lead_filter: sparse(i, 0.8, json(leadFilterCriteria(targets, matchMode))),
      template_id: null, // set by the caller once sequence_template ids exist
      cc_recipients: sparse(i, 0.4, recipients([{ email: `gtm@${tenants.find((t, ti) => tenantIds[ti] === tenantId).domain}`, name: 'GTM Team' }])),
      bcc_recipients: sparse(i, 0.35, recipients([{ email: `crm@${tenants.find((t, ti) => tenantIds[ti] === tenantId).domain}`, name: 'CRM Archive' }])),
      created_by: creator.id,
      updated_by: sparse(i, 0.7, pick(tenantUsers).id),
      created_at: createdAt,
      updated_at: after(createdAt, 40),
    });
  }
  return rows;
};

// ---- campaign_email (sequence steps) -------------------------------------------------

const STEP_COPY = [
  {
    subject: 'Quick question about {{company}}',
    body: `<p>Hi {{first_name}},</p>
<p>I work with {{industry}} teams that are modernizing plant systems — usually the trigger is an ERP or data platform project that outgrew the tooling around it.</p>
<p>Given what {{company}} is running, I suspect the same pattern applies. Worth a short call?</p>
<p>Best,<br/>{{sender_name}}</p>`,
  },
  {
    subject: 'Re: Quick question about {{company}}',
    body: `<p>Hi {{first_name}},</p>
<p>Following up on the note below. One thing that tends to land with {{title}}s: we cut the reporting lag between the floor and the plan from days to minutes, without ripping out the ERP.</p>
<p>Happy to share how a comparable manufacturer approached it.</p>
<p>Best,<br/>{{sender_name}}</p>`,
  },
  {
    subject: 'Worth a look? — {{company}}',
    body: `<p>Hi {{first_name}},</p>
<p>Sharing a short case study from a {{industry}} company of similar size — they went from a two-week close to same-day visibility across four plants.</p>
<p>If this is not your area, could you point me to whoever owns it?</p>
<p>Best,<br/>{{sender_name}}</p>`,
  },
  {
    subject: 'Closing the loop',
    body: `<p>Hi {{first_name}},</p>
<p>I have not heard back, so I will assume the timing is not right and stop here.</p>
<p>If it becomes relevant later, just reply to this thread and I will pick it back up.</p>
<p>Best,<br/>{{sender_name}}</p>`,
  },
];

export const buildCampaignEmails = (campaigns, campaignIds, userIds, attachmentIds) => {
  const rows = [];
  campaigns.forEach((campaign, ci) => {
    const steps = int(2, 4); // ~60 steps across 20 campaigns
    for (let s = 0; s < steps; s++) {
      const i = rows.length;
      const copy = STEP_COPY[s];
      rows.push({
        campaign_id: campaignIds[ci],
        step_number: s + 1,
        delay_days: s === 0 ? 0 : pick([2, 3, 4, 5, 7]),
        subject: copy.subject,
        body_template: copy.body,
        // A step's status tracks its parent campaign — a DRAFT campaign has no RUNNING step.
        status: campaign.status === 'DRAFT' ? 'PENDING'
          : campaign.status === 'COMPLETED' ? 'COMPLETED'
            : campaign.status === 'PAUSED' ? 'PAUSED'
              : campaign.status === 'RUNNING' ? (s === 0 ? 'RUNNING' : 'PENDING')
                : 'PENDING',
        attachment_ids: sparse(i, 0.3, [String(pick(attachmentIds))]),
        updated_by: sparse(i, 0.7, pick(userIds)),
        updated_at: after(campaign.created_at, 30),
      });
    }
  });
  return rows;
};

// ---- campaign_contact ----------------------------------------------------------------

const REPLY_BODY = {
  INTERESTED: 'This is timely — we are scoping exactly this for next quarter. Can you send times?',
  NOT_NOW: 'Not a priority this quarter, but check back with me after our budget reset in Q1.',
  OBJECTION: 'We looked at this two years ago and it stalled on integration with our MES. What is different now?',
  OUT_OF_OFFICE: 'I am out of the office until the 14th with limited access to email.',
  NOT_RELEVANT: 'Wrong person — this would sit with our IT group, not operations.',
};

/**
 * EmailDataListConverter: [{stepNumber, emailPlatform, messageId, conversationId, sentAt,
 * emailDeliveryStatus, messages, intentCategory, classifiedAt}]. intentCategory/classifiedAt
 * are only present once a reply has actually been classified by the AI.
 */
const emailData = (steps, platform, replied) =>
  Array.from({ length: steps }, (_, s) => {
    const sentAt = between(90, 2);
    const isLast = s === steps - 1;
    const status = replied && isLast ? 'REPLIED' : chance(0.55) ? 'OPENED' : chance(0.05) ? 'BOUNCED' : 'SENT';
    const intent = status === 'REPLIED' ? pick(E.ReplyIntent) : null;
    return {
      stepNumber: s + 1,
      emailPlatform: platform,
      messageId: `<${int(100000, 999999)}.${int(1000, 9999)}@mail.example.com>`,
      conversationId: `thread_${int(100000, 999999)}`,
      sentAt: isoLocal(sentAt),
      emailDeliveryStatus: status,
      messages: [],
      intentCategory: intent,
      classifiedAt: intent ? isoLocal(after(sentAt, 2)) : null,
    };
  });

export const buildCampaignContacts = (campaigns, campaignIds, contacts, contactIds, mailboxes, count) => {
  const rows = [];
  const seen = new Set();

  for (let i = 0; rows.length < count && i < count * 4; i++) {
    const ci = int(0, campaigns.length - 1);
    const campaign = campaigns[ci];
    if (campaign.status === 'DRAFT' && chance(0.7)) continue; // drafts rarely have enrollments

    // Only enroll contacts belonging to the campaign's tenant.
    const pool = contacts.map((c, k) => ({ c, id: contactIds[k] })).filter(({ c }) => c.tenant_id === campaign.tenant_id && !c.do_not_contact);
    if (!pool.length) continue;
    const { c: contact, id: contactId } = pick(pool);

    const key = `${campaignIds[ci]}:${contactId}`;
    if (seen.has(key)) continue;
    seen.add(key);

    const status = campaign.status === 'COMPLETED' ? pick(['COMPLETED', 'COMPLETED', 'BOUNCED', 'UNSUBSCRIBED'])
      : campaign.status === 'RUNNING' ? pick(['ACTIVE', 'ACTIVE', 'PENDING', 'COMPLETED'])
        : campaign.status === 'PAUSED' ? 'PAUSED' : 'PENDING';
    const totalSteps = int(2, 4);
    const currentStep = status === 'PENDING' ? 0 : status === 'COMPLETED' ? totalSteps : int(1, totalSteps);
    const replied = ['ACTIVE', 'COMPLETED'].includes(status) && chance(0.18);
    const platform = pick(E.MailBoxType);
    const lastSent = currentStep > 0 ? between(60, 1) : null;

    rows.push({
      campaign_id: campaignIds[ci],
      contact_id: contactId,
      status,
      current_step: currentStep,
      participating: !['UNSUBSCRIBED', 'BOUNCED'].includes(status),
      reply_received: replied,
      last_sent_at: lastSent,
      // Only a live sequence has a next send scheduled.
      next_send_at: ['ACTIVE', 'PENDING'].includes(status) && currentStep < totalSteps
        ? plusDays(lastSent ?? NOW, int(2, 7)) : null,
      email_data: currentStep > 0 ? json(emailData(currentStep, platform, replied)) : null,
      updated_at: lastSent ?? campaign.created_at,
    });
  }
  return rows;
};

// ---- contact_email -------------------------------------------------------------------

export const buildContactEmails = (campaigns, campaignIds, contacts, contactIds, users, userIds, attachmentIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, campaigns.length - 1);
    const campaign = campaigns[ci];
    const pool = contacts.map((c, k) => ({ c, id: contactIds[k] })).filter(({ c }) => c.tenant_id === campaign.tenant_id);
    if (!pool.length) continue;
    const { c: contact, id: contactId } = pick(pool);
    const type = chance(0.7) ? 'CAMPAIGN' : 'DIRECT';
    const sender = pick(users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === campaign.tenant_id));
    const inbound = chance(0.25);
    const step = int(0, 3);

    rows.push({
      // A DIRECT email is not part of a campaign — that's the whole distinction.
      campaign_id: type === 'CAMPAIGN' ? campaignIds[ci] : null,
      contact_id: contactId,
      type,
      platform: pick(E.MailBoxType),
      subject: inbound
        ? `Re: ${STEP_COPY[step].subject.replace('{{company}}', contact.company_domain.replace('.com', ''))}`
        : STEP_COPY[step].subject.replace('{{company}}', contact.company_domain.replace('.com', '')),
      body: inbound
        ? `<p>${pick(Object.values(REPLY_BODY))}</p>`
        : STEP_COPY[step].body.replace('{{first_name}}', contact.first_name).replace('{{sender_name}}', sender.u.name),
      message_id: `<${int(100000, 999999)}.${int(1000, 9999)}@mail.example.com>`,
      conversation_id: `thread_${int(100000, 999999)}`,
      to_recipients: recipients([{ email: contact.email, name: contact.full_name }]),
      cc_recipients: sparse(i, 0.6, recipients([{ email: sender.u.email, name: sender.u.name }])),
      bcc_recipients: sparse(i, 0.5, recipients([{ email: `crm@${sender.u.email.split('@')[1]}`, name: 'CRM Archive' }])),
      attachment_ids: sparse(i, 0.5, [String(pick(attachmentIds))]),
      tenant_id: campaign.tenant_id,
      workspace_id: campaign.workspace_id,
      created_by: sender.id,
      created_at: between(120, 0),
    });
  }
  return rows;
};

// ---- contact_outreach_status ---------------------------------------------------------
// The cross-campaign view of a contact: one row per contact, listing every campaign it is
// currently in. Derived from campaign_contact so the two agree.

export const buildOutreachStatuses = (campaignContacts, contacts, contactIds, count) => {
  const byContact = new Map();
  campaignContacts.forEach((cc) => {
    if (!byContact.has(cc.contact_id)) byContact.set(cc.contact_id, []);
    byContact.get(cc.contact_id).push(cc);
  });

  const rows = [];
  for (const [contactId, ccs] of byContact) {
    if (rows.length >= count) break;
    const i = rows.length;
    const idx = contactIds.indexOf(contactId);
    if (idx === -1) continue;
    const contact = contacts[idx];
    const active = ccs.filter((cc) => ['ACTIVE', 'PENDING'].includes(cc.status));
    const anyBounced = ccs.some((cc) => cc.status === 'BOUNCED');
    const anyUnsub = ccs.some((cc) => cc.status === 'UNSUBSCRIBED');
    const allDone = ccs.every((cc) => cc.status === 'COMPLETED');
    const status = anyUnsub ? 'UNSUBSCRIBED' : anyBounced ? 'BOUNCED'
      : active.length ? 'ACTIVE' : allDone ? (chance(0.15) ? 'CONVERTED' : 'COMPLETED') : 'PAUSED';
    const lastEmail = ccs.map((cc) => cc.last_sent_at).filter(Boolean).sort((a, b) => b - a)[0] ?? null;

    rows.push({
      contact_id: contactId,
      tenant_id: contact.tenant_id,
      status,
      current_campaign_ids: active.map((cc) => String(cc.campaign_id)),
      last_email_at: lastEmail,
      sequence_completed_at: ['COMPLETED', 'CONVERTED'].includes(status) && lastEmail ? after(lastEmail, 10) : null,
      unsubscribe_token: `unsub_${int(10000000, 99999999)}`,
      updated_at: lastEmail ?? between(90, 0),
    });
  }
  return rows;
};

// ---- campaign_chat_memory ------------------------------------------------------------

export const buildChatMemories = (campaigns, campaignIds, userIds) =>
  campaigns.map((campaign, i) => {
    const createdAt = campaign.created_at;
    return {
      campaign_id: campaignIds[i],
      tenant_id: campaign.tenant_id,
      workspace_id: campaign.workspace_id,
      name: campaign.name,
      industry: sparse(i, 0.8, campaign.industry ?? 'Manufacturing'),
      targeting_criteria: sparse(i, 0.85, json(targetingCriteria(`conv_${2000 + i}`, [], []))),
      // NOTE: LeadFilter, not LeadFilterCriteria — a different DTO behind the same name.
      lead_filter: sparse(i, 0.75, json(leadFilter())),
      contact_limit: sparse(i, 0.8, pick([50, 100, 250, 500])),
      last_search_at: sparse(i, 0.85, after(createdAt, 20)),
      created_by: campaign.created_by,
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 30),
    };
  });

// ---- message (AI chat turns) ---------------------------------------------------------

const CHAT_TURNS = [
  ['Find manufacturing companies in the Midwest running SAP and Snowflake',
    'I found 12 companies matching all three technologies. They skew 1,000-5,000 employees and cluster in Illinois, Ohio and Michigan. Want me to pull the operations leaders at each?'],
  ['Only the ones with more than 1000 employees',
    'Narrowed to 8 companies above 1,000 employees. I have 34 contacts across them, mostly VP and Director level in Operations and IT.'],
  ['Write a first-touch email for these contacts',
    'Drafted a first-touch email leading with the ERP-modernization angle and referencing their Snowflake investment. Want a three-step sequence around it?'],
  ['Add a follow-up two days later',
    'Added step 2 with a 2-day delay. It references the case study and asks for a referral if they are the wrong owner.'],
  ['How many contacts would this campaign reach?',
    'As filtered, the campaign would reach 34 contacts across 8 companies. 3 are marked do-not-contact and would be skipped.'],
];

export const buildMessages = (campaigns, campaignIds, users, userIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const ci = int(0, campaigns.length - 1);
    const campaign = campaigns[ci];
    const tenantUsers = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === campaign.tenant_id);
    const [request, response] = CHAT_TURNS[i % CHAT_TURNS.length];
    const type = pick(E.MessageType);
    const searched = /find|how many|narrow|only/i.test(request);

    rows.push({
      campaign_id: sparse(i, 0.8, campaignIds[ci]),
      conversation_id: `conv_${1000 + ci}`,
      type,
      request,
      response,
      search_performed: searched,
      // TargetingCriteriaDtoConverter — same shape as TargetingCriteria.
      targeting_criteria: searched ? json(targetingCriteria(`conv_${1000 + ci}`, [], [])) : null,
      tenant_id: campaign.tenant_id,
      workspace_id: campaign.workspace_id,
      user_id: pick(tenantUsers).id,
      created_at: after(campaign.created_at, 30),
    });
  }
  return rows;
};

// ---- sequence_template / email_sequence_template -------------------------------------

export const buildSequenceTemplates = () => [
  { name: 'Classic 3-Step', purpose: 'Balanced first-touch, value-add follow-up and breakup. The default for net-new outbound.', step_count: 3, default_delays: [0, 3, 5] },
  { name: 'Fast 2-Step', purpose: 'Short, high-intent sequence for warm leads and event follow-up.', step_count: 2, default_delays: [0, 2] },
  { name: 'Patient 4-Step', purpose: 'Longer nurture for enterprise accounts with slow buying cycles.', step_count: 4, default_delays: [0, 4, 7, 10] },
  { name: 'Event Follow-up', purpose: 'Trade-show follow-up referencing the booth conversation.', step_count: 3, default_delays: [0, 2, 6] },
  { name: 'Hiring-Signal Retarget', purpose: 'Triggered when the scraper detects relevant job postings at an account.', step_count: 3, default_delays: [0, 3, 7] },
];

export const buildEmailSequenceTemplates = (tenants, tenantIds, userIds) => {
  const rows = [];
  tenantIds.forEach((tenantId, ti) => {
    const templates = buildSequenceTemplates().slice(0, ti === 0 ? 3 : 2);
    templates.forEach((t) => {
      const i = rows.length;
      const createdAt = between(300, 60);
      rows.push({
        tenant_id: tenantId,
        name: t.name,
        description: sparse(i, 0.85, t.purpose),
        // EmailSequenceTemplateStepListConverter -> [{stepNumber,subject,bodyTemplate,delayDays}]
        steps: json(
          Array.from({ length: t.step_count }, (_, s) => ({
            stepNumber: s + 1,
            subject: STEP_COPY[s].subject,
            bodyTemplate: STEP_COPY[s].body,
            delayDays: t.default_delays[s],
          })),
        ),
        created_by: pick(userIds),
        created_at: createdAt,
        updated_at: after(createdAt, 60),
      });
    });
  });
  return rows;
};

// ---- prompt_specification / fact -----------------------------------------------------

export const buildPromptSpecifications = (userIds) => [
  {
    type: 'CAMPAIGN_EMAIL_GENERATOR',
    prompt_template: `You write B2B outbound email for a company selling into US manufacturing.
Write a {{step_number}} of {{total_steps}} email to {{first_name}}, {{title}} at {{company}}.
Known technographics: {{technologies}}. Recent signals: {{signals}}.
Rules: under 120 words, one clear ask, no superlatives, no "I hope this finds you well".
Reference a concrete operational outcome, not a feature list.`,
    created_by: null,
    created_at: null,
  },
  {
    type: 'CAMPAIGN_GENERATOR',
    prompt_template: `You design outbound campaigns. Given a targeting brief, propose a campaign
name, the ICP in one sentence, and a {{step_count}}-step email sequence with delays.
Ground every claim in the supplied firmographics and technographics. If the brief is too
vague to target, say so and ask exactly one clarifying question.`,
    created_by: null,
    created_at: null,
  },
  {
    type: 'CAMPAIGN_COPILOT',
    prompt_template: `You are a copilot inside a lead-search product. Translate the user's request
into filters over: industry, employee range, region, technologies, titles, seniority.
Use keywordMatchMode ALL when the user says "and"/"all of", ANY when they say "or"/"any of".
Report result counts before proposing next steps. Never invent companies not in the results.`,
    created_by: null,
    created_at: null,
  },
].map((p, i) => ({ ...p, created_by: pick(userIds), created_at: between(500, 200) }));

export const buildFacts = () =>
  [
    'Manufacturing buyers rarely replace an ERP outright; most modernization lands as an integration project around it.',
    'Job postings are the earliest reliable public signal of a technology investment — usually 2-3 quarters ahead of a press release.',
    'Plant-level operations leaders hold more practical budget authority than the org chart suggests.',
    'AS9100 and IATF 16949 certifications are a strong proxy for aerospace and automotive supply-chain tiering.',
    'Reply rates in industrial outbound peak Tuesday through Thursday, 8-11am in the recipient timezone.',
    'A company running both SAP and Snowflake is almost always mid-migration and actively buying integration work.',
  ].map((fact) => {
    const createdAt = between(400, 100);
    return { fact, created_at: createdAt, updated_at: after(createdAt, 60) };
  });
