import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'

interface HealthResponse {
  status: 'ok' | 'degraded'
  neo4j: string
}

type HealthState =
  | { kind: 'loading' }
  | { kind: 'loaded'; health: HealthResponse }
  | { kind: 'error'; message: string }

/**
 * Placeholder page proving the frontend -> API -> Neo4j round trip.
 * Replaced by the dashboard shell in a later issue.
 */
function App() {
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

  const healthy = state.kind === 'loaded' && state.health.status === 'ok'

  return (
    <main className="flex min-h-dvh items-center justify-center bg-background p-6">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle>KG Coach Dashboard</CardTitle>
          <CardDescription>Development environment health</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">API</span>
            {state.kind === 'loading' ? (
              <Badge variant="outline">checking…</Badge>
            ) : state.kind === 'error' ? (
              <Badge variant="destructive">unreachable</Badge>
            ) : (
              <Badge className="bg-green-600 text-white">connected</Badge>
            )}
          </div>
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">Neo4j</span>
            {state.kind !== 'loaded' ? (
              <Badge variant="outline">—</Badge>
            ) : healthy ? (
              <Badge className="bg-green-600 text-white">connected</Badge>
            ) : (
              <Badge variant="destructive">degraded</Badge>
            )}
          </div>
          {state.kind === 'error' && (
            <p className="text-sm text-muted-foreground">{state.message}</p>
          )}
          {state.kind === 'loaded' && !healthy && (
            <p className="text-sm text-muted-foreground">{state.health.neo4j}</p>
          )}
        </CardContent>
      </Card>
    </main>
  )
}

export default App
