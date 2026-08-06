export interface CoachSession {
  coachId: string
  name: string
}

/** Hardcoded coach used by the mock login — the spec explicitly allows mock auth. */
export const MOCK_COACH: CoachSession = {
  coachId: 'coach_01HXSAM',
  name: 'Sam',
}

const STORAGE_KEY = 'kg-coach-session'

/**
 * Load the persisted coach session from localStorage.
 *
 * @returns The stored session, or null if absent or malformed.
 */
export function loadSession(): CoachSession | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<CoachSession>
    if (typeof parsed.coachId !== 'string' || typeof parsed.name !== 'string') {
      return null
    }
    return { coachId: parsed.coachId, name: parsed.name }
  } catch {
    // Corrupt JSON or storage unavailable — treat as signed out.
    return null
  }
}

/**
 * Persist the coach session to localStorage.
 *
 * @param session - The session to store.
 */
export function saveSession(session: CoachSession): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
}

/** Remove the persisted coach session. */
export function clearSession(): void {
  localStorage.removeItem(STORAGE_KEY)
}
