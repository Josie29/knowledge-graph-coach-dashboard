import { useEffect, useRef, useState } from 'react'
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
import { Skeleton } from '@/components/ui/skeleton'
import { CopilotChart } from '@/components/CopilotChart'
import {
  createCopilotAgent,
  fetchContextSlice,
  parseCopilotAnswer,
  type ContextSlice,
  type CopilotAnswer,
} from '@/lib/copilot'
import { cn } from '@/lib/utils'

interface CopilotProps {
  memberId: string
}

interface UserTurn {
  role: 'user'
  text: string
}

interface AssistantTurn {
  role: 'assistant'
  status: 'running' | 'done' | 'error'
  activity: string[]
  answer?: CopilotAnswer
  error?: string
}

type Turn = UserTurn | AssistantTurn

const MEMBER_PROMPTS = [
  'Show me the brief',
  "How's adherence trending?",
  'Sleep this week',
  'What changed since last week?',
]
const CHART_PROMPTS = ['Plot adherence trend', 'Show message pattern', 'Compare last 4 weeks']

type ChurnLevel = NonNullable<ContextSlice['churn_risk']>['level']

// Churn is a three-band computed score (see docs/churn-risk-classification.md),
// so both the badge and the brief panel key off the level rather than testing
// for 'elevated' — a moderate member must read as a caution, not an all-clear.
const CHURN_BADGE: Record<ChurnLevel, 'destructive' | 'secondary' | 'outline'> = {
  elevated: 'destructive',
  moderate: 'secondary',
  low: 'outline',
}

const CHURN_PANEL: Record<ChurnLevel, string> = {
  elevated: 'border-destructive/40 bg-destructive/5',
  moderate: 'border-border bg-muted/40',
  low: 'border-border',
}

type Tab = 'chat' | 'brief' | 'history'

/** Tiny markdown-ish renderer: paragraphs, **bold**, and `-` bullet lines. */
function Markdownish({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/)
  return (
    <div className="flex flex-col gap-2 text-sm">
      {blocks.map((block, blockIndex) => {
        const lines = block.split('\n')
        const isList = lines.every((line) => /^\s*[-*•]\s+/.test(line))
        const renderInline = (line: string) =>
          line.split(/(\*\*[^*]+\*\*)/g).map((piece, pieceIndex) =>
            piece.startsWith('**') && piece.endsWith('**') ? (
              <strong key={pieceIndex}>{piece.slice(2, -2)}</strong>
            ) : (
              <span key={pieceIndex}>{piece}</span>
            ),
          )
        if (isList) {
          return (
            <ul key={blockIndex} className="ml-4 list-disc space-y-1">
              {lines.map((line, lineIndex) => (
                <li key={lineIndex}>{renderInline(line.replace(/^\s*[-*•]\s+/, ''))}</li>
              ))}
            </ul>
          )
        }
        return <p key={blockIndex}>{renderInline(block)}</p>
      })}
    </div>
  )
}

function AnswerView({ answer }: { answer: CopilotAnswer }) {
  return (
    <div className="flex flex-col gap-2">
      <Markdownish text={answer.markdown} />
      {answer.kind === 'chart' && <CopilotChart spec={answer.chart} />}
      {answer.citations.length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">Grounded in ({answer.citations.length})</summary>
          <ul className="ml-4 mt-1 list-disc space-y-0.5">
            {answer.citations.map((citation, index) => (
              <li key={index}>{citation}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

function ChatTab({ memberId }: { memberId: string }) {
  const agentRef = useRef<ReturnType<typeof createCopilotAgent> | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const busy = turns.some((turn) => turn.role === 'assistant' && turn.status === 'running')

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [turns])

  function patchAssistant(patch: (turn: AssistantTurn) => AssistantTurn) {
    setTurns((current) => {
      const next = [...current]
      for (let i = next.length - 1; i >= 0; i--) {
        const turn = next[i]
        if (turn.role === 'assistant') {
          next[i] = patch(turn)
          break
        }
      }
      return next
    })
  }

  async function send(text: string) {
    if (busy || !text.trim()) return
    const message = text.trim()
    setInput('')
    agentRef.current ??= createCopilotAgent(memberId)
    const agent = agentRef.current
    setTurns((current) => [
      ...current,
      { role: 'user', text: message },
      { role: 'assistant', status: 'running', activity: [] },
    ])
    agent.addMessage({ id: crypto.randomUUID(), role: 'user', content: message })

    // The typed CopilotAnswer arrives as the args of the run's output tool
    // call ("final_result…"); member_context calls surface as activity notes.
    const toolNames = new Map<string, string>()
    const toolArgs = new Map<string, string>()
    let lastOutputCallId: string | null = null
    try {
      await agent.runAgent(
        {},
        {
          onEvent: ({ event }) => {
            const e = event as unknown as {
              type: string
              toolCallId?: string
              toolCallName?: string
              delta?: string
            }
            if (e.type === 'TOOL_CALL_START' && e.toolCallId && e.toolCallName) {
              toolNames.set(e.toolCallId, e.toolCallName)
              if (e.toolCallName === 'member_context') {
                patchAssistant((turn) => ({
                  ...turn,
                  activity: [...turn.activity, 'Fetching member context from the graph…'],
                }))
              } else {
                lastOutputCallId = e.toolCallId
              }
            } else if (e.type === 'TOOL_CALL_ARGS' && e.toolCallId && e.delta) {
              toolArgs.set(e.toolCallId, (toolArgs.get(e.toolCallId) ?? '') + e.delta)
            }
          },
        },
      )
      const raw = lastOutputCallId ? (toolArgs.get(lastOutputCallId) ?? '') : ''
      const answer = parseCopilotAnswer(raw)
      patchAssistant((turn) =>
        answer
          ? { ...turn, status: 'done', answer }
          : {
              ...turn,
              status: 'error',
              error: 'The copilot returned an unreadable answer — try again.',
            },
      )
    } catch (err: unknown) {
      patchAssistant((turn) => ({
        ...turn,
        status: 'error',
        error: err instanceof Error ? err.message : 'Copilot request failed',
      }))
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap gap-1.5" aria-label="Quick prompts">
        {[...MEMBER_PROMPTS, ...CHART_PROMPTS].map((prompt) => (
          <Button
            key={prompt}
            variant="outline"
            size="sm"
            className="h-7 rounded-full px-3 text-xs"
            disabled={busy}
            onClick={() => void send(prompt)}
          >
            {CHART_PROMPTS.includes(prompt) ? '📈 ' : ''}
            {prompt}
          </Button>
        ))}
      </div>

      <div
        ref={scrollRef}
        className="flex max-h-96 min-h-40 flex-1 flex-col gap-3 overflow-y-auto rounded-md border p-3"
        aria-live="polite"
      >
        {turns.length === 0 && (
          <p className="m-auto text-center text-sm text-muted-foreground">
            Ask about this member — answers are grounded in the member-context graph.
          </p>
        )}
        {turns.map((turn, index) =>
          turn.role === 'user' ? (
            <div key={index} className="ml-auto max-w-[85%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">
              {turn.text}
            </div>
          ) : (
            <div key={index} className="mr-auto w-full max-w-[95%] rounded-lg bg-muted/40 px-3 py-2">
              {turn.activity.map((note, noteIndex) => (
                <p key={noteIndex} className="text-xs italic text-muted-foreground">
                  {note}
                </p>
              ))}
              {turn.status === 'running' && (
                <div className="mt-1 flex flex-col gap-1.5">
                  <Skeleton className="h-3 w-40" />
                  <Skeleton className="h-3 w-56" />
                </div>
              )}
              {turn.status === 'error' && (
                <p role="alert" className="text-sm text-destructive">
                  {turn.error}
                </p>
              )}
              {turn.answer && <AnswerView answer={turn.answer} />}
            </div>
          ),
        )}
      </div>

      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          void send(input)
        }}
      >
        <Input
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="Ask a follow-up about this member…"
          disabled={busy}
          aria-label="Copilot message"
        />
        <Button type="submit" disabled={busy || !input.trim()}>
          Send
        </Button>
      </form>
    </div>
  )
}

function BriefTab({ slice }: { slice: ContextSlice }) {
  const brief = slice.coach_brief
  const churn = slice.churn_risk
  if (!brief) return <p className="text-sm text-muted-foreground">No brief in the graph.</p>
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted-foreground">
        Morning brief for {brief.generated_for} (the dataset's "today")
      </p>
      <ul className="flex flex-col gap-2">
        {brief.tasks.map((task, index) => (
          <li key={index} className="flex items-start gap-2 rounded-md border px-3 py-2 text-sm">
            <span aria-hidden>{task.type === 'celebrate' ? '🎉' : '⚠️'}</span>
            <div>
              <p className="text-xs font-medium uppercase text-muted-foreground">
                {task.type.replace('_', ' ')}
              </p>
              <p>{task.text}</p>
            </div>
          </li>
        ))}
      </ul>
      {churn && (
        <div className={`flex flex-col gap-1 rounded-md border px-3 py-2 ${CHURN_PANEL[churn.level]}`}>
          <p className="text-sm font-medium">
            Churn risk: <span className="uppercase">{churn.level}</span>{' '}
            <span className="font-normal text-muted-foreground">
              ({churn.score} of {churn.max_score} points)
            </span>
          </p>
          {churn.reasons.length > 0 ? (
            <ul className="ml-4 list-disc text-xs text-muted-foreground">
              {churn.reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">No warning signs firing.</p>
          )}
          <p className="text-xs italic text-muted-foreground">
            Computed from adherence and workout history — not a coach's judgement.
          </p>
        </div>
      )}
    </div>
  )
}

function HistoryTab({ slice }: { slice: ContextSlice }) {
  const history = slice.chat_history ?? []
  if (history.length === 0)
    return <p className="text-sm text-muted-foreground">No chat history in the graph.</p>
  return (
    <ul className="flex max-h-96 flex-col gap-2 overflow-y-auto">
      {history.map((message, index) => {
        const fromMember = message.sender === 'member'
        const captions = message.attachment_captions ?? []
        return (
          <li
            key={index}
            className={cn(
              'max-w-[85%] rounded-lg px-3 py-2 text-sm',
              fromMember ? 'mr-auto bg-muted/50' : 'ml-auto bg-primary/10',
            )}
          >
            <p className="text-[10px] uppercase text-muted-foreground">
              {message.sender} · {message.ts.slice(0, 16).replace('T', ' ')}
            </p>
            <p>{message.text}</p>
            {message.has_attachments &&
              captions.map((caption, captionIndex) => (
                <div
                  key={captionIndex}
                  className="mt-2 flex items-center gap-2 rounded-md border bg-background px-2 py-1.5 text-xs"
                >
                  <span className="text-base" aria-hidden>
                    🖼️
                  </span>
                  <span>
                    <span className="font-medium">Image attachment</span> · {caption}
                  </span>
                </div>
              ))}
          </li>
        )
      })}
    </ul>
  )
}

/**
 * The AI Copilot panel: a chat over the AG-UI stream with quick prompts and
 * agent-emitted Recharts charts, plus the morning brief (tasks + churn
 * indicator) and the past coach↔member chat history from KG 2.
 */
export function Copilot({ memberId }: CopilotProps) {
  const [tab, setTab] = useState<Tab>('chat')
  const [slice, setSlice] = useState<ContextSlice | null>(null)
  const [sliceError, setSliceError] = useState<string | null>(null)

  useEffect(() => {
    fetchContextSlice(memberId, ['coach_brief', 'churn_risk', 'chat_history'])
      .then(setSlice)
      .catch((err: unknown) =>
        setSliceError(err instanceof Error ? err.message : 'Failed to load context'),
      )
  }, [memberId])

  const churn = slice?.churn_risk

  const tabs: { id: Tab; label: string }[] = [
    { id: 'chat', label: 'Chat' },
    { id: 'brief', label: 'Morning brief' },
    { id: 'history', label: 'History' },
  ]

  return (
    <Card className="min-h-64">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>AI Copilot</CardTitle>
          {churn && (
            <Badge variant={CHURN_BADGE[churn.level]}>
              churn risk: {churn.level} ({churn.score}/{churn.max_score})
            </Badge>
          )}
        </div>
        <CardDescription>
          Ask questions about this member's context, grounded in the knowledge graph.
        </CardDescription>
        <div role="tablist" className="flex gap-1 pt-1">
          {tabs.map((entry) => (
            <button
              key={entry.id}
              role="tab"
              aria-selected={tab === entry.id}
              onClick={() => setTab(entry.id)}
              className={cn(
                'rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
                tab === entry.id
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:bg-muted',
              )}
            >
              {entry.label}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col">
        {tab === 'chat' ? (
          <ChatTab memberId={memberId} />
        ) : sliceError ? (
          <p role="alert" className="text-sm text-destructive">
            {sliceError}
          </p>
        ) : !slice ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-48" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : tab === 'brief' ? (
          <BriefTab slice={slice} />
        ) : (
          <HistoryTab slice={slice} />
        )}
      </CardContent>
    </Card>
  )
}
