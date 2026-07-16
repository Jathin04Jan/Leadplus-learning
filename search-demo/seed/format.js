/**
 * The app persists "list-ish" data three different ways, and they are NOT interchangeable.
 * Getting these wrong is invisible in psql but throws or silently misreads in the app, so
 * each writer here names the converter it imitates.
 *
 *   1. TypedJsonConverter          -> real Jackson JSON            -> `json()`
 *   2. EnumListConverter           -> a Postgres-array-LOOKING string in a `text` column
 *                                     ("{A,B}", and "{}" — never NULL — when empty)
 *                                                                  -> `enumList()`
 *   3. @JdbcTypeCode(SqlTypes.ARRAY) -> a genuine varchar[]/bigint[] column
 *                                     -> just pass a JS array; node-pg handles it.
 */

/** TypedJsonConverter columns (sending_window, lead_filter, email_data, recipients, ...). */
export const json = (v) => JSON.stringify(v);

/**
 * EnumListConverter columns (tenant.modules, tenant_user.roles, lead_file_import.import_types,
 * lead_search_history.employee_ranges, tenant_lead_filter.employee_ranges).
 * Hand-rolled in EnumListConverter.java as "{" + join(",") + "}"; null/empty -> "{}", so
 * these columns are never NULL once written through JPA. We match that exactly.
 */
export const enumList = (values) => `{${(values ?? []).join(',')}}`;

/** RecipientListConverter shape: [{"email":"a@b.com","name":"A B"}]. */
export const recipients = (list) => json(list.map(({ email, name }) => ({ email, name })));

/**
 * Jackson writes LocalDateTime with WRITE_DATES_AS_TIMESTAMPS=false, i.e. ISO-8601 with no
 * zone and no trailing Z — what the converters' lenient readers expect inside JSON blobs.
 */
export const isoLocal = (d) => d.toISOString().replace('Z', '').replace(/\.(\d{3})000/, '.$1');
