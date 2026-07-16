/**
 * The RFQ marketplace side: catalogs (industry/service/specification), vendors, sourcing
 * requests, quotations and the attachments/collaborators hanging off them.
 *
 * The catalogs are small by nature — they're reference data, not volume — but every one of
 * them still has to be populated, and the join tables have to point at real catalog rows.
 */
import * as E from '../enums.js';
import * as C from '../catalog.js';
import { json } from '../format.js';
import { rnd, int, pick, chance, pickSome, pickBetween, sparse, between, after, plusDays } from '../rng.js';

const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-');

// ---- industry ------------------------------------------------------------------------

const INDUSTRY_BLURB = {
  Manufacturing: 'Discrete and process manufacturers across the industrial base.',
  'Industrial Automation': 'Controls, robotics and plant-floor automation integrators.',
  Automotive: 'OEMs and tier-1/tier-2 suppliers to the automotive supply chain.',
  'Aerospace & Defense': 'Airframe, propulsion and defense primes and their suppliers.',
  Pharmaceuticals: 'Drug manufacturers and contract development organizations.',
  Chemicals: 'Specialty and commodity chemical producers.',
  'Logistics & Supply Chain': 'Freight, warehousing and supply-chain service providers.',
  'Energy & Utilities': 'Generation, transmission and utility operators.',
  'Consumer Goods': 'Durable and non-durable consumer product manufacturers.',
  Electronics: 'PCB, semiconductor and electronic component makers.',
  'Food & Beverage': 'Food processing and beverage production.',
  Retail: 'Retailers and their private-label supply base.',
  Healthcare: 'Providers, payers and medical device manufacturers.',
};

export const buildIndustries = (userIds) =>
  C.INDUSTRY_NAMES.map((name, i) => {
    const createdAt = between(700, 400);
    return {
      name,
      slug: slugify(name),
      description: sparse(i, 0.85, INDUSTRY_BLURB[name] ?? `${name} companies and their suppliers.`),
      image: sparse(i, 0.6, `https://cdn.leadplus.ai/industries/${slugify(name)}.svg`),
      segments: pickBetween(C.SEGMENTS, 1, 3),
      active: true,
      disabled: chance(0.08),
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

// ---- service_category / service ------------------------------------------------------

export const buildServiceCategories = (userIds) =>
  C.SERVICE_CATEGORIES.map((name, i) => {
    const createdAt = between(700, 400);
    return {
      name,
      active: true,
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

export const buildServices = (categoryIds, userIds) =>
  C.SERVICES.map(([name, catIdx], i) => {
    const createdAt = between(700, 400);
    return {
      name,
      slug: slugify(name),
      service_category_id: categoryIds[catIdx],
      active: true,
      disabled: chance(0.06),
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

// ---- specification_category / specification ------------------------------------------

export const buildSpecificationCategories = (userIds) =>
  C.SPECIFICATION_CATEGORIES.map(([name, type], i) => {
    const createdAt = between(700, 400);
    return {
      name,
      // Free string, not an enum — no SpecificationType exists in the backend.
      type,
      active: true,
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

export const buildSpecifications = (categories, categoryIds, userIds) =>
  C.SPECIFICATIONS.map(([name, catIdx], i) => {
    const createdAt = between(700, 400);
    return {
      name,
      type: categories[catIdx][1],
      specification_category_id: categoryIds[catIdx],
      icon: sparse(i, 0.75, `https://cdn.leadplus.ai/specs/${slugify(name)}.svg`),
      active: true,
      disabled: chance(0.06),
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

// ---- join tables ---------------------------------------------------------------------

export const buildIndustryServiceMappings = (industryIds, serviceIds) => {
  const rows = [];
  const seen = new Set();
  industryIds.forEach((industryId) => {
    pickSome(serviceIds, int(3, 8)).forEach((serviceId) => {
      const key = `${industryId}:${serviceId}`;
      if (seen.has(key)) return;
      seen.add(key);
      rows.push({ industry_id: industryId, service_id: serviceId });
    });
  });
  return rows;
};

export const buildServiceSpecifications = (serviceIds, specificationIds) => {
  const rows = [];
  const seen = new Set();
  serviceIds.forEach((serviceId) => {
    pickSome(specificationIds, int(2, 6)).forEach((specificationId) => {
      const key = `${serviceId}:${specificationId}`;
      if (seen.has(key)) return;
      seen.add(key);
      rows.push({ service_id: serviceId, specification_id: specificationId });
    });
  });
  return rows;
};

// ---- question_section / question -----------------------------------------------------

const SECTIONS = ['Capabilities', 'Capacity & Lead Time', 'Quality & Certification', 'Commercial Terms'];

const QUESTIONS = [
  ['Capabilities', 'Which processes do you run in-house?', 'MULTISELECT', ['CNC Milling', 'CNC Turning', 'Injection Molding', 'Sheet Metal', 'Welding', 'Assembly']],
  ['Capabilities', 'What is your largest part envelope?', 'TEXT', null],
  ['Capabilities', 'Do you offer design-for-manufacturing support?', 'BOOLEAN', null],
  ['Capacity & Lead Time', 'What is your typical lead time for production volumes?', 'RADIO', ['< 1 week', '1-2 weeks', '2-4 weeks', '4-6 weeks', '6+ weeks']],
  ['Capacity & Lead Time', 'How many shifts do you run?', 'RADIO', ['1', '2', '3']],
  ['Capacity & Lead Time', 'Describe your current capacity utilization.', 'TEXTAREA', null],
  ['Quality & Certification', 'Which certifications do you hold?', 'MULTISELECT', C.CERTIFICATIONS],
  ['Quality & Certification', 'Do you provide first-article inspection reports?', 'BOOLEAN', null],
  ['Quality & Certification', 'Describe your quality management system.', 'TEXTAREA', null],
  ['Commercial Terms', 'What are your standard payment terms?', 'RADIO', ['Net 15', 'Net 30', 'Net 45', 'Net 60']],
  ['Commercial Terms', 'What is your minimum order value?', 'TEXT', null],
  ['Commercial Terms', 'Do you accept blanket purchase orders?', 'BOOLEAN', null],
];

export const buildQuestionSections = (userIds) =>
  SECTIONS.map((name, i) => {
    const createdAt = between(700, 400);
    return {
      name,
      position: i + 1,
      active: true,
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

export const buildQuestions = (sectionIds, industryIds, userIds) =>
  QUESTIONS.map(([section, label, type, options], i) => {
    const createdAt = between(700, 400);
    return {
      question_section_id: sectionIds[SECTIONS.indexOf(section)],
      label,
      type,
      position: i + 1,
      // text[] and bigint[] native arrays.
      options: options ?? [],
      industry_ids: sparse(i, 0.6, pickSome(industryIds, int(1, 4))) ?? [],
      active: chance(0.92),
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 200),
    };
  });

// ---- vendor --------------------------------------------------------------------------

const TAGLINES = [
  'Precision parts, on time, every time.', 'Your partner from prototype to production.',
  'Tight tolerances. Tighter deadlines.', 'Certified quality, built in the Midwest.',
  'Engineering-led contract manufacturing.',
];

export const buildVendors = (tenantIds, users, userIds, industryIds, serviceIds, specificationIds, questions, questionIds) =>
  C.VENDOR_NAMES.map((company, i) => {
    const [state, city, postal] = pick(C.LOCATIONS);
    const domain = `${company.toLowerCase().replace(/[^a-z0-9]+/g, '')}.com`;
    const tenantId = tenantIds[i % tenantIds.length];
    const tenantUsers = users.map((u, ui) => ({ u, id: userIds[ui] })).filter(({ u }) => u.tenant_id === tenantId);
    const createdAt = between(600, 60);
    const status = pick(E.VendorVerificationStatus);

    return {
      tenant_id: tenantId,
      user_id: pick(tenantUsers).id,
      company_name: company,
      // AddressConverter -> JSON {street,city,state,postalCode,country}
      address: json({ street: `${int(100, 9999)} ${pick(['Industrial', 'Commerce', 'Foundry', 'Railroad', 'Airport'])} ${pick(['Dr', 'Blvd', 'Way', 'Rd'])}`, city, state, postalCode: postal, country: 'United States' }),
      website: `https://www.${domain}`,
      sales_email: `sales@${domain}`,
      phone_number: `+1${int(200, 989)}${int(200, 999)}${String(int(0, 9999)).padStart(4, '0')}`,
      fax_number: sparse(i, 0.35, `+1${int(200, 989)}${int(200, 999)}${String(int(0, 9999)).padStart(4, '0')}`),
      description: `${company} is a ${pick(C.COMPANY_SIZES)}-person contract manufacturer in ${city}, ${state}. ` +
        `We specialize in ${pickSome(C.SERVICES.map((s) => s[0]), 3).join(', ')} for ${pick(C.INDUSTRY_NAMES).toLowerCase()} customers.`,
      tagline: sparse(i, 0.8, pick(TAGLINES)),
      team_description: sparse(i, 0.7, `Our team of ${int(8, 120)} includes ${int(2, 15)} degreed engineers and ${int(1, 6)} certified quality inspectors.`),
      team_photo: sparse(i, 0.5, `https://cdn.leadplus.ai/vendors/${slugify(company)}/team.jpg`),
      logo: sparse(i, 0.75, `https://cdn.leadplus.ai/vendors/${slugify(company)}/logo.png`),
      video_link: sparse(i, 0.35, `https://www.youtube.com/watch?v=${Math.abs(int(1e10, 9e10)).toString(36)}`),
      scheduling_link: sparse(i, 0.5, `https://cal.example.com/${slugify(company)}/intro`),
      // Free-form text, no converter — really is prose in prod.
      business_hours: sparse(i, 0.7, pick(['Mon-Fri 7:00am-5:00pm CT', 'Mon-Fri 6:00am-4:30pm ET, 2nd shift by arrangement', 'Mon-Thu 6:00am-4:00pm PT'])),
      // SocialMediaConverter -> JSON
      social_media: sparse(i, 0.65, json({
        linkedin: `https://www.linkedin.com/company/${slugify(company)}`,
        twitter: chance(0.4) ? `https://twitter.com/${slugify(company).slice(0, 15)}` : null,
        facebook: chance(0.3) ? `https://www.facebook.com/${slugify(company)}` : null,
        instagram: null,
        youtube: chance(0.2) ? `https://www.youtube.com/@${slugify(company)}` : null,
      })),
      // AnswerListConverter -> JSON [{questionId, answer}]
      questionnaire: sparse(i, 0.7, json(
        pickSome(questionIds, int(3, 8)).map((qid) => {
          const q = questions[questionIds.indexOf(qid)];
          const answer = q.type === 'BOOLEAN' ? pick(['true', 'false'])
            : q.type === 'MULTISELECT' ? pickBetween(q.options, 1, 3).join(', ')
              : q.type === 'RADIO' ? pick(q.options)
                : q.type === 'TEXTAREA' ? 'Documented and audited annually; records retained for seven years.'
                  : `${int(2, 48)} in`;
          return { questionId: qid, answer };
        }),
      )),
      year_established: String(int(1946, 2018)),
      company_size: pick(C.COMPANY_SIZES),
      annual_revenue: pick(C.ANNUAL_REVENUES),
      avg_hourly_rate: sparse(i, 0.6, pick(C.HOURLY_RATES)),
      min_project_size: sparse(i, 0.6, pick(C.PROJECT_SIZES)),
      vendor_verification_status: status,
      review_comment: sparse(i, 0.5, status === 'REJECTED'
        ? 'Certifications could not be verified against the issuing registrar.'
        : 'Capabilities and certifications verified against submitted documentation.'),
      // Native arrays. NOTE: client_budgets / client_sizes / client_employee_ranges are
      // free-form varchar[], NOT the BudgetRange/EmployeeRange enums, despite the names.
      certifications: pickBetween(C.CERTIFICATIONS, 1, 4),
      client_budgets: pickBetween(['$1k-$5k', '$5k-$25k', '$25k-$100k', '$100k+'], 1, 3),
      client_employee_ranges: pickBetween(C.COMPANY_SIZES, 1, 3),
      client_industries: pickBetween(C.INDUSTRY_NAMES, 1, 4),
      client_locations: pickBetween(C.LOCATIONS.map((l) => l[0]), 1, 5),
      client_sizes: pickBetween(['Startup', 'SMB', 'Mid-Market', 'Enterprise'], 1, 3),
      languages_spoken: pickBetween(C.LANGUAGES, 1, 3),
      regions_covered: pickBetween(C.REGIONS.slice(0, 4), 1, 4),
      industry_ids: pickSome(industryIds, int(1, 4)),
      service_ids: pickSome(serviceIds, int(2, 8)),
      specification_ids: pickSome(specificationIds, int(2, 8)),
      open_to_any_location: chance(0.3),
      open_to_any_industry: chance(0.25),
      active: status !== 'REJECTED',
      created_at: createdAt,
      updated_at: after(createdAt, 120),
    };
  });

// ---- vendor_agreement ----------------------------------------------------------------

export const buildVendorAgreements = (vendors, vendorIds) => {
  const rows = [];
  vendors.forEach((vendor, vi) => {
    E.AgreementType.forEach((type) => {
      const i = rows.length;
      const signed = vendor.vendor_verification_status === 'APPROVED' || chance(0.5);
      const createdAt = after(vendor.created_at, 30);
      const signer = `${pick(C.FIRST_NAMES)} ${pick(C.LAST_NAMES)}`;

      rows.push({
        vendor_id: vendorIds[vi],
        agreement_type: type,
        name: `${type === 'PRIVACY_POLICY' ? 'Privacy Policy' : 'Terms of Service'} — ${vendor.company_name}`,
        title: sparse(i, 0.8, pick(['President', 'VP of Sales', 'General Manager', 'Owner'])),
        agreement_text: `This ${type === 'PRIVACY_POLICY' ? 'privacy policy' : 'agreement'} is entered into by ${vendor.company_name} ` +
          `and governs participation in the LeadPlus sourcing marketplace. Version ${int(1, 3)}.`,
        version: int(1, 3),
        signed,
        signed_by: signed ? signer : null,
        signed_at: signed ? after(createdAt, 20) : null,
        verified: signed && vendor.vendor_verification_status === 'APPROVED',
        // One-time signing code — only live while a signature is pending.
        otp: signed ? null : String(int(100000, 999999)),
        created_at: createdAt,
        updated_at: after(createdAt, 40),
      });
    });
  });
  return rows;
};

// ---- vendor_showcase -----------------------------------------------------------------

const SHOWCASES = [
  ['Aerospace bracket line transfer', 'Transferred a legacy bracket line with zero missed shipments.', 'Cut cycle time 22% and scrap 40% while holding AS9100 compliance throughout.'],
  ['High-mix CNC cell', 'Stood up a high-mix, low-volume machining cell for a medical customer.', 'Reduced changeover from 90 to 25 minutes; on-time delivery reached 99.1%.'],
  ['Injection molding transfer', 'Relocated 14 tools from an offshore supplier.', 'Landed cost dropped 12% and lead time went from 10 weeks to 3.'],
  ['Weldment redesign', 'Redesigned a weldment for manufacturability.', 'Removed 6 parts from the BOM and 18% from the piece price.'],
  ['Automated inspection', 'Deployed CMM-based automated first-article inspection.', 'Inspection labor down 55%; first-pass yield up to 98.6%.'],
];

export const buildVendorShowcases = (vendors, vendorIds, serviceIds, userIds) => {
  const rows = [];
  vendors.forEach((vendor, vi) => {
    const n = int(1, 3);
    for (let j = 0; j < n; j++) {
      const i = rows.length;
      const [project, description, results] = SHOWCASES[(vi + j) % SHOWCASES.length];
      const createdAt = after(vendor.created_at, 90);
      rows.push({
        vendor_id: vendorIds[vi],
        tenant_id: vendor.tenant_id,
        project_name: project,
        client_name: sparse(i, 0.6, `${pick(C.PREFIX)} ${pick(C.SUFFIX)}`), // often NDA'd
        description: sparse(i, 0.9, description),
        results_and_outcomes: sparse(i, 0.85, results),
        duration: sparse(i, 0.8, pick(['6 weeks', '3 months', '4 months', '6 months', '1 year'])),
        // varchar[] of ids-as-strings — the app stores these stringified, not as bigint[].
        service_ids: pickSome(vendor.service_ids, int(1, 3)).map(String),
        active: chance(0.9),
        created_by: pick(userIds),
        updated_by: sparse(i, 0.6, pick(userIds)),
        created_at: createdAt,
        updated_at: after(createdAt, 60),
      });
    }
  });
  return rows;
};

// ---- lead_data_pack / vendor_data_pack -----------------------------------------------

export const buildLeadDataPacks = (industryIds, userIds) =>
  [
    ['Industrial Core', ['Manufacturing', 'Industrial Automation']],
    ['Mobility & Aero', ['Automotive', 'Aerospace & Defense']],
    ['Process Industries', ['Chemicals', 'Pharmaceuticals', 'Food & Beverage']],
    ['Energy Transition', ['Energy & Utilities']],
    ['Consumer & Retail', ['Consumer Goods', 'Retail']],
    ['Electronics & Devices', ['Electronics', 'Healthcare']],
  ].map(([name, industries], i) => {
    const createdAt = between(500, 200);
    return {
      name,
      slug: slugify(name),
      // varchar[] of industry ids, stringified.
      industry_ids: industries.map((n) => String(industryIds[C.INDUSTRY_NAMES.indexOf(n)])).filter((v) => v !== 'undefined'),
      active: chance(0.9),
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 100),
    };
  });

export const buildVendorDataPacks = (vendors, vendorIds, packIds, users, userIds) => {
  const rows = [];
  vendors.forEach((vendor, vi) => {
    if (!chance(0.7)) return;
    pickSome(packIds, int(1, 3)).forEach((packId) => {
      const i = rows.length;
      const createdAt = after(vendor.created_at, 60);
      rows.push({
        vendor_id: vendorIds[vi],
        lead_data_pack_id: packId,
        tenant_id: vendor.tenant_id,
        assigned_by: sparse(i, 0.8, pick(users).email),
        active: chance(0.9),
        created_by: pick(userIds),
        updated_by: sparse(i, 0.6, pick(userIds)),
        created_at: createdAt,
        updated_at: after(createdAt, 30),
      });
    });
  });
  return rows;
};

// ---- request_for_quote / request_for_proposal ----------------------------------------

const RFQ_ITEMS = [
  ['Aluminum housing, 6061-T6', 'CNC-machined housing, 6061-T6, bead blast finish, ±0.005 in on critical features. Drawings available on request.'],
  ['Stainless manifold block', '316L manifold block, 12 ports, passivated. Requires material certs and FAI.'],
  ['Injection molded enclosure', 'ABS enclosure, textured finish, 4-cavity tool. Looking for tooling + first 10k parts.'],
  ['Sheet metal chassis', '14-gauge cold-rolled steel chassis, powder coated RAL 7035, welded assembly.'],
  ['Titanium bracket', 'Grade 5 titanium bracket, AS9100 required, ITAR-controlled drawing package.'],
  ['Cable harness assembly', 'Wire harness, 18 circuits, IPC/WHMA-A-620 Class 2, UL recognized components.'],
];

export const buildRfqs = (users, userIds, serviceIds, vendorIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const [item, description] = RFQ_ITEMS[i % RFQ_ITEMS.length];
    const ui = int(0, users.length - 1);
    const createdAt = between(240, 5);
    const status = pick(E.RequestStatus);

    rows.push({
      user_id: userIds[ui],
      title: `RFQ: ${item}`,
      description,
      quantity: pick([10, 25, 50, 100, 250, 500, 1000, 5000]),
      budget: pick(E.BudgetRange),
      status,
      deadline: plusDays(createdAt, int(14, 90)),
      // varchar[] of stringified ids.
      service_ids: pickSome(serviceIds, int(1, 3)).map(String),
      vendor_ids: sparse(i, 0.6, pickSome(vendorIds, int(1, 5)).map(String)) ?? [],
      active: status === 'OPEN',
      created_by: userIds[ui],
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 30),
    });
  }
  return rows;
};

export const buildRfps = (users, userIds, serviceIds, specificationIds, count) => {
  const rows = [];
  const RFP_TITLES = [
    ['Contract manufacturing partner — industrial controls', 'Seeking a long-term partner for build-to-print industrial control assemblies across two plants.'],
    ['Tooling program — consumer durables', 'Multi-tool program for a new consumer durables line. Design support expected.'],
    ['Machining supplier consolidation', 'Consolidating 9 machining suppliers to 3. Seeking proposals covering capacity and quality systems.'],
    ['Inspection & metrology services', 'Outsourced first-article and in-process inspection across three sites.'],
    ['Finishing services RFP', 'Powder coat and anodize services for an expanding enclosure line.'],
  ];

  for (let i = 0; i < count; i++) {
    const [title, description] = RFP_TITLES[i % RFP_TITLES.length];
    const ui = int(0, users.length - 1);
    const createdAt = between(240, 5);
    const status = pick(E.RequestStatus);

    rows.push({
      user_id: userIds[ui],
      title,
      description,
      quantity: pick([1, 5, 10, 100, 1000]),
      budget: pick(E.BudgetRange),
      status,
      timeline: sparse(i, 0.8, pick(['Award in 60 days, production in Q4', 'Evaluation through end of quarter', '6-month phased transition', 'Immediate — replacing a failed supplier'])),
      service_ids: pickSome(serviceIds, int(1, 4)).map(String),
      specification_ids: sparse(i, 0.7, pickSome(specificationIds, int(1, 5)).map(String)) ?? [],
      active: status === 'OPEN',
      created_by: userIds[ui],
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 30),
    });
  }
  return rows;
};

// ---- quotation -----------------------------------------------------------------------

export const buildQuotations = (rfqs, rfqIds, rfps, rfpIds, vendors, vendorIds, workspaces, workspaceIds, userIds, count) => {
  const rows = [];
  for (let i = 0; i < count; i++) {
    const fromRfq = chance(0.7);
    const sourceType = fromRfq ? 'REQUEST_FOR_QUOTE' : 'REQUEST_FOR_PROPOSAL';
    const si = fromRfq ? int(0, rfqs.length - 1) : int(0, rfps.length - 1);
    const source = fromRfq ? rfqs[si] : rfps[si];
    const sourceId = fromRfq ? rfqIds[si] : rfpIds[si];
    const vi = int(0, vendors.length - 1);
    const vendor = vendors[vi];
    const createdAt = after(source.created_at, 20);
    const status = pick(E.QuotationStatus);

    const items = Array.from({ length: int(1, 4) }, () => {
      const serviceName = pick(C.SERVICES.map((s) => s[0]));
      return {
        serviceName,
        description: `${serviceName} per supplied drawings and specifications.`,
        duration: pick(['1 week', '2 weeks', '3 weeks', '4 weeks', '6 weeks']),
        price: Number((int(500, 90000) + rnd()).toFixed(2)),
      };
    });

    // The quote is worked in one of the vendor's own tenant's workspaces.
    const wsForTenant = workspaceIds.filter((_, wi) => workspaces[wi].tenant_id === vendor.tenant_id);

    rows.push({
      source_type: sourceType,
      source_id: sourceId,
      vendor_id: vendorIds[vi],
      tenant_id: vendor.tenant_id,
      workspace_id: sparse(i, 0.8, pick(wsForTenant)),
      title: `Quotation — ${source.title.replace(/^RFQ: /, '')}`,
      description: sparse(i, 0.85, `${vendor.company_name} is pleased to quote the referenced scope. Pricing is firm for 30 days.`),
      // QuotationItemListConverter -> JSON array
      items: json(items),
      // Free-form text, no converter.
      payment_terms: sparse(i, 0.8, pick(['Net 30', 'Net 45', '50% on order, 50% on delivery', 'Net 30, 2% 10 net 30 available'])),
      deliverables: pickBetween(['Material certifications', 'First-article inspection report', 'CMM dimensional report', 'Certificate of conformance', 'Packaging per spec', 'ITAR compliance statement'], 1, 4),
      budget: source.budget,
      status,
      deadline: plusDays(createdAt, int(14, 60)),
      active: status !== 'REJECTED',
      created_by: pick(userIds),
      updated_by: sparse(i, 0.6, pick(userIds)),
      created_at: createdAt,
      updated_at: after(createdAt, 20),
    });
  }
  return rows;
};

// ---- collaborator --------------------------------------------------------------------

export const buildCollaborators = (rfqs, rfqIds, rfps, rfpIds, users, userIds) => {
  const rows = [];
  const add = (sourceType, sourceId, ownerUserId, createdAt) => {
    // The requester is always OWNER; a few others get added with lesser roles.
    const collaborators = [{ userId: ownerUserId, role: 'OWNER' }];
    const n = int(0, 2);
    for (let j = 0; j < n; j++) collaborators.push({ userId: pick(userIds), role: pick(['EDITOR', 'VIEWER', 'BD']) });

    collaborators.forEach(({ userId, role }) => {
      const i = rows.length;
      rows.push({
        source_type: sourceType,
        source_id: sourceId,
        user_id: userId,
        role,
        active: chance(0.92),
        created_by: ownerUserId,
        updated_by: sparse(i, 0.5, ownerUserId),
        created_at: createdAt,
        updated_at: after(createdAt, 20),
      });
    });
  };

  rfqs.forEach((rfq, i) => add('REQUEST_FOR_QUOTE', rfqIds[i], rfq.user_id, rfq.created_at));
  rfps.forEach((rfp, i) => add('REQUEST_FOR_PROPOSAL', rfpIds[i], rfp.user_id, rfp.created_at));
  return rows;
};

// ---- attachment / attachment_library / email_image -----------------------------------

const FILES = [
  ['housing-rev-c.pdf', 'application/pdf'], ['manifold-drawing.dwg', 'image/vnd.dwg'],
  ['enclosure-model.step', 'application/step'], ['material-cert-316l.pdf', 'application/pdf'],
  ['fai-report.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
  ['capability-statement.pdf', 'application/pdf'], ['quality-manual.pdf', 'application/pdf'],
  ['tooling-quote.pdf', 'application/pdf'], ['bracket-print.pdf', 'application/pdf'],
  ['harness-spec.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
];

export const buildAttachments = (sources, userIds) => {
  // `sources` is [{ sourceType, ids, createdAts }] across all four SourceType values, so
  // every enum value is represented and every source_id resolves.
  const rows = [];
  sources.forEach(({ sourceType, ids, createdAts }) => {
    ids.forEach((sourceId, k) => {
      const n = int(0, 2);
      for (let j = 0; j < n; j++) {
        const i = rows.length;
        const [fileName, fileType] = pick(FILES);
        const createdAt = createdAts[k];
        rows.push({
          source_type: sourceType,
          source_id: sourceId,
          file_name: fileName,
          file_type: fileType,
          file_url: `https://files.leadplus.ai/${sourceType.toLowerCase()}/${sourceId}/${fileName}`,
          size_bytes: int(12_000, 9_000_000),
          active: chance(0.93),
          created_by: pick(userIds),
          updated_by: sparse(i, 0.5, pick(userIds)),
          created_at: createdAt,
          updated_at: after(createdAt, 30),
        });
      }
    });
  });
  return rows;
};

export const buildAttachmentLibrary = (workspaceIds, userIds) => {
  const rows = [];
  workspaceIds.forEach((wsId) => {
    const n = int(2, 5);
    for (let j = 0; j < n; j++) {
      const i = rows.length;
      const [filename, fileType] = pick(FILES);
      rows.push({
        workspace_id: wsId,
        filename,
        file_type: fileType,
        file_url: `https://files.leadplus.ai/library/${wsId}/${filename}`,
        size_bytes: int(12_000, 9_000_000),
        updated_by: sparse(i, 0.7, pick(userIds)),
        updated_at: between(200, 0),
      });
    }
  });
  return rows;
};

export const buildEmailImages = (campaignIds, announcementIds, userIds) => {
  const rows = [];
  const add = (sourceType, ids) => {
    pickSome(ids, Math.min(ids.length, int(3, 8))).forEach((sourceId) => {
      const i = rows.length;
      rows.push({
        source_type: sourceType,
        source_id: sourceId,
        resource_url: `https://cdn.leadplus.ai/email-images/${sourceType.toLowerCase()}/${sourceId}/${pick(['logo', 'signature', 'banner', 'case-study', 'chart'])}.png`,
        created_by: pick(userIds),
        created_at: between(200, 0),
      });
    });
  };
  add('CAMPAIGN', campaignIds);
  add('ANNOUNCEMENT', announcementIds);
  // DIRECT images belong to a one-off email composed outside any campaign; the app stores
  // the composing user's id as source_id.
  add('DIRECT', userIds);
  return rows;
};
