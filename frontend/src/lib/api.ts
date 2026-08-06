export interface Goal {
  id: string
  text: string
  priority: number
  target_date: string | null
}

export interface MemberProfile {
  id: string
  name: string
  age: number
  sex: string
  tier: string
  member_since: string
  coach_id: string
  timezone: string
}

export interface MemberResponse {
  profile: MemberProfile
  goals: Goal[]
}

/** The single seeded member until member selection lands in a later issue. */
export const JORDAN_MEMBER_ID = 'mbr_01HX9JORDAN'

/**
 * Fetch a member's profile header and goals from the API.
 *
 * @param memberId - Member identifier, e.g. `mbr_01HX9JORDAN`.
 * @returns The member's profile and goals.
 * @throws Error if the request fails or returns a non-2xx status.
 */
export async function fetchMember(memberId: string): Promise<MemberResponse> {
  const res = await fetch(`/api/members/${encodeURIComponent(memberId)}`)
  if (!res.ok) {
    throw new Error(
      res.status === 404 ? `Member ${memberId} not found` : `API returned ${res.status}`,
    )
  }
  return (await res.json()) as MemberResponse
}
