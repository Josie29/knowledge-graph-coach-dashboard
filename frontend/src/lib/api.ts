import type { components } from '@/lib/api-types'

export interface Goal {
  id: string
  text: string
  priority: number
  target_date: string | null
}

// Workout generator contract — generated from the backend's Pydantic schemas
// (openapi.json → src/lib/api-types.ts via `npm run gen:api`).
export type WorkoutPlan = components['schemas']['WorkoutPlan']
export type WorkoutRequest = components['schemas']['WorkoutRequest']
export type ConstraintSet = components['schemas']['ConstraintSet']
export type PlannedExercise = components['schemas']['PlannedExercise']
export type PlanSection = components['schemas']['PlanSection']
export type ExcludedExercise = components['schemas']['ExcludedExercise']
export type RuleFiring = components['schemas']['RuleFiring']

// Trace store contract, also generated from the backend's Pydantic schemas.
export type TraceSummary = components['schemas']['TraceSummary']
export type TraceDetail = components['schemas']['TraceDetail']
export type TraceSpan = components['schemas']['TraceSpan']
export type TraceStats = components['schemas']['TraceStats']
export type SpanCategory = components['schemas']['SpanCategory']

export interface MemberProfile {
  id: string
  name: string
  age: number
  sex: string
  tier: string
  member_since: string
  coach_id: string
  timezone: string
}

export interface MemberResponse {
  profile: MemberProfile
  goals: Goal[]
}

/** The single seeded member until member selection lands in a later issue. */
export const JORDAN_MEMBER_ID = 'mbr_01HX9JORDAN'

/**
 * Fetch a member's profile header and goals from the API.
 *
 * @param memberId - Member identifier, e.g. `mbr_01HX9JORDAN`.
 * @returns The member's profile and goals.
 * @throws Error if the request fails or returns a non-2xx status.
 */
export async function fetchMember(memberId: string): Promise<MemberResponse> {
  const res = await fetch(`/api/members/${encodeURIComponent(memberId)}`)
  if (!res.ok) {
    throw new Error(
      res.status === 404 ? `Member ${memberId} not found` : `API returned ${res.status}`,
    )
  }
  return (await res.json()) as MemberResponse
}

/**
 * Generate (or adjust) a workout plan for a member.
 *
 * Pass the previous plan's `constraints_used` as `prior_constraints` together
 * with a follow-up message to adjust an existing plan.
 *
 * @param request - Prompt, time window, member, and optional prior constraints.
 * @returns The typed workout plan with provenance and exclusions.
 * @throws Error with the API's detail message on failure.
 */
export async function generateWorkout(request: WorkoutRequest): Promise<WorkoutPlan> {
  const res = await fetch('/api/workout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!res.ok) {
    let detail = `API returned ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // keep the status-based message
    }
    throw new Error(detail)
  }
  return (await res.json()) as WorkoutPlan
}

/**
 * Read a JSON endpoint, surfacing the API's `detail` message on failure.
 *
 * @param path - Absolute API path, e.g. `/api/traces`.
 * @returns The parsed response body.
 * @throws Error with the API's detail message on a non-2xx status.
 */
async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) {
    let detail = `API returned ${res.status}`
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // keep the status-based message
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

/**
 * List recent traces, newest first.
 *
 * @param limit - Maximum traces to return.
 * @returns Trace summaries with span counts, tokens, and cost.
 * @throws Error with the API's detail message on failure.
 */
export async function fetchTraces(limit = 50): Promise<TraceSummary[]> {
  return getJson<TraceSummary[]>(`/api/traces?limit=${limit}`)
}

/**
 * Fetch one trace and every span inside it.
 *
 * @param traceId - The 32-character hex trace id.
 * @returns The trace summary plus its spans, ordered by start time.
 * @throws Error with the API's detail message on failure.
 */
export async function fetchTrace(traceId: string): Promise<TraceDetail> {
  return getJson<TraceDetail>(`/api/traces/${encodeURIComponent(traceId)}`)
}

/**
 * Fetch aggregate tracing figures for a recent window.
 *
 * @param windowHours - How far back to aggregate.
 * @returns Counts, token and cost totals, and latency percentiles.
 * @throws Error with the API's detail message on failure.
 */
export async function fetchTraceStats(windowHours = 24): Promise<TraceStats> {
  return getJson<TraceStats>(`/api/traces/stats?window_hours=${windowHours}`)
}
