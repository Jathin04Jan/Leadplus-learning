/**
 * Enum constants, transcribed verbatim from the Java enums in the product source:
 *   Leadplus-corelabs/leadplus-service/src/main/java/ai/leadplus/**
 *
 * Every varchar column that is @Enumerated(EnumType.STRING) stores one of these names, so
 * the seed must not invent strings — wrong values make the replica look fake and break the
 * app on read. Each block cites the file it came from; re-check them if the source moves.
 */

// ---- domain/common -------------------------------------------------------------------
export const PlanTier = ['BASIC', 'ENTERPRISE', 'ADVANCED'];                     // PlanTier.java
export const PlanFeature = ['BD_TERRITORY_ROUTING'];                             // PlanFeature.java
export const RequestStatus = ['OPEN', 'CLOSED', 'CANCELLED'];                    // RequestStatus.java
export const LeadType = ['LEAD_COMPANY', 'LEAD_CONTACT'];                        // LeadType.java
export const CRM = ['HUBSPOT', 'ZOHO'];                                          // CRM.java
export const KeywordMatchMode = ['ANY', 'ALL'];                                  // KeywordMatchMode.java
export const RevisionAction = ['CREATE', 'UPDATE', 'DELETE', 'RESTORE'];         // RevisionAction.java
export const SourcingRequestType = ['REQUEST_FOR_QUOTE', 'REQUEST_FOR_PROPOSAL']; // SourcingRequestType.java
export const ApolloDataType = [                                                  // ApolloDataType.java
  'PEOPLE_SEARCH', 'SINGLE_ENRICHMENT', 'BULK_ENRICHMENT',
  'ORGANIZATION_ENRICHMENT', 'ORGANIZATION_SEARCH',
];
export const BudgetRange = [                                                     // BudgetRange.java
  'BELOW_1K', 'BETWEEN_1K_AND_5K', 'BETWEEN_5K_AND_10K',
  'BETWEEN_10K_AND_25K', 'BETWEEN_25K_AND_50K', 'ABOVE_50K',
];

// ---- application/common --------------------------------------------------------------
export const Recency = ['THIS_WEEK', 'THIS_MONTH', 'THIS_YEAR'];                 // Recency.java

// ---- leadgen/search ------------------------------------------------------------------
export const DataSource = ['APOLLO', 'MANUAL'];                                  // DataSource.java
export const LeadCompanyStatus = ['COLD', 'IN_PROGRESS', 'CONVERTED'];           // LeadCompanyStatus.java
export const EventType = ['NEWS', 'FUNDING', 'HIRING', 'PRODUCT', 'PARTNERSHIP', 'OTHER']; // EventType.java
export const LeadQueryType = [                                                   // LeadQueryType.java
  'COMPANY_INDUSTRY', 'COMPANY_REGION', 'COMPANY_COUNTRY', 'COMPANY_STATE',
  'COMPANY_CITY', 'CONTACT_SENIORITY', 'CONTACT_TITLE',
];
export const LeadContactEventType = [                                            // LeadContactEventType.java
  'CAMPAIGN_INITIATED', 'CAMPAIGN_EMAIL_SENT', 'CAMPAIGN_EMAIL_REPLIED',
  'EMAIL_SENT', 'EMAIL_OPENED', 'NOTE_ADDED', 'NOTE_UPDATED', 'NOTE_DELETED',
];
export const LeadContactEventCategory = ['CAMPAIGN', 'EMAIL', 'NOTE'];           // LeadContactEventCategory.java
export const LeadContactEventSourceType = [                                      // LeadContactEventSourceType.java
  'CAMPAIGN', 'CAMPAIGN_EMAIL', 'CAMPAIGN_CONTACT', 'CONTACT_EMAIL', 'LEAD_NOTE',
];
// EmployeeRange.java stores its *label* (@JsonValue), not the constant name.
export const EmployeeRangeLabels = ['0-500', '501-1000', '1001-5000', '5001-10000', '10001+'];

// ---- leadgen/campaign ----------------------------------------------------------------
export const CampaignStatus = [                                                  // CampaignStatus.java
  'DRAFT', 'PENDING_APPROVAL', 'APPROVED', 'RUNNING', 'PAUSED', 'COMPLETED',
];
export const CampaignEmailStatus = ['PENDING', 'RUNNING', 'PAUSED', 'COMPLETED']; // CampaignEmailStatus.java
export const CampaignContactStatus = [                                           // CampaignContactStatus.java
  'PENDING', 'ACTIVE', 'PAUSED', 'COMPLETED', 'BOUNCED', 'UNSUBSCRIBED',
];
export const ContactEmailType = ['DIRECT', 'CAMPAIGN'];                          // ContactEmailType.java
export const ReplyIntent = [                                                     // ReplyIntent.java
  'INTERESTED', 'NOT_NOW', 'OBJECTION', 'OUT_OF_OFFICE', 'NOT_RELEVANT',
];
export const LocationType = ['STATE', 'COUNTRY'];                                // LocationType.java
export const EmailDeliveryStatus = ['SENT', 'OPENED', 'REPLIED', 'BOUNCED'];     // EmailDeliveryStatus.java

// ---- leadgen/tracking ----------------------------------------------------------------
export const GlobalOutreachStatus = [                                            // GlobalOutreachStatus.java
  'ACTIVE', 'PAUSED', 'COMPLETED', 'BOUNCED', 'UNSUBSCRIBED', 'CONVERTED',
];

// ---- shared/workspace ----------------------------------------------------------------
export const MailBoxType = ['GMAIL', 'OUTLOOK', 'SMTP', 'SES'];                  // MailBoxType.java
export const Module = ['CUSTOMER', 'VENDOR', 'LEAD_GENERATION', 'ADMINISTRATION']; // Module.java
export const SourceType = [                                                      // SourceType.java
  'REQUEST_FOR_QUOTE', 'REQUEST_FOR_PROPOSAL', 'SHOWCASE', 'QUOTATION',
];
export const WorkspaceUserRole = ['OWNER', 'MEMBER', 'WORKSPACE_ADMIN'];         // WorkspaceUserRole.java
export const WorkspaceUserStatus = ['INVITED', 'ACCEPTED', 'REVOKED'];           // WorkspaceUserStatus.java
export const UserRole = ['CUSTOMER', 'VENDOR', 'GUEST', 'USER', 'ADMIN', 'TENANT_OWNER']; // UserRole.java
export const UserStatus = ['PENDING', 'APPROVED', 'REJECTED'];                   // UserStatus.java
export const IdentityProviderType = ['GOOGLE', 'FACEBOOK', 'LINKEDIN', 'APPLE', 'MICROSOFT']; // IdentityProviderType.java
export const TenantAnnouncementStatus = ['DRAFT', 'IN_PROGRESS', 'COMPLETED', 'FAILED']; // TenantAnnouncementStatus.java
export const TenantAnnouncementContactStatus = ['PENDING', 'SENT', 'BOUNCED', 'FAILED']; // TenantAnnouncementContactStatus.java
export const TenantAnnouncementContactSource = ['CRM', 'LEAD'];                  // TenantAnnouncementContactSource.java
export const EmailImageSourceType = ['DIRECT', 'CAMPAIGN', 'ANNOUNCEMENT'];      // EmailImageSourceType.java
export const AwsSESVerificationState = ['VERIFIED', 'UNVERIFIED', 'PENDING'];    // AwsSESVerificationState.java
export const MailboxConnectionStatus = ['VERIFIED', 'UNVERIFIED', 'PENDING'];    // MailboxConnectionStatus.java

// ---- shared/admin --------------------------------------------------------------------
export const AgreementType = ['PRIVACY_POLICY', 'TERMS_OF_SERVICE'];             // AgreementType.java
export const FeedbackType = ['FEATURE_REQUEST', 'BUG_REPORT', 'IMPROVEMENT', 'GENERAL']; // FeedbackType.java
export const FeedbackStatus = ['NEW', 'REVIEWED', 'RESPONDED'];                  // FeedbackStatus.java
export const LogLevel = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];                   // LogLevel.java
export const UserActivityLogType = [                                             // UserActivityLogType.java
  'EMAIL_SENT', 'CAMPAIGN', 'CALL', 'EMAIL_OPENED', 'NOTE', 'MEETING',
];
export const QuestionType = ['TEXT', 'TEXTAREA', 'MULTISELECT', 'BOOLEAN', 'RADIO']; // QuestionType.java
export const ImportType = [                                                      // ImportType.java
  'CONTACT_ENRICHMENT', 'COMPANY_ENRICHMENT', 'CONTACT_CREATE', 'COMPANY_CREATE',
];
export const ImportStatus = [                                                    // ImportStatus.java
  'PENDING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'PARTIAL_SUCCESS', 'ROLLED_BACK',
];
export const RecordStatus = ['INSERTED', 'UPDATED', 'SKIPPED', 'FAILED', 'NO_CHANGE']; // RecordStatus.java
export const TargetEntity = ['LEAD_CONTACT', 'LEAD_COMPANY'];                    // TargetEntity.java
export const ScrapeJobStatus = ['PENDING', 'COMPLETED', 'FAILED'];               // ScrapeJobStatus.java
export const ScrapeSourceType = ['COMPANY_WEBSITE', 'COMPANY_JOB'];              // ScrapeSourceType.java
export const PromptSpecificationType = [                                         // PromptSpecificationType.java
  'CAMPAIGN_EMAIL_GENERATOR', 'CAMPAIGN_GENERATOR', 'CAMPAIGN_COPILOT',
];

// ---- shared/ai -----------------------------------------------------------------------
export const MessageType = [                                                     // MessageType.java
  'CAMPAIGN_GENERATOR', 'CAMPAIGN_AGENT', 'LEAD_CHAT_ASSISTANT', 'CUSTOMER_CHAT_ASSISTANT',
];

// ---- portal --------------------------------------------------------------------------
export const VendorVerificationStatus = ['INCOMPLETE', 'PENDING', 'APPROVED', 'REJECTED']; // VendorVerificationStatus.java
export const CollaboratorRole = ['OWNER', 'EDITOR', 'VIEWER', 'BD'];             // CollaboratorRole.java
export const QuotationStatus = ['ACCEPTED', 'REJECTED', 'QUOTED', 'PENDING'];    // QuotationStatus.java
export const ContactAddStatus = ['CREATED', 'ENRICHED', 'NO_CHANGE'];            // ContactAddStatus.java
