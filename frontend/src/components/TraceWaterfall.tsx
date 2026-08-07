import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { formatDuration } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { SpanCategory, TraceSpan } from '@/lib/api'

/**
 * Bar colour per span category. Distinct hues rather than theme tokens: the
 * whole point of the waterfall is telling LLM calls, tool calls, and graph
 * queries apart at a glance.
 */
const CATEGORY_BAR: Record<SpanCategory, string> = {
  agent: 'bg-violet-500',
  llm: 'bg-blue-500',
  tool: 'bg-amber-500',
  db: 'bg-emerald-500',
  http: 'bg-slate-400',
  other: 'bg-zinc-400',
}

const CATEGORY_LABEL: Record<SpanCategory, string> = {
  agent: 'agent',
  llm: 'LLM',
  tool: 'tool',
  db: 'graph',
  http: 'request',
  other: 'other',
}

/** Maximum nesting indent, so a deep trace never squeezes the label column. */
const MAX_DEPTH = 5

/**
 * Label a span with something that distinguishes it from its siblings.
 *
 * Span names are stable identifiers, not descriptions — every graph span is
 * `neo4j.query` and every resolver step is `resolve_concepts` — so a row falls
 * back through the summaries the backend records: the step's own outcome
 * first ("resolve 'burpees' -> mp_plyometric"), then the Cypher it ran.
 *
 * @param span - The span being rendered.
 * @returns The text for the span's row.
 */
function spanLabel(span: TraceSpan): string {
  const summary = span.attributes?.['kg_coach.summary']
  if (typeof summary === 'string' && summary) return summary
  const statement = span.attributes?.['db.statement.summary']
  if (typeof statement === 'string' && statement) return statement
  return span.name
}

interface PositionedSpan {
  span: TraceSpan
  depth: number
  offsetPercent: number
  widthPercent: number
}

/**
 * Compute how deeply a span is nested inside its trace.
 *
 * @param span - The span to measure.
 * @param parents - Map of span id to parent span id.
 * @returns Nesting depth, capped at MAX_DEPTH. Spans whose parent was dropped
 *   by sampling resolve to depth 0 rather than looping.
 */
function depthOf(span: TraceSpan, parents: Map<string, string | null>): number {
  let depth = 0
  let current = span.parent_span_id
  while (current && depth < MAX_DEPTH) {
    const next = parents.get(current)
    if (next === undefined) break
    current = next
    depth += 1
  }
  return depth
}

/**
 * Lay spans out on a shared timeline.
 *
 * @param spans - Every span in the trace, ordered by start time.
 * @returns Each span with its nesting depth and its bar's position and width
 *   as percentages of the trace's total wall-clock time.
 */
function position(spans: TraceSpan[]): PositionedSpan[] {
  const starts = spans.map((span) => new Date(span.started_at).getTime())
  const traceStart = Math.min(...starts)
  const traceEnd = Math.max(
    ...spans.map(
      (span, index) => starts[index] + span.duration_ms,
    ),
  )
  // A trace can be shorter than a millisecond; never divide by zero.
  const total = Math.max(traceEnd - traceStart, 1)
  const parents = new Map(spans.map((span) => [span.span_id, span.parent_span_id]))

  return spans.map((span, index) => ({
    span,
    depth: depthOf(span, parents),
    offsetPercent: ((starts[index] - traceStart) / total) * 100,
    // Floor the width so a sub-millisecond span is still visible.
    widthPercent: Math.max((span.duration_ms / total) * 100, 0.5),
  }))
}

function SpanDetails({ span }: { span: TraceSpan }) {
  const attributes = Object.entries(span.attributes ?? {})
  return (
    <div className="flex flex-col gap-3 border-t bg-muted/40 px-3 py-3 text-xs">
      {span.error_message && (
        <p className="text-destructive">{span.error_message}</p>
      )}
      {span.input_preview && (
        <Preview
          label="Input"
          body={span.input_preview}
          truncated={span.input_truncated}
        />
      )}
      {span.output_preview && (
        <Preview
          label="Output"
          body={span.output_preview}
          truncated={span.output_truncated}
        />
      )}
      {attributes.length > 0 && (
        <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-3 gap-y-1">
          {attributes.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="text-muted-foreground">{key}</dt>
              <dd className="truncate font-mono">{String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}

function Preview({
  label,
  body,
  truncated,
}: {
  label: string
  body: string
  truncated: boolean
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-muted-foreground">
        {label}
        {truncated && ' (truncated)'}
      </span>
      <pre className="max-h-48 overflow-auto rounded bg-background p-2 font-mono text-xs whitespace-pre-wrap">
        {body}
      </pre>
    </div>
  )
}

/**
 * Render a trace's spans as a timeline, one row per span.
 *
 * Rows expand to show the span's prompt/completion preview and remaining
 * attributes, which is what makes a bad plan debuggable after the fact.
 */
export function TraceWaterfall({ spans }: { spans: TraceSpan[] }) {
  const [expanded, setExpanded] = useState<string | null>(null)

  if (spans.length === 0) {
    return <p className="text-sm text-muted-foreground">This trace has no spans.</p>
  }

  return (
    <ul className="flex flex-col divide-y rounded-md border">
      {position(spans).map(({ span, depth, offsetPercent, widthPercent }) => {
        const isOpen = expanded === span.span_id
        return (
          <li key={span.span_id}>
            <button
              type="button"
              aria-expanded={isOpen}
              onClick={() => setExpanded(isOpen ? null : span.span_id)}
              className="flex w-full flex-col gap-1 px-3 py-2 text-left hover:bg-muted/50 sm:grid sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] sm:items-center sm:gap-3"
            >
              <span
                className="flex min-w-0 items-center gap-2"
                style={{ paddingInlineStart: `${depth * 0.75}rem` }}
              >
                <Badge variant="secondary" className="shrink-0">
                  {CATEGORY_LABEL[span.category]}
                </Badge>
                <span
                  className={cn(
                    'truncate text-sm',
                    // Cypher reads better monospaced, and the visual break
                    // makes the graph rows scannable against agent rows.
                    span.category === 'db' && 'font-mono text-xs',
                  )}
                >
                  {spanLabel(span)}
                </span>
                {span.status === 'error' && (
                  <Badge variant="destructive" className="shrink-0">
                    error
                  </Badge>
                )}
              </span>
              <span className="flex items-center gap-2">
                <span className="relative h-2 min-w-0 flex-1 rounded-full bg-muted">
                  <span
                    className={cn(
                      'absolute inset-y-0 rounded-full',
                      CATEGORY_BAR[span.category],
                    )}
                    style={{
                      insetInlineStart: `${offsetPercent}%`,
                      width: `${widthPercent}%`,
                    }}
                  />
                </span>
                <span className="w-16 shrink-0 text-right text-xs tabular-nums text-muted-foreground">
                  {formatDuration(span.duration_ms)}
                </span>
              </span>
            </button>
            {isOpen && <SpanDetails span={span} />}
          </li>
        )
      })}
    </ul>
  )
}
