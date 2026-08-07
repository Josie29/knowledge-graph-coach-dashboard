/**
 * Format a duration for display.
 *
 * @param milliseconds - Elapsed time.
 * @returns A compact label, in milliseconds or seconds.
 */
export function formatDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`
  return `${(milliseconds / 1000).toFixed(2)} s`
}

/**
 * Format a micro-USD cost for display.
 *
 * Costs are stored as integer millionths of a dollar so totals stay exact and
 * identical across the two supported databases.
 *
 * @param microUsd - Cost in millionths of a dollar.
 * @returns A dollar label, or an em dash when the call was not priced.
 */
export function formatCost(microUsd: number): string {
  if (!microUsd) return '—'
  return `$${(microUsd / 1_000_000).toFixed(4)}`
}
