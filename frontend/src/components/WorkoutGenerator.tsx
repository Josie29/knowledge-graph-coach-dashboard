import { useRef, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import {
  generateWorkout,
  type ExcludedExercise,
  type PlanSection,
  type PlannedExercise,
  type WorkoutPlan,
} from '@/lib/api'

interface WorkoutGeneratorProps {
  memberId: string
}

/**
 * Starter prompts for the coach prompt field. Each one exercises a different
 * part of the constraint pipeline — muscle/region resolution, the injury
 * traversal through the anatomy hierarchy, equipment substitution, an explicit
 * exclusion, and a movement-pattern veto — so the trace shows something
 * different every time. Wording stays free of durations; the time window is
 * its own input.
 */
const SUGGESTED_PROMPTS = [
  'Full-body session with isolation work around the pecs',
  "Lower-body strength that won't aggravate her left knee",
  'Upper-body push and pull — no barbell, only dumbbells and a kettlebell',
  'Posterior chain and glutes, but exclude deadlifts',
  'Low-impact conditioning and core work, nothing with jumping',
]

type GeneratorState =
  | { kind: 'idle' }
  | { kind: 'loading'; plan?: WorkoutPlan }
  | { kind: 'loaded'; plan: WorkoutPlan }
  | { kind: 'error'; message: string; plan?: WorkoutPlan }

/** "3 × 10 reps · 75s rest" / "3 × 40s hold · 45s rest" line for one exercise. */
function prescription(exercise: PlannedExercise): string {
  const work =
    exercise.reps != null
      ? `${exercise.sets} × ${exercise.reps} reps`
      : `${exercise.sets} × ${exercise.duration_seconds ?? 0}s`
  return `${work} · ${exercise.rest_seconds}s rest`
}

function ExerciseRow({ exercise }: { exercise: PlannedExercise }) {
  const poolNotes = exercise.pool_notes ?? []
  const firedRules = exercise.fired_rules ?? []
  const downranked = poolNotes.some((note) => note.includes('preference'))
  return (
    <li className="rounded-md border px-3 py-2">
      <details>
        <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <span className="text-sm font-medium">
            {exercise.name}
            {downranked && (
              <Badge variant="secondary" className="ml-2 align-middle text-[10px]">
                down-ranked
              </Badge>
            )}
          </span>
          <span className="text-xs text-muted-foreground">{prescription(exercise)}</span>
        </summary>
        <div className="mt-2 flex flex-col gap-1 border-t pt-2 text-xs text-muted-foreground">
          <p>
            <span className="font-medium text-foreground">Why chosen: </span>
            {exercise.why_chosen}
          </p>
          {firedRules.map((rule) => (
            <p key={rule.rule_id}>
              <span className="font-medium text-foreground">
                {rule.decision} · {rule.rule_id}:{' '}
              </span>
              {rule.rationale}
              {rule.anatomy_path && <> — {rule.anatomy_path.description}</>}
            </p>
          ))}
          {poolNotes.map((note) => (
            <p key={note}>{note}</p>
          ))}
          {exercise.coach_note && (
            <p>
              <span className="font-medium text-foreground">Coach note: </span>
              {exercise.coach_note}
            </p>
          )}
        </div>
      </details>
    </li>
  )
}

function SectionBlock({ section }: { section: PlanSection }) {
  if (section.exercises.length === 0) return null
  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold">{section.title}</h3>
      <ul className="flex flex-col gap-2">
        {section.exercises.map((exercise, index) => (
          <ExerciseRow key={`${exercise.exercise_id}-${index}`} exercise={exercise} />
        ))}
      </ul>
    </div>
  )
}

function ExclusionItem({ exclusion }: { exclusion: ExcludedExercise }) {
  return (
    <li className="text-xs">
      <span className="font-medium">{exclusion.name}</span>
      <span className="text-muted-foreground"> — {exclusion.reason}</span>
      {exclusion.path && (
        <div className="text-muted-foreground/80">path: {exclusion.path.description}</div>
      )}
      {(exclusion.alternatives ?? []).length > 0 && (
        <div className="text-muted-foreground/80">
          alternatives: {(exclusion.alternatives ?? []).map((alt) => alt.name).join(', ')}
        </div>
      )}
    </li>
  )
}

function PlanView({ plan }: { plan: WorkoutPlan }) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-base font-semibold">{plan.title}</h3>
          <Badge variant="outline">
            ~{plan.estimated_duration_minutes} of {plan.time_window_minutes} min
          </Badge>
          <Badge variant="outline">{plan.pool_size} safe exercises in pool</Badge>
        </div>
        <p className="text-sm text-muted-foreground">{plan.rationale}</p>
      </div>

      <SectionBlock section={plan.warmup} />
      <SectionBlock section={plan.main} />
      <SectionBlock section={plan.cooldown} />

      {plan.filtered_out_for_safety.length > 0 && (
        <details className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">
            Filtered out for safety ({plan.filtered_out_for_safety.length})
          </summary>
          <ul className="mt-2 flex flex-col gap-2 border-t pt-2">
            {plan.filtered_out_for_safety.map((exclusion) => (
              <ExclusionItem key={exclusion.exercise_id} exclusion={exclusion} />
            ))}
          </ul>
        </details>
      )}

      {plan.other_exclusions.length > 0 && (
        <details className="rounded-md border px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">
            Not feasible or excluded ({plan.other_exclusions.length})
          </summary>
          <ul className="mt-2 flex flex-col gap-2 border-t pt-2">
            {plan.other_exclusions.map((exclusion) => (
              <ExclusionItem key={exclusion.exercise_id} exclusion={exclusion} />
            ))}
          </ul>
        </details>
      )}

      <details className="rounded-md border px-3 py-2">
        <summary className="cursor-pointer text-sm font-medium">
          Constraint resolution trace ({plan.resolution_notes.length})
        </summary>
        <ul className="mt-2 flex flex-col gap-1 border-t pt-2 text-xs text-muted-foreground">
          {plan.resolution_notes.map((note, index) => (
            <li key={index}>{note}</li>
          ))}
        </ul>
      </details>
    </div>
  )
}

/**
 * The Workout Generator panel: prompt + time window form, the structured plan
 * with per-exercise provenance and safety exclusions, and an adjustment input
 * that merges follow-up constraints into the plan's constraint set.
 */
export function WorkoutGenerator({ memberId }: WorkoutGeneratorProps) {
  const [prompt, setPrompt] = useState('')
  const [timeWindow, setTimeWindow] = useState(50)
  const [adjustment, setAdjustment] = useState('')
  const [state, setState] = useState<GeneratorState>({ kind: 'idle' })
  const promptRef = useRef<HTMLTextAreaElement | null>(null)

  const busy = state.kind === 'loading'
  const currentPlan =
    state.kind === 'loaded' ? state.plan : state.kind !== 'idle' ? state.plan : undefined

  async function run(message: string, priorPlan?: WorkoutPlan) {
    setState({ kind: 'loading', plan: priorPlan })
    try {
      const plan = await generateWorkout({
        member_id: memberId,
        prompt: message,
        time_window_minutes: timeWindow,
        prior_constraints: priorPlan ? priorPlan.constraints_used : null,
      })
      setState({ kind: 'loaded', plan })
      setAdjustment('')
    } catch (err: unknown) {
      setState({
        kind: 'error',
        message: err instanceof Error ? err.message : 'Workout generation failed',
        plan: priorPlan,
      })
    }
  }

  return (
    <Card className="min-h-64">
      <CardHeader>
        <CardTitle>Workout Generator</CardTitle>
        <CardDescription>
          Generate a safe, personalized workout from a prompt and time window. Member
          equipment, injuries, and dislikes are applied automatically from the knowledge
          graph.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            if (prompt.trim()) void run(prompt.trim())
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="workout-prompt">Coach prompt</Label>
            <Textarea
              id="workout-prompt"
              ref={promptRef}
              placeholder="e.g. Lower body strength session, easy on the knee"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              disabled={busy}
            />
            <div className="flex flex-wrap gap-1.5 pt-0.5" aria-label="Suggested prompts">
              {SUGGESTED_PROMPTS.map((suggestion) => (
                <Button
                  key={suggestion}
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-7 rounded-full px-3 text-xs"
                  disabled={busy}
                  onClick={() => {
                    setPrompt(suggestion)
                    promptRef.current?.focus()
                  }}
                >
                  {suggestion}
                </Button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="workout-window">Time window (min)</Label>
              <Input
                id="workout-window"
                type="number"
                min={5}
                max={180}
                className="w-28"
                value={timeWindow}
                onChange={(event) => setTimeWindow(Number(event.target.value))}
                disabled={busy}
              />
            </div>
            <Button type="submit" disabled={busy || !prompt.trim()}>
              {busy && !currentPlan ? 'Generating…' : 'Generate plan'}
            </Button>
          </div>
        </form>

        {state.kind === 'error' && (
          <p role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {state.message}
          </p>
        )}

        {busy && (
          <div className="flex flex-col gap-2" aria-label="Generating plan">
            <Skeleton className="h-5 w-48" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
            <p className="text-xs text-muted-foreground">
              Resolving constraints, traversing the safety graph, composing the plan…
            </p>
          </div>
        )}

        {!busy && currentPlan && (
          <>
            <Separator />
            <PlanView plan={currentPlan} />
            <form
              className="flex flex-col gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                if (adjustment.trim()) void run(adjustment.trim(), currentPlan)
              }}
            >
              <Label htmlFor="workout-adjustment">Adjust this plan</Label>
              <div className="flex flex-wrap gap-2">
                <Input
                  id="workout-adjustment"
                  className="min-w-56 flex-1"
                  placeholder='e.g. "exclude deadlifts" or "no barbell, only dumbbells and a kettlebell"'
                  value={adjustment}
                  onChange={(event) => setAdjustment(event.target.value)}
                  disabled={busy}
                />
                <Button type="submit" variant="secondary" disabled={busy || !adjustment.trim()}>
                  Regenerate
                </Button>
              </div>
            </form>
          </>
        )}
      </CardContent>
    </Card>
  )
}
