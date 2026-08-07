import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  fetchMemberSubgraph,
  formatPropertyValue,
  nodeRadius,
  readPalette,
  TYPE_LABELS,
  TYPE_ORDER,
  type GraphNode,
  type GraphPalette,
  type MemberSubgraph,
} from '@/lib/graph'

/** force-graph mutates the objects it is given, adding x/y and swapping link
 *  endpoints for node references. These mirror the post-mutation shape. */
type ForceNode = GraphNode & { x?: number; y?: number }
type ForceLink = {
  id: string
  source: string | ForceNode
  target: string | ForceNode
  type: string
  derived: boolean
}

interface D3Force {
  strength?: (value: number) => void
  distance?: (value: number) => void
}

interface ForceGraphHandle {
  zoomToFit: (durationMs?: number, padding?: number) => void
  d3Force: (name: string) => D3Force | undefined
  d3ReheatSimulation?: () => void
}

type GraphState =
  | { kind: 'loading' }
  | { kind: 'loaded'; graph: MemberSubgraph }
  | { kind: 'error'; message: string }

/** Endpoints arrive as ids and are swapped for node objects once simulated. */
function endpointId(endpoint: string | ForceNode): string {
  return typeof endpoint === 'string' ? endpoint : endpoint.id
}

export function GraphView({ memberId }: { memberId: string }) {
  const [state, setState] = useState<GraphState>({ kind: 'loading' })
  const [includeNeighbors, setIncludeNeighbors] = useState(true)
  const [selected, setSelected] = useState<GraphNode | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)
  const [palette, setPalette] = useState<GraphPalette | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })

  const containerRef = useRef<HTMLDivElement>(null)
  const graphRef = useRef<ForceGraphHandle | null>(null)
  // Mirrors `hovered` so the pointer handler reads the node under the cursor
  // at the instant of the press, not what the last render closed over.
  const hoveredRef = useRef<string | null>(null)

  useEffect(() => {
    setState({ kind: 'loading' })
    setSelected(null)
    let cancelled = false
    fetchMemberSubgraph(memberId, includeNeighbors)
      .then((graph) => {
        if (!cancelled) setState({ kind: 'loaded', graph })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setState({
          kind: 'error',
          message: err instanceof Error ? err.message : 'Failed to load the graph',
        })
      })
    return () => {
      cancelled = true
    }
  }, [memberId, includeNeighbors])

  // Canvas cannot resolve `var(--x)`, so the tokens are read once here and
  // re-read whenever the theme class flips on <html>.
  useEffect(() => {
    setPalette(readPalette())
    const observer = new MutationObserver(() => setPalette(readPalette()))
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class'],
    })
    return () => observer.disconnect()
  }, [])

  // ForceGraph2D defaults to the window size, which overflows the card.
  // Keyed on state.kind because the container is behind the loading skeleton
  // on first render — without the re-run the observer never attaches and the
  // canvas stays 0px wide.
  useEffect(() => {
    const element = containerRef.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => {
      setSize({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      })
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [state.kind])

  // The defaults pack ~90 nodes tightly enough that labels collide. Push the
  // nodes apart and lengthen the links so the chain is readable.
  useEffect(() => {
    if (state.kind !== 'loaded') return
    const forceGraph = graphRef.current
    forceGraph?.d3Force('charge')?.strength?.(-180)
    forceGraph?.d3Force('link')?.distance?.(45)
    // The warmup ticks already ran with the default forces, so the new ones
    // only take effect once the simulation is restarted.
    forceGraph?.d3ReheatSimulation?.()
  }, [state.kind, includeNeighbors])

  const graph = state.kind === 'loaded' ? state.graph : null

  // Cloned because force-graph mutates what it is handed; without this the
  // simulation would write x/y into the objects held in React state.
  const graphData = useMemo(
    () => ({
      nodes: (graph?.nodes ?? []).map((node) => ({ ...node })) as ForceNode[],
      links: (graph?.edges ?? []).map((edge) => ({ ...edge })) as ForceLink[],
    }),
    [graph],
  )

  /** Node ids one hop from the hovered node, plus the node itself. */
  const highlighted = useMemo(() => {
    if (!hovered || !graph) return null
    const ids = new Set<string>([hovered])
    for (const edge of graph.edges) {
      if (edge.source === hovered) ids.add(edge.target)
      if (edge.target === hovered) ids.add(edge.source)
    }
    return ids
  }, [hovered, graph])

  const drawNode = useCallback(
    (node: ForceNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      if (!palette) return
      const radius = nodeRadius(node)
      const dimmed = highlighted !== null && !highlighted.has(node.id)
      ctx.globalAlpha = dimmed ? 0.15 : 1

      ctx.beginPath()
      ctx.arc(node.x ?? 0, node.y ?? 0, radius, 0, 2 * Math.PI)
      ctx.fillStyle = palette.node(node.type, node.blocked)
      ctx.fill()
      if (selected?.id === node.id) {
        ctx.strokeStyle = palette.text
        ctx.lineWidth = 2 / globalScale
        ctx.stroke()
      }

      // Labels only once zoomed in enough to read them, and always for the
      // nodes that carry the explanation.
      const alwaysLabel = radius >= 6 || node.blocked
      if (globalScale > 1.4 || alwaysLabel) {
        const fontSize = Math.max(3, 11 / globalScale)
        ctx.font = `${fontSize}px Inter Variable, sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'top'
        ctx.fillStyle = palette.text
        const text =
          node.label.length > 28 ? `${node.label.slice(0, 27)}…` : node.label
        ctx.fillText(text, node.x ?? 0, (node.y ?? 0) + radius + 1.5)
      }
      ctx.globalAlpha = 1
    },
    [palette, selected, highlighted],
  )

  // Custom-drawn nodes get no hit area for free; without this, clicking a
  // node does nothing.
  const paintPointerArea = useCallback(
    (node: ForceNode, color: string, ctx: CanvasRenderingContext2D) => {
      ctx.fillStyle = color
      ctx.beginPath()
      ctx.arc(node.x ?? 0, node.y ?? 0, nodeRadius(node) + 2, 0, 2 * Math.PI)
      ctx.fill()
    },
    [],
  )

  const linkColor = useCallback(
    (link: ForceLink) => {
      if (!palette) return 'transparent'
      if (
        highlighted !== null &&
        !(
          highlighted.has(endpointId(link.source)) &&
          highlighted.has(endpointId(link.target))
        )
      ) {
        return palette.edge
      }
      return link.derived ? palette.edgeDerived : palette.edge
    },
    [palette, highlighted],
  )

  if (state.kind === 'loading') {
    return (
      <div className="flex flex-col gap-3">
        <Skeleton className="h-6 w-64" />
        <Skeleton className="h-[32rem] w-full" />
      </div>
    )
  }

  if (state.kind === 'error') {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Couldn't load the graph</CardTitle>
          <CardDescription>{state.message}</CardDescription>
        </CardHeader>
      </Card>
    )
  }

  const blockedCount = state.graph.nodes.filter((node) => node.blocked).length
  const legendTypes = TYPE_ORDER.filter((type) => state.graph.counts[type])
  // Optional in the schema because the backend defaults it to an empty list.
  const notes = state.graph.notes ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h2 className="text-lg font-semibold">Knowledge graph</h2>
          <p className="text-sm text-muted-foreground">
            {state.graph.nodes.length} nodes · {state.graph.edges.length} edges ·{' '}
            {blockedCount} exercise{blockedCount === 1 ? '' : 's'} blocked by a
            safety rule. Dashed edges are derived, not stored in Neo4j.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setIncludeNeighbors((value) => !value)}
          >
            {includeNeighbors ? 'Hide catalog detail' : 'Show catalog detail'}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => graphRef.current?.zoomToFit(400, 40)}
          >
            Fit
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_20rem]">
        {/* Selection runs off the node under the cursor at press time rather
            than force-graph's onNodeClick alone: a click that shifts by even
            a pixel is treated as a node drag and never reaches onNodeClick,
            which is easy to do on a trackpad. Capture phase, because
            force-graph stops propagation on the canvas. */}
        <div
          ref={containerRef}
          onPointerDownCapture={() =>
            setSelected(
              state.graph.nodes.find((node) => node.id === hoveredRef.current) ??
                null,
            )
          }
          className="h-[32rem] w-full overflow-hidden rounded-lg border bg-card"
        >
          {palette && size.width > 0 ? (
            <ForceGraph2D
              ref={graphRef as never}
              graphData={graphData}
              width={size.width}
              height={size.height}
              backgroundColor={palette.canvas}
              nodeCanvasObject={drawNode}
              nodePointerAreaPaint={paintPointerArea}
              nodeLabel={(node: ForceNode) => `${TYPE_LABELS[node.type] ?? node.type}: ${node.label}`}
              linkColor={linkColor}
              linkWidth={(link: ForceLink) => (link.derived ? 1.5 : 1)}
              linkLineDash={(link: ForceLink) => (link.derived ? [3, 2] : null)}
              linkDirectionalArrowLength={3}
              linkDirectionalArrowRelPos={1}
              // Node dragging is left enabled even though this view never
              // needs it: `enableNodeDrag={false}` also strips the pointer
              // handlers force-graph uses to detect clicks, which silently
              // kills the inspector.
              onNodeClick={(node: ForceNode) => setSelected({ ...node })}
              onNodeHover={(node: ForceNode | null) => {
                hoveredRef.current = node?.id ?? null
                setHovered(node?.id ?? null)
              }}
              onEngineStop={() => graphRef.current?.zoomToFit(400, 60)}
              // Run the whole layout off-screen, then freeze it. A graph that
              // keeps drifting is hard to read and hard to click, and fitting
              // a still-collapsed layout zooms to a single dot.
              warmupTicks={200}
              cooldownTicks={150}
            />
          ) : null}
        </div>

        <div className="flex flex-col gap-4">
          <NodeInspector node={selected} />
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Legend</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-1.5">
              {legendTypes.map((type) => (
                <div key={type} className="flex items-center gap-2 text-xs">
                  <span
                    aria-hidden
                    className="size-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: `var(--graph-${LEGEND_VARS[type]})` }}
                  />
                  <span className="text-muted-foreground">
                    {TYPE_LABELS[type] ?? type}
                  </span>
                  <span className="ml-auto tabular-nums">
                    {state.graph.counts[type]}
                  </span>
                </div>
              ))}
              <div className="mt-1 flex items-center gap-2 border-t pt-2 text-xs">
                <span
                  aria-hidden
                  className="size-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: 'var(--graph-blocked)' }}
                />
                <span className="text-muted-foreground">Blocked exercise</span>
                <span className="ml-auto tabular-nums">{blockedCount}</span>
              </div>
            </CardContent>
          </Card>
        </div>
      </div>

      {notes.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">How this was derived</CardTitle>
            <CardDescription>
              The resolver and safety-rule steps that produced the dashed edges.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-1.5">
              {notes.map((note) => (
                <li key={note} className="text-xs text-muted-foreground">
                  {note}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}

/** Legend swatch tokens, keyed by node type (see index.css `--graph-*`). */
const LEGEND_VARS: Record<string, string> = {
  Member: 'member',
  Goal: 'goal',
  Injury: 'injury',
  Condition: 'condition',
  AnatomicalStructure: 'anatomy',
  SafetyRule: 'rule',
  Joint: 'joint',
  Exercise: 'exercise',
  MuscleGroup: 'muscle',
  Equipment: 'equipment',
  MovementPattern: 'pattern',
}

function NodeInspector({ node }: { node: GraphNode | null }) {
  if (!node) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Inspector</CardTitle>
          <CardDescription>
            Click any node to see what the graph holds about it.
          </CardDescription>
        </CardHeader>
      </Card>
    )
  }

  const entries = Object.entries(node.properties ?? {}).sort(([a], [b]) =>
    a.localeCompare(b),
  )

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{TYPE_LABELS[node.type] ?? node.type}</Badge>
          {node.blocked ? <Badge variant="destructive">blocked</Badge> : null}
        </div>
        <CardTitle className="text-sm break-words">{node.label}</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="flex flex-col gap-2">
          {entries.map(([key, value]) => (
            <div key={key} className="flex flex-col gap-0.5">
              <dt className="text-xs font-medium text-muted-foreground">{key}</dt>
              <dd className="text-xs break-words">{formatPropertyValue(value)}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  )
}
