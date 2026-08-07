import { useState } from 'react'
import { DashboardHeader, type DashboardView } from '@/components/DashboardHeader'
import { LoginScreen } from '@/components/LoginScreen'
import { MemberView } from '@/components/MemberView'
import { TracesView } from '@/components/TracesView'
import {
  clearSession,
  loadSession,
  MOCK_COACH,
  saveSession,
  type CoachSession,
} from '@/lib/session'

/**
 * Dashboard shell: mock coach login gating the member and traces views.
 * The session persists in localStorage so a refresh stays signed in.
 */
function App() {
  const [session, setSession] = useState<CoachSession | null>(() => loadSession())
  const [view, setView] = useState<DashboardView>('dashboard')

  if (!session) {
    return (
      <LoginScreen
        onLogin={() => {
          saveSession(MOCK_COACH)
          setSession(MOCK_COACH)
        }}
      />
    )
  }

  return (
    <div className="flex min-h-dvh flex-col bg-background">
      <DashboardHeader
        session={session}
        view={view}
        onViewChange={setView}
        onSignOut={() => {
          clearSession()
          setSession(null)
        }}
      />
      {view === 'traces' ? <TracesView /> : <MemberView panel={view} />}
    </div>
  )
}

export default App
