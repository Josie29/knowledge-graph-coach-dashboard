import { useState } from 'react'
import { LoginScreen } from '@/components/LoginScreen'
import { MemberView } from '@/components/MemberView'
import {
  clearSession,
  loadSession,
  MOCK_COACH,
  saveSession,
  type CoachSession,
} from '@/lib/session'

/**
 * Dashboard shell: mock coach login gating the member view.
 * The session persists in localStorage so a refresh stays signed in.
 */
function App() {
  const [session, setSession] = useState<CoachSession | null>(() => loadSession())

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
    <MemberView
      session={session}
      onSignOut={() => {
        clearSession()
        setSession(null)
      }}
    />
  )
}

export default App
