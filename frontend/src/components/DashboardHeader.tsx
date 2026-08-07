import { Button } from '@/components/ui/button'
import { HealthIndicator } from '@/components/HealthIndicator'
import { cn } from '@/lib/utils'
import type { CoachSession } from '@/lib/session'

/** Top-level screens the dashboard can show. */
export type DashboardView = 'member' | 'traces'

const VIEWS: ReadonlyArray<{ id: DashboardView; label: string }> = [
  { id: 'member', label: 'Member' },
  { id: 'traces', label: 'Traces' },
]

interface DashboardHeaderProps {
  session: CoachSession
  view: DashboardView
  onViewChange: (view: DashboardView) => void
  onSignOut: () => void
}

/**
 * Shared dashboard chrome: product name, health, view switcher, sign-out.
 *
 * The switcher is a plain button group rather than a router: the app has two
 * screens and no deep-linking requirement, so this matches the existing
 * conditional-render structure instead of adding a routing dependency.
 */
export function DashboardHeader({
  session,
  view,
  onViewChange,
  onSignOut,
}: DashboardHeaderProps) {
  return (
    <header className="border-b">
      <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-4">
          <span className="text-sm font-semibold">KG Coach Dashboard</span>
          <HealthIndicator />
        </div>
        <nav aria-label="Dashboard sections" className="flex items-center gap-1">
          {VIEWS.map(({ id, label }) => (
            <Button
              key={id}
              variant="ghost"
              size="sm"
              aria-current={view === id ? 'page' : undefined}
              onClick={() => onViewChange(id)}
              className={cn(
                'text-muted-foreground',
                view === id && 'bg-muted text-foreground',
              )}
            >
              {label}
            </Button>
          ))}
        </nav>
        <div className="flex items-center gap-3">
          <span className="text-sm text-muted-foreground">
            Coach {session.name} <code className="text-xs">({session.coachId})</code>
          </span>
          <Button variant="outline" size="sm" onClick={onSignOut}>
            Sign out
          </Button>
        </div>
      </div>
    </header>
  )
}
