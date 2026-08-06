import { useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { MOCK_COACH } from '@/lib/session'

interface LoginScreenProps {
  onLogin: () => void
}

/**
 * Mock sign-in screen. Any credentials (or none) log in as the hardcoded
 * coach — the spec explicitly allows mock auth.
 */
export function LoginScreen({ onLogin }: LoginScreenProps) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  return (
    <main className="flex min-h-dvh items-center justify-center bg-background p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>KG Coach Dashboard</CardTitle>
          <CardDescription>
            Mock sign-in — any credentials work. You'll be signed in as coach{' '}
            <code className="text-xs">{MOCK_COACH.coachId}</code>.
          </CardDescription>
        </CardHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            onLogin()
          }}
        >
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                placeholder="coach@example.com"
                autoComplete="username"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                placeholder="anything"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
          </CardContent>
          <CardFooter className="pt-6">
            <Button type="submit" className="w-full">
              Sign in as Coach {MOCK_COACH.name}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </main>
  )
}
