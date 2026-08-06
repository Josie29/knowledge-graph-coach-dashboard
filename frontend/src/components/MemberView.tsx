import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { HealthIndicator } from '@/components/HealthIndicator'
import { WorkoutGenerator } from '@/components/WorkoutGenerator'
import { fetchMember, JORDAN_MEMBER_ID, type Goal, type MemberResponse } from '@/lib/api'
import type { CoachSession } from '@/lib/session'

interface MemberViewProps {
  session: CoachSession
  onSignOut: () => void
}

type MemberState =
  | { kind: 'loading' }
  | { kind: 'loaded'; member: MemberResponse }
  | { kind: 'error'; message: string }

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

function PlaceholderPanel({ title, description, hint }: { title: string; description: string; hint: string }) {
  return (
    <Card className="min-h-64">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-1 items-center justify-center rounded-lg">
        <p className="rounded-lg border border-dashed px-6 py-10 text-center text-sm text-muted-foreground">
          {hint}
        </p>
      </CardContent>
    </Card>
  )
}

/**
 * The coach's member view: profile header for the selected member plus the
 * Workout Generator and AI Copilot panels (placeholders until later issues).
 */
export function MemberView({ session, onSignOut }: MemberViewProps) {
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
    <div className="flex min-h-dvh flex-col bg-background">
      <header className="border-b">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-4">
            <span className="text-sm font-semibold">KG Coach Dashboard</span>
            <HealthIndicator />
          </div>
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">
              Coach {session.name}{' '}
              <code className="text-xs">({session.coachId})</code>
            </span>
            <Button variant="outline" size="sm" onClick={onSignOut}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

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

        <section
          aria-label="Coaching tools"
          className="grid flex-1 grid-cols-1 gap-6 lg:grid-cols-2"
        >
          <WorkoutGenerator memberId={JORDAN_MEMBER_ID} />
          <PlaceholderPanel
            title="AI Copilot"
            description="Ask questions about this member's context, grounded in the knowledge graph."
            hint="Coming soon — copilot chat lands in a later issue."
          />
        </section>
      </main>
    </div>
  )
}
