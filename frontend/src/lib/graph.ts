import type { components } from '@/lib/api-types'

// Knowledge-graph contract — generated from the backend's Pydantic schemas
// (openapi.json → src/lib/api-types.ts via `npm run gen:api`).
export type MemberSubgraph = components['schemas']['MemberSubgraphResponse']
export type GraphNode = components['schemas']['GraphNode']
export type GraphEdge = components['schemas']['GraphEdge']

/**
 * Fetch the member-centric subgraph behind the coach's safety decisions.
 *
 * @param memberId - Member identifier, e.g. `mbr_01HX9JORDAN`.
 * @param includeNeighbors - Expand each exercise to its muscle groups,
 *   equipment, and movement patterns. Off gives a smaller, more legible graph.
 * @returns Nodes and edges where every edge endpoint resolves to a node.
 * @throws Error if the request fails or returns a non-2xx status.
 */
export async function fetchMemberSubgraph(
  memberId: string,
  includeNeighbors: boolean,
): Promise<MemberSubgraph> {
  const query = new URLSearchParams({ include_neighbors: String(includeNeighbors) })
  const res = await fetch(
    `/api/graph/member/${encodeURIComponent(memberId)}?${query}`,
  )
  if (!res.ok) {
    throw new Error(
      res.status === 404
        ? `Member ${memberId} not found`
        : res.status === 503
          ? 'The knowledge graph is unavailable — is Neo4j running?'
          : `API returned ${res.status}`,
    )
  }
  return (await res.json()) as MemberSubgraph
}

/** CSS custom property holding each node type's colour. */
const NODE_COLOR_VARS: Record<string, string> = {
  Member: '--graph-member',
  Injury: '--graph-injury',
  Condition: '--graph-condition',
  SafetyRule: '--graph-rule',
  AnatomicalStructure: '--graph-anatomy',
  Joint: '--graph-joint',
  Exercise: '--graph-exercise',
  Goal: '--graph-goal',
  MuscleGroup: '--graph-muscle',
  Equipment: '--graph-equipment',
  MovementPattern: '--graph-pattern',
}

/** Node radius by type — the explanatory chain outranks catalog context. */
const NODE_SIZES: Record<string, number> = {
  Member: 9,
  Injury: 7,
  Condition: 7,
  SafetyRule: 6,
  Joint: 7,
  Exercise: 5,
}
const DEFAULT_NODE_SIZE = 3.5

/** Human-readable names for the node types, for the legend. */
export const TYPE_LABELS: Record<string, string> = {
  Member: 'Member',
  Injury: 'Injury',
  Condition: 'Condition',
  SafetyRule: 'Safety rule',
  AnatomicalStructure: 'Anatomy',
  Joint: 'Joint',
  Exercise: 'Exercise',
  Goal: 'Goal',
  MuscleGroup: 'Muscle group',
  Equipment: 'Equipment',
  MovementPattern: 'Movement pattern',
}

/** The order the legend lists types in: story first, catalog context last. */
export const TYPE_ORDER = [
  'Member',
  'Goal',
  'Injury',
  'Condition',
  'AnatomicalStructure',
  'SafetyRule',
  'Joint',
  'Exercise',
  'MuscleGroup',
  'Equipment',
  'MovementPattern',
]

export interface GraphPalette {
  node: (type: string, blocked: boolean) => string
  edge: string
  edgeDerived: string
  canvas: string
  text: string
}

/**
 * Read the graph colours off the document.
 *
 * Canvas needs concrete colour values — it cannot resolve `var(--x)` the way
 * CSS can — so the tokens are looked up here rather than used directly. Call
 * this again when the theme changes, or the graph keeps the old palette.
 *
 * @returns Lookups returning resolved colour strings.
 */
export function readPalette(): GraphPalette {
  const styles = getComputedStyle(document.documentElement)
  const read = (name: string, fallback: string): string =>
    styles.getPropertyValue(name).trim() || fallback

  const blocked = read('--graph-blocked', '#dc2626')
  const unknown = read('--graph-unknown', '#9ca3af')
  const colors = new Map<string, string>()
  for (const [type, variable] of Object.entries(NODE_COLOR_VARS)) {
    colors.set(type, read(variable, unknown))
  }

  return {
    node: (type, isBlocked) =>
      isBlocked ? blocked : (colors.get(type) ?? unknown),
    edge: read('--graph-edge', '#d1d5db'),
    edgeDerived: read('--graph-edge-derived', '#dc2626'),
    canvas: read('--graph-canvas', '#ffffff'),
    text: read('--foreground', '#111827'),
  }
}

/**
 * Radius for a node, in graph units.
 *
 * @param node - The node being drawn.
 * @returns The radius; blocked exercises are enlarged so a refusal is
 *   findable without hunting for a red dot.
 */
export function nodeRadius(node: GraphNode): number {
  const base = NODE_SIZES[node.type] ?? DEFAULT_NODE_SIZE
  return node.blocked ? base + 1.5 : base
}

/**
 * Format a node property value for the inspector panel.
 *
 * @param value - Any JSON value from the node's `properties` map.
 * @returns A single-line string; arrays become comma-separated lists.
 */
export function formatPropertyValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}
