import { HttpAgent } from '@ag-ui/client'
import type { components } from '@/lib/api-types'

/** Member-context slice served by GET /api/members/{id}/context. */
export type ContextSlice = components['schemas']['ContextSlice']
export type CoachBriefSlice = components['schemas']['CoachBriefSlice']

// The copilot's typed output streams over AG-UI (no OpenAPI response model),
// so these mirror backend/app/agents/copilot.py's CopilotAnswer union.
export interface ChartSeries {
  data_key: string
  label: string
}

export interface ChartSpec {
  chart_type: 'line' | 'bar' | 'area'
  title: string
  x_key: string
  series: ChartSeries[]
  data: Record<string, string | number | null>[]
  y_label?: string | null
  source: string
}

export interface TextAnswer {
  kind: 'text'
  markdown: string
  citations: string[]
}

export interface ChartAnswer {
  kind: 'chart'
  markdown: string
  chart: ChartSpec
  citations: string[]
}

export type CopilotAnswer = TextAnswer | ChartAnswer

/**
 * Parse a completed copilot output tool call into a CopilotAnswer.
 *
 * @param raw - The accumulated JSON args of the output tool call.
 * @returns The parsed answer, or null when the JSON is malformed.
 */
export function parseCopilotAnswer(raw: string): CopilotAnswer | null {
  try {
    const parsed = JSON.parse(raw) as CopilotAnswer
    if (parsed && (parsed.kind === 'text' || parsed.kind === 'chart')) return parsed
    return null
  } catch {
    return null
  }
}

/**
 * Create the AG-UI client for a member's copilot conversation. The agent
 * instance holds the message history, so keep one per conversation.
 */
export function createCopilotAgent(memberId: string): HttpAgent {
  return new HttpAgent({ url: `/api/copilot/${encodeURIComponent(memberId)}` })
}

/**
 * Fetch sections of the member-context graph for the brief/history surfaces.
 *
 * @param memberId - Member identifier.
 * @param sections - Which graph sections to read.
 * @returns The typed context slice.
 * @throws Error on a non-2xx response.
 */
export async function fetchContextSlice(
  memberId: string,
  sections: string[],
): Promise<ContextSlice> {
  const params = new URLSearchParams()
  for (const section of sections) params.append('sections', section)
  const res = await fetch(
    `/api/members/${encodeURIComponent(memberId)}/context?${params.toString()}`,
  )
  if (!res.ok) throw new Error(`API returned ${res.status}`)
  return (await res.json()) as ContextSlice
}
