import { useCallback, useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { TraceWaterfall } from '@/components/TraceWaterfall'
import { formatCost, formatDuration } from '@/lib/format'
import { cn } from '@/lib/utils'
import {
  fetchTrace,
  fetchTraces,
  fetchTraceStats,
  type TraceDetail,
  type TraceStats,
  type TraceSummary,
} from '@/lib/api'

type ListState =
  | { kind: 'loading' }
  | { kind: 'loaded'; traces: TraceSummary[]; stats: TraceStats }
  | { kind: 'error'; message: string }

/**
 * Format a timestamp as a local wall-clock time.
 *
 * @param iso - ISO 8601 timestamp from the API.
 * @returns A short local time label.
 */
function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border px-3 py-2">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-lg font-semibold tabular-nums">{value}</span>
    </div>
  )
}

function StatsRow({ stats }: { stats: TraceStats }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile label={`Traces (${stats.window_hours}h)`} value={String(stats.trace_count)} />
      <StatTile label="Failed" value={String(stats.error_trace_count)} />
      <StatTile label="LLM calls" value={String(stats.llm_call_count)} />
      <StatTile label="Graph queries" value={String(stats.graph_query_count)} />
      <StatTile label="Tokens" value={stats.total_tokens.toLocaleString()} />
      <StatTile label="Cost" value={formatCost(stats.total_cost_micro_usd)} />
    </div>
  )
}

function TraceRow({
  trace,
  selected,
  onSelect,
}: {
  trace: TraceSummary
  selected: boolean
  onSelect: () => void
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? 'true' : undefined}
        className={cn(
          'flex w-full flex-col gap-1 px-3 py-2 text-left hover:bg-muted/50',
          selected && 'bg-muted',
        )}
      >
        <span className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-medium">{trace.name}</span>
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
            {formatDuration(trace.duration_ms)}
          </span>
        </span>
        <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          <span className="tabular-nums">{formatTime(trace.started_at)}</span>
          {trace.status === 'error' && <Badge variant="destructive">error</Badge>}
          {trace.llm_count > 0 && <Badge variant="secondary">{trace.llm_count} LLM</Badge>}
          {trace.tool_count > 0 && <Badge variant="secondary">{trace.tool_count} tool</Badge>}
          {trace.db_count > 0 && <Badge variant="secondary">{trace.db_count} graph</Badge>}
          {trace.input_tokens + trace.output_tokens > 0 && (
            <span className="tabular-nums">
              {(trace.input_tokens + trace.output_tokens).toLocaleString()} tok
            </span>
          )}
          {trace.cost_micro_usd > 0 && (
            <span className="tabular-nums">{formatCost(trace.cost_micro_usd)}</span>
          )}
        </span>
      </button>
    </li>
  )
}

/**
 * The Traces view: recent runs on the left, the selected run's span timeline
 * on the right.
 *
 * Every LLM call, tool call, and Neo4j query the API makes is recorded
 * locally, so one request reads end to end: prompt, concept resolution, safety
 * traversal, plan composition, with timings, tokens, and cost.
 */
export function TracesView() {
  const [state, setState] = useState<ListState>({ kind: 'loading' })
  const [selected, setSelected] = useState<TraceDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)

  const load = useCallback(() => {
    setState({ kind: 'loading' })
    Promise.all([fetchTraces(), fetchTraceStats()])
      .then(([traces, stats]) => setState({ kind: 'loaded', traces, stats }))
      .catch((err: unknown) =>
        setState({
          kind: 'error',
          message: err instanceof Error ? err.message : 'Failed to load traces',
        }),
      )
  }, [])

  useEffect(load, [load])

  const select = (traceId: string) => {
    setDetailError(null)
    fetchTrace(traceId)
      .then(setSelected)
      .catch((err: unknown) =>
        setDetailError(err instanceof Error ? err.message : 'Failed to load trace'),
      )
  }

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-6 px-4 py-6 sm:px-6">
      <section aria-label="Tracing overview" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-2xl font-semibold">Traces</h1>
            <p className="text-sm text-muted-foreground">
              Every LLM call, tool call, and graph query, recorded locally.
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={load}>
            Refresh
          </Button>
        </div>
        {state.kind === 'loaded' && <StatsRow stats={state.stats} />}
      </section>

      {state.kind === 'loading' && (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      )}

      {state.kind === 'error' && (
        <Card>
          <CardHeader>
            <CardTitle>Couldn't load traces</CardTitle>
            <CardDescription>{state.message}</CardDescription>
          </CardHeader>
        </Card>
      )}

      {state.kind === 'loaded' && state.traces.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle>No traces yet</CardTitle>
            <CardDescription>
              Generate a workout or ask the copilot a question, then come back. Health
              checks are deliberately not traced.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {state.kind === 'loaded' && state.traces.length > 0 && (
        <section
          aria-label="Recent traces"
          className="grid flex-1 grid-cols-1 items-start gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]"
        >
          <ul className="flex flex-col divide-y overflow-hidden rounded-md border">
            {state.traces.map((trace) => (
              <TraceRow
                key={trace.trace_id}
                trace={trace}
                selected={selected?.summary.trace_id === trace.trace_id}
                onSelect={() => select(trace.trace_id)}
              />
            ))}
          </ul>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                {selected ? selected.summary.name : 'Select a trace'}
              </CardTitle>
              <CardDescription>
                {detailError
                  ? detailError
                  : selected
                    ? `${selected.spans.length} spans · ${formatDuration(
                        selected.summary.duration_ms,
                      )} · ${formatCost(selected.summary.cost_micro_usd)}`
                    : 'Pick a run on the left to see its span timeline.'}
              </CardDescription>
            </CardHeader>
            {selected && (
              <CardContent>
                <TraceWaterfall spans={selected.spans} />
              </CardContent>
            )}
          </Card>
        </section>
      )}
    </main>
  )
}
