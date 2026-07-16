/**
 * Deterministic PRNG + sampling helpers.
 *
 * Everything random in the seed goes through this LCG so that re-seeding reproduces the
 * exact same database. Never use Math.random() in the seed.
 */

let _s = 1337;

/** Reset the stream. Called once at the start of a seed run. */
export const reseed = (s = 1337) => {
  _s = s;
};

/** Uniform [0,1). */
export const rnd = () => ((_s = (_s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);

/** Uniform integer in [min, max] inclusive. */
export const int = (min, max) => min + Math.floor(rnd() * (max - min + 1));

/** One element of `arr`. */
export const pick = (arr) => arr[Math.floor(rnd() * arr.length)];

/** True with probability p. */
export const chance = (p) => rnd() < p;

/** `n` distinct elements of `arr` (fewer if arr is shorter). */
export const pickSome = (arr, n) => {
  const pool = [...arr];
  const out = [];
  for (let i = 0; i < n && pool.length; i++) {
    out.push(...pool.splice(Math.floor(rnd() * pool.length), 1));
  }
  return out;
};

/** `min`..`max` distinct elements of `arr`. */
export const pickBetween = (arr, min, max) => pickSome(arr, int(min, max));

/**
 * Realistic sparsity for a nullable column: fills roughly `p` of rows, but ALWAYS fills
 * row 0 so no column can come out 100% NULL (a hard requirement of the replica).
 * Pass the row index as `i`.
 */
export const sparse = (i, p, value) => (i === 0 || rnd() < p ? value ?? null : null);

/** Lazy variant — `fn` only runs when the value is actually kept. */
export const sparseFn = (i, p, fn) => (i === 0 || rnd() < p ? fn() : null);

// ---- dates ---------------------------------------------------------------------------

/** The demo's "now". Fixed so seeding is deterministic across days. */
export const NOW = new Date('2026-07-01T12:00:00Z');
const DAY = 86400000;

/** A Date `days` before NOW (fractional days ok). */
export const daysAgo = (days) => new Date(NOW.getTime() - days * DAY);

/** Random Date between `maxDays` and `minDays` ago. */
export const between = (maxDays, minDays = 0) => daysAgo(minDays + rnd() * (maxDays - minDays));

/** A Date `days` after `d`. */
export const plusDays = (d, days) => new Date(d.getTime() + days * DAY);

/** A Date a random 0..`maxDays` after `d`, never past NOW. */
export const after = (d, maxDays = 30) => {
  const t = d.getTime() + rnd() * maxDays * DAY;
  return new Date(Math.min(t, NOW.getTime()));
};
