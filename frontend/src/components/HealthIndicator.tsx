import { useEffect, useState } from 'react'

interface HealthResponse {
  status: 'ok' | 'degraded'
  neo4j: string
}

type HealthState =
  | { kind: 'loading' }
  | { kind: 'loaded'; health: HealthResponse }
  | { kind: 'error'; message: string }

/**
 * Compact API/Neo4j status readout for the dashboard header — the successor
 * to the scaffold's full-page health card, preserving the /api/health round trip.
 */
export function HealthIndicator() {
  const [state, setState] = useState<HealthState>({ kind: 'loading' })

  useEffect(() => {
    fetch('/api/health')
      .then(async (res) => {
        if (!res.ok) throw new Error(`API returned ${res.status}`)
        const health = (await res.json()) as HealthResponse
        setState({ kind: 'loaded', health })
      })
      .catch((err: unknown) => {
        setState({
          kind: 'error',
          message: err instanceof Error ? err.message : 'API unreachable',
        })
      })
  }, [])

  const { dotClass, label, detail } =
    state.kind === 'loading'
      ? { dotClass: 'bg-muted-foreground', label: 'Checking…', detail: undefined }
      : state.kind === 'error'
        ? { dotClass: 'bg-destructive', label: 'API unreachable', detail: state.message }
        : state.health.status === 'ok'
          ? { dotClass: 'bg-green-600', label: 'All systems go', detail: 'API and Neo4j connected' }
          : { dotClass: 'bg-amber-500', label: 'Neo4j degraded', detail: state.health.neo4j }

  return (
    <span
      className="flex items-center gap-2 text-xs text-muted-foreground"
      title={detail}
      aria-live="polite"
    >
      <span aria-hidden="true" className={`size-2 rounded-full ${dotClass}`} />
      {label}
    </span>
  )
}
