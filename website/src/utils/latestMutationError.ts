/**
 * The error of the most recently started mutation among several, or null.
 *
 * A react-query mutation keeps its `error` until that same mutation runs
 * again, so OR-ing several mutations' errors keeps an old failure on screen
 * after a different action succeeds, and lets an older failure hide a newer
 * one. Ranking by `submittedAt` makes one notice describe the latest action.
 */
export function latestMutationError(...mutations: { submittedAt: number; error: unknown }[]): unknown {
  let latest: { submittedAt: number; error: unknown } | null = null
  // `>=`: on a same-millisecond tie the later-listed mutation wins, so a tie
  // resolves the same way every time.
  for (const m of mutations) if (!latest || m.submittedAt >= latest.submittedAt) latest = m
  return latest?.error ?? null
}
