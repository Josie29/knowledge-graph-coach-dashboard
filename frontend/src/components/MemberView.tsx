import { lazy, Suspense, useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { Copilot } from '@/components/Copilot'
import { WorkoutGenerator } from '@/components/WorkoutGenerator'
import { fetchMember, JORDAN_MEMBER_ID, type Goal, type MemberResponse } from '@/lib/api'

type MemberState =
  | { kind: 'loading' }
  | { kind: 'loaded'; member: MemberResponse }
  | { kind: 'error'; message: string }

/** Which member-scoped panel to show below the profile. */
export type MemberPanel = 'dashboard' | 'graph'

// Lazy: the force-graph renderer is ~700 kB and this is the secondary tab, so
// the dashboard's first paint should not pay for it.
const GraphView = lazy(() =>
  import('@/components/GraphView').then((module) => ({ default: module.GraphView })),
)

/**
 * Format an ISO date string as a short month-year label, e.g. "Sep 2024".
 *
 * @param iso - ISO 8601 date string (YYYY-MM-DD).
 * @returns Localized "Mon YYYY" label, or the raw string if unparsable.
 */
function formatMonthYear(iso: string): string {
  const date = new Date(`${iso}T00:00:00`)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
}

function GoalItem({ goal }: { goal: Goal }) {
  return (
    <li className="flex items-start gap-2 text-sm">
      <Badge variant={goal.priority === 1 ? 'default' : 'secondary'} className="mt-px shrink-0">
        P{goal.priority}
      </Badge>
      <span>
        {goal.text}
        {goal.target_date && (
          <span className="text-muted-foreground"> · by {formatMonthYear(goal.target_date)}</span>
        )}
      </span>
    </li>
  )
}

/**
 * The coach's member view: profile header for the selected member plus either
 * the coaching tools or the knowledge-graph explorer.
 *
 * The profile stays visible across both panels because both are scoped to this
 * member. The dashboard chrome around it lives in DashboardHeader, shared with
 * the Traces view.
 */
export function MemberView({ panel }: { panel: MemberPanel }) {
  const [state, setState] = useState<MemberState>({ kind: 'loading' })

  useEffect(() => {
    fetchMember(JORDAN_MEMBER_ID)
      .then((member) => setState({ kind: 'loaded', member }))
      .catch((err: unknown) => {
        setState({
          kind: 'error',
          message: err instanceof Error ? err.message : 'Failed to load member',
        })
      })
  }, [])

  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-6 px-4 py-6 sm:px-6">
      <section aria-label="Member profile">
        {state.kind === 'loading' ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-8 w-56" />
            <Skeleton className="h-4 w-80" />
            <Skeleton className="h-4 w-64" />
          </div>
        ) : state.kind === 'error' ? (
          <Card>
            <CardHeader>
              <CardTitle>Couldn't load member</CardTitle>
              <CardDescription>{state.message}</CardDescription>
            </CardHeader>
          </Card>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-semibold">{state.member.profile.name}</h1>
              <Badge variant="outline">{state.member.profile.tier}</Badge>
            </div>
            <p className="text-sm text-muted-foreground">
              Age {state.member.profile.age} · Member since{' '}
              {formatMonthYear(state.member.profile.member_since)} ·{' '}
              {state.member.profile.timezone}
            </p>
            <div className="flex flex-col gap-2">
              <h2 className="text-sm font-medium">Goals</h2>
              <ul className="flex flex-col gap-2">
                {state.member.goals.map((goal) => (
                  <GoalItem key={goal.id} goal={goal} />
                ))}
              </ul>
            </div>
          </div>
        )}
      </section>

      <Separator />

      {panel === 'dashboard' ? (
        <section
          aria-label="Coaching tools"
          className="grid flex-1 grid-cols-1 gap-6 lg:grid-cols-2"
        >
          <WorkoutGenerator memberId={JORDAN_MEMBER_ID} />
          <Copilot memberId={JORDAN_MEMBER_ID} />
        </section>
      ) : (
        <section aria-label="Knowledge graph" className="flex flex-1 flex-col">
          <Suspense
            fallback={
              <div className="flex flex-col gap-3">
                <Skeleton className="h-6 w-64" />
                <Skeleton className="h-[32rem] w-full" />
              </div>
            }
          >
            <GraphView memberId={JORDAN_MEMBER_ID} />
          </Suspense>
        </section>
      )}
    </main>
  )
}
