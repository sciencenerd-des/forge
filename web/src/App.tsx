import { useCallback, useEffect, useState } from 'react'
import {
  Activity, Bot, Box, BrainCircuit, Clock3,
  Database, FileCode2, ListChecks, OctagonX,
  Radio, Search, Settings2, ShieldCheck, TerminalSquare, Server, Check, X, FolderGit2,
} from 'lucide-react'
import './App.css'
import {
  ControlPlaneClient,
  type Approval, type ProjectSummary, type RuntimeConfig, type RuntimeRun, type RuntimeSystem,
} from './lib/control-plane'

type RunStatus = 'running' | 'paused' | 'completed' | 'failed'
type NodeStatus = 'completed' | 'active' | 'waiting'
type View = 'Runs' | 'Projects' | 'Approvals' | 'Config'

interface RunEvent { id: number; time: string; source: string; kind: 'info' | 'success' | 'warning'; message: string }

const client = new ControlPlaneClient()

function App() {
  const [view, setView] = useState<View>('Runs')
  const [status, setStatus] = useState<RunStatus>('running')
  const [events, setEvents] = useState<RunEvent[]>([])
  const [liveRun, setLiveRun] = useState<RuntimeRun | null>(null)
  const [runtimeSystem, setRuntimeSystem] = useState<RuntimeSystem | null>(null)
  const [config, setConfig] = useState<RuntimeConfig | null>(null)
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [controlError, setControlError] = useState<string | null>(null)
  const [eventQuery, setEventQuery] = useState('')
  const [confirmStop, setConfirmStop] = useState(false)
  const [goalDraft, setGoalDraft] = useState('')
  const [starting, setStarting] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [runs, system] = await Promise.all([client.listRuntimeRuns(), client.getRuntimeSystem()])
      setRuntimeSystem(system)
      const run = runs[0] ?? null
      setLiveRun(run)
      if (!run) { setStatus('paused'); setEvents([]) }
      else {
        setStatus(run.status === 'completed' ? 'completed'
          : ['failed', 'blocked', 'stopped'].includes(run.status) ? 'failed'
          : run.status === 'paused' ? 'paused' : 'running')
        setEvents(run.log_tail.map((line, index) => ({
          id: index,
          time: run.updated_at ? new Date(run.updated_at).toLocaleTimeString([], { hour12: false }) : '--:--:--',
          source: run.current_node,
          kind: /failed|error|❌/i.test(line) ? 'warning' : /complete|passed|🏁|✅/i.test(line) ? 'success' : 'info',
          message: line.slice(0, 200),
        })))
      }
      setControlError(null)
    } catch (error) {
      setControlError(error instanceof Error ? error.message : 'Control plane unavailable')
    }
  }, [])

  // slow feeds (projects, approvals) + one-shot config
  const refreshSlow = useCallback(async () => {
    try {
      const [p, a] = await Promise.all([client.listProjects(), client.listApprovals('pending')])
      setProjects(p); setApprovals(a)
    } catch { /* surfaced via controlError on the fast loop */ }
  }, [])

  useEffect(() => {
    const initial = window.setTimeout(() => {
      void refresh(); void refreshSlow()
      client.getRuntimeConfig().then(setConfig).catch((error: unknown) => {
        setControlError(error instanceof Error ? error.message : 'Configuration unavailable')
      })
    }, 0)
    const fast = window.setInterval(() => void refresh(), 1500)
    const slow = window.setInterval(() => void refreshSlow(), 5000)
    return () => { window.clearTimeout(initial); window.clearInterval(fast); window.clearInterval(slow) }
  }, [refresh, refreshSlow])

  const progress = liveRun?.task_total
    ? Math.round((liveRun.task_completed / liveRun.task_total) * 100)
    : 0

  const workflow: Array<{ name: string; status: NodeStatus; note: string }> =
    ['planner', 'auditor', 'executor', 'evaluator'].map((node) => ({
      name: node[0].toUpperCase() + node.slice(1),
      status: liveRun?.current_node === node ? 'active' : 'waiting',
      note: liveRun?.current_node === node ? 'Running now' : 'Waiting',
    }))

  const lastEvaluatorLine = findLastEvaluatorLine(liveRun?.log_tail ?? [])
  const filteredEvents = eventQuery.trim()
    ? events.filter((event) => `${event.source} ${event.message}`.toLowerCase().includes(eventQuery.trim().toLowerCase()))
    : events

  const stopRun = async () => {
    if (!liveRun) return
    try { await client.stopRun(liveRun.project_id); setConfirmStop(false); void refresh() }
    catch (e) { setControlError(e instanceof Error ? e.message : 'stop failed') }
  }

  const decide = async (id: string, approved: boolean) => {
    const reason = window.prompt(`${approved ? 'Approve' : 'Deny'} reason (required):`)
    if (!reason?.trim()) return
    try { await client.decideApproval(id, approved, reason.trim()); void refreshSlow() }
    catch (e) { setControlError(e instanceof Error ? e.message : 'decision failed') }
  }

  const startRun = async () => {
    if (goalDraft.trim().length < 3) return
    setStarting(true)
    try {
      await client.startRun(goalDraft.trim())
      setGoalDraft('')
      await refresh()
    } catch (error) {
      setControlError(error instanceof Error ? error.message : 'run failed to start')
    } finally {
      setStarting(false)
    }
  }

  const nav: Array<{ label: View; icon: typeof Activity; badge?: number }> = [
    { label: 'Runs', icon: Activity },
    { label: 'Projects', icon: Box, badge: projects.length || undefined },
    { label: 'Approvals', icon: ShieldCheck, badge: approvals.length || undefined },
    { label: 'Config', icon: Settings2 },
  ]
  const runLive = runtimeSystem?.gateway.status === 'running' || (liveRun?.pid != null)

  return (
    <div className="forge-shell min-h-screen bg-[#090b0d] text-slate-200">
      <div className="geo-field" aria-hidden="true"><span /><span /><span /><span /></div>
      <div className="min-h-screen pt-24 md:pt-28">
        <aside className="dynamic-island" aria-label="Application navigation">
          <div className="island-brand">
            <div className="brand-mark grid size-8 place-items-center rounded-md bg-amber-300 text-black"><TerminalSquare size={18} strokeWidth={2.5} /></div>
            <div><div className="font-mono text-base font-bold tracking-[0.18em] text-white">FORGE</div><div className="hidden text-xs uppercase tracking-widest text-slate-500 xl:block">Agent harness</div></div>
          </div>
          <nav className="island-nav" aria-label="Primary">
            {nav.map(({ label, icon: Icon, badge }) => (
              <button key={label} onClick={() => setView(label)} aria-pressed={view === label} className={`nav-item ${view === label ? 'nav-active' : ''}`}>
                <Icon size={16} />{label}
                {badge ? <span className="ml-1 rounded bg-amber-300 px-1.5 text-[10px] font-bold text-black">{badge}</span> : null}
              </button>
            ))}
          </nav>
          <div className="island-meta">
            <div className="workspace-chip"><span className="workspace-shape" /><span className="hidden xl:inline">{config?.default_project && config.default_project !== 'auto (resolve-or-create)' ? config.default_project.slice(0, 12) : liveRun?.project_name ?? 'forge'}</span></div>
            <div className={`island-live ${runLive ? '' : 'offline'}`} role="status" aria-label={`Gateway ${runtimeSystem?.gateway.status ?? 'connecting'}`}><Radio size={13} /><span className="hidden 2xl:inline">{runLive ? 'Runtime live' : 'Runtime idle'}</span></div>
          </div>
        </aside>

        <main className="min-w-0 border-t-3 border-black">
          <header className="flex min-h-16 flex-wrap items-center justify-between gap-3 border-b border-white/8 bg-[#0d1013]/90 px-4 py-3 backdrop-blur md:px-6">
            <div className="min-w-0">
              <div className="flex items-center gap-2"><span className={`status-dot ${status}`} /><span className="font-mono text-sm uppercase tracking-widest text-slate-400">{view === 'Runs' ? (liveRun?.status ?? status) : view}</span><span className="text-slate-700">/</span><span className="font-mono text-sm text-slate-500">{view === 'Runs' ? (liveRun?.id?.slice(0, 12) ?? 'idle') : provLabel(config)}</span></div>
              <h1 className="run-title mt-1 truncate text-xl font-semibold text-white md:text-2xl">{headerTitle(view, liveRun)}</h1>
              {controlError && <div role="alert" className="mt-1 text-sm font-semibold text-red-700">{controlError}</div>}
            </div>
            {view === 'Runs' && (
              <div className="flex items-center gap-2">
                <button className="control-button danger" onClick={() => setConfirmStop(true)} disabled={!liveRun || liveRun.pid == null} title="Signal the run process and mark it stopped"><OctagonX size={14} />Stop</button>
              </div>
            )}
          </header>

          {view === 'Runs' && <RunsView liveRun={liveRun} runtimeSystem={runtimeSystem} progress={progress} workflow={workflow} events={filteredEvents} eventQuery={eventQuery} onEventQuery={setEventQuery} goalDraft={goalDraft} onGoalDraft={setGoalDraft} onStartRun={() => void startRun()} starting={starting} lastEvaluatorLine={lastEvaluatorLine} />}
          {view === 'Projects' && <ProjectsView projects={projects} />}
          {view === 'Approvals' && <ApprovalsView approvals={approvals} onDecide={decide} />}
          {view === 'Config' && <ConfigView config={config} />}
        </main>
        {confirmStop && liveRun ? (
          <div className="modal-backdrop" role="presentation" onMouseDown={() => setConfirmStop(false)}>
            <div className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="stop-title" onMouseDown={(event) => event.stopPropagation()}>
              <div className="section-label">Destructive control</div>
              <h2 id="stop-title" className="mt-2 text-2xl font-black uppercase">Stop this run?</h2>
              <p className="mt-3 text-sm">Forge will signal PID {liveRun.pid} and preserve durable state for inspection or resume.</p>
              <div className="mt-5 flex justify-end gap-3"><button className="control-button" onClick={() => setConfirmStop(false)}>Cancel</button><button className="control-button danger" onClick={() => void stopRun()}>Stop run</button></div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}

function findLastEvaluatorLine(lines: string[]): string | null {
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    if (/evaluator|verdict|VARIED|PASS|FAIL/i.test(lines[index])) return lines[index].slice(0, 220)
  }
  return null
}

function headerTitle(view: View, run: RuntimeRun | null): string {
  if (view === 'Runs') return run?.goal_title ?? 'No active PGE run'
  if (view === 'Projects') return 'Projects'
  if (view === 'Approvals') return 'Approvals'
  return 'Configuration'
}
function provLabel(c: RuntimeConfig | null): string {
  return c ? `${c.provider.kind} · ${c.provider.model}`.slice(0, 28) : 'connecting'
}

// ── Runs ─────────────────────────────────────────────────────────────────────
function RunsView({ liveRun, runtimeSystem, progress, workflow, events, eventQuery, onEventQuery, goalDraft, onGoalDraft, onStartRun, starting, lastEvaluatorLine }: {
  liveRun: RuntimeRun | null; runtimeSystem: RuntimeSystem | null; progress: number
  workflow: Array<{ name: string; status: NodeStatus; note: string }>; events: RunEvent[]
  eventQuery: string; onEventQuery: (value: string) => void
  goalDraft: string; onGoalDraft: (value: string) => void; onStartRun: () => void; starting: boolean
  lastEvaluatorLine: string | null
}) {
  return (
    <>
      {!liveRun ? <section className="goal-composer"><div><div className="section-label">Start a durable run</div><h2>Give Forge one concrete goal.</h2></div><label><span className="sr-only">Run goal</span><textarea value={goalDraft} onChange={(event) => onGoalDraft(event.target.value)} placeholder="Fix the parser bug and make every regression test pass" /></label><button className="button-primary" onClick={onStartRun} disabled={starting || goalDraft.trim().length < 3}>{starting ? 'Starting…' : 'Start run'}</button></section> : null}
      <section className="grid border-b border-white/8 bg-[#0b0e10] sm:grid-cols-2 xl:grid-cols-4">
        <Metric label="Batch" value={`${liveRun?.batch ?? 0}`} note={`pid ${liveRun?.pid ?? 'stopped'}`} icon={Clock3} />
        <Metric label="Task progress" value={`${liveRun?.task_completed ?? 0} / ${liveRun?.task_total ?? 0}`} note={`${progress}% complete`} icon={ListChecks} />
        <Metric label="Evidence" value={`${liveRun?.file_count ?? 0} files`} note={`${liveRun?.test_count ?? 0} tests`} icon={BrainCircuit} />
        <Metric label="Runtime" value={liveRun?.current_node ?? 'idle'} note={`${runtimeSystem?.active_pge_runs ?? 0} active · gateway ${runtimeSystem?.gateway.pid ?? 'off'}`} icon={Database} />
      </section>

      <div className="grid min-h-[calc(100vh-177px)] xl:grid-cols-[minmax(0,1fr)_350px]">
        <div className="min-w-0 border-white/8 xl:border-r">
          <section className="border-b border-white/8 p-4 md:p-6">
            <div className="mb-4 flex items-center justify-between"><div><p className="section-label">Workflow</p><h2 className="mt-1 text-lg font-semibold text-white">Deterministic node state</h2></div>{liveRun ? <span className="tag">attempt {liveRun.attempt_count}</span> : null}</div>
            <div className="grid gap-2 md:grid-cols-4">{workflow.map((node, index) => <WorkflowNode key={node.name} {...node} index={index + 1} />)}</div>
          </section>
          <section>
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/8 px-4 py-4 md:px-6"><div className="flex items-center gap-2"><Radio size={18} className="text-emerald-400" /><h2 className="text-lg font-semibold text-white">Live execution</h2></div><label className="event-search"><Search size={16} aria-hidden="true" /><span className="sr-only">Search events</span><input value={eventQuery} onChange={(event) => onEventQuery(event.target.value)} placeholder="Filter output" /></label></div>
            <div className="divide-y divide-white/5">
              {events.length ? events.map((event) => <EventRow key={event.id} event={event} />) : <Empty icon={Radio} text="No run output yet. Start one with `forge run --goal …`." />}
            </div>
          </section>
        </div>

        <aside className="bg-[#0b0e10] p-4 md:p-6">
          <p className="section-label">Current task</p>
          <h2 className="mt-2 text-xl font-bold leading-snug text-white">{liveRun?.active_task ?? 'No active task'}</h2>
          <p className="mt-3 text-sm leading-relaxed text-slate-500">Durable PGE state from PostgreSQL and the lifecycle manifest.</p>
          <div className="mt-5 space-y-2">
            <ContractRow icon={FileCode2} label="Project" value={liveRun?.project_name ?? 'unknown'} />
            <ContractRow icon={ShieldCheck} label="Attempts" value={`${liveRun?.attempt_count ?? 0} total · ${liveRun?.no_progress_count ?? 0} no-progress`} />
            <ContractRow icon={Bot} label="Executor model" value={liveRun?.model ?? 'local model'} />
          </div>
          {lastEvaluatorLine && (
            <div className="panel mt-5">
              <div className="section-label">Latest evaluator signal</div>
              <p className="mt-2 break-words font-mono text-sm leading-relaxed text-slate-400">{lastEvaluatorLine}</p>
            </div>
          )}
        </aside>
      </div>
    </>
  )
}

// ── Projects ─────────────────────────────────────────────────────────────────
function ProjectsView({ projects }: { projects: ProjectSummary[] }) {
  return (
    <section className="p-4 md:p-6">
      {projects.length === 0 ? <Empty icon={FolderGit2} text="No projects yet. One is created automatically on your first `forge run`." /> : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {projects.map((p) => {
            const pct = p.task_total ? Math.round((p.task_completed / p.task_total) * 100) : 0
            return (
              <div key={p.id} className="panel">
                <div className="flex items-center justify-between"><div className="flex items-center gap-2 text-base font-bold text-white"><Box size={16} className="text-amber-300" />{p.name}</div>{p.active_goal_status ? <span className={`tag ${p.active_goal_status === 'active' ? '' : ''}`}>{p.active_goal_status}</span> : null}</div>
                <p className="mt-2 truncate font-mono text-xs text-slate-600">{p.repo_path ?? '—'}</p>
                <p className="mt-3 text-sm text-slate-400">{p.active_goal ?? 'No active goal'}</p>
                <div className="mt-4"><div className="mb-1 flex justify-between text-xs"><span className="text-slate-500">{p.goal_count} goal{p.goal_count === 1 ? '' : 's'} · {p.task_completed}/{p.task_total} tasks</span><span className="font-mono text-slate-500">{pct}%</span></div><div className="h-2 overflow-hidden rounded-full bg-white/5"><div className="h-full bg-amber-300/70" style={{ width: `${pct}%` }} /></div></div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// ── Approvals ────────────────────────────────────────────────────────────────
function ApprovalsView({ approvals, onDecide }: { approvals: Approval[]; onDecide: (id: string, ok: boolean) => void }) {
  return (
    <section className="p-4 md:p-6">
      {approvals.length === 0 ? <Empty icon={ShieldCheck} text="No pending approvals. High-risk actions the agent requests will appear here for a human decision." /> : (
        <div className="space-y-3">
          {approvals.map((a) => (
            <div key={a.id} className="panel">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2"><span className={`risk-pill risk-${a.risk}`}>{a.risk}</span><span className="font-mono text-sm font-semibold text-white">{a.action_type}</span></div>
                  <p className="mt-1 truncate text-xs text-slate-500">run {a.run_id.slice(0, 12)} · requested by {a.requested_by}</p>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => onDecide(a.id, true)} className="flex items-center gap-1 rounded-md bg-emerald-400 px-3 py-2 text-sm font-semibold text-black hover:bg-emerald-300"><Check size={14} />Approve</button>
                  <button onClick={() => onDecide(a.id, false)} className="control-button danger"><X size={14} />Deny</button>
                </div>
              </div>
              <pre className="mt-3 overflow-x-auto rounded-md border border-white/6 bg-black/30 p-3 font-mono text-xs text-slate-400">{JSON.stringify(a.action_preview, null, 2)}</pre>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ── Config (plug-and-play) ───────────────────────────────────────────────────
function ConfigView({ config }: { config: RuntimeConfig | null }) {
  if (!config) return <section className="p-4 md:p-6"><Empty icon={Settings2} text="Loading resolved configuration…" /></section>
  return (
    <section className="grid gap-4 p-4 md:grid-cols-2 md:p-6">
      <div className="panel">
        <div className="flex items-center gap-2 text-base font-bold text-white"><Server size={16} className="text-amber-300" />Model provider</div>
        <p className="mt-1 text-xs text-slate-500">Any OpenAI-compatible backend. Change via <code className="font-mono text-amber-200/80">.env</code> / environment.</p>
        <div className="mt-4 space-y-2">
          <ConfigRow label="Backend" value={config.provider.kind} />
          <ConfigRow label="Base URL" value={config.provider.base_url} env="LLM_BASE_URL" />
          <ConfigRow label="Model" value={config.provider.model} env="LLM_MODEL" />
        </div>
        <div className="mt-4 border-t border-white/6 pt-3">
          <div className="section-label">Per-role model routing</div>
          <div className="mt-2 grid grid-cols-2 gap-1">
            {Object.entries(config.roles).map(([role, model]) => (
              <div key={role} className="flex items-center justify-between gap-2 rounded border border-white/6 bg-white/[0.02] px-2 py-1.5"><span className="font-mono text-[11px] uppercase tracking-wider text-slate-500">{role}</span><span className="truncate font-mono text-[11px] text-slate-300">{model}</span></div>
            ))}
          </div>
        </div>
      </div>
      <div className="panel">
        <div className="flex items-center gap-2 text-base font-bold text-white"><Database size={16} className="text-amber-300" />Runtime &amp; storage</div>
        <p className="mt-1 text-xs text-slate-500">Resolved from <code className="font-mono text-amber-200/80">forge_config</code> — no secrets shown.</p>
        <div className="mt-4 space-y-2">
          <ConfigRow label="Database" value={config.database} env="DATABASE_URL" />
          <ConfigRow label="Default project" value={config.default_project} env="FORGE_DEFAULT_PROJECT" />
          <ConfigRow label="Home" value={config.home} env="FORGE_HOME" />
          <ConfigRow label="Workspaces" value={config.workspaces} env="FORGE_WORKSPACES" />
        </div>
        <div className="mt-4 rounded-lg border border-white/7 bg-black/20 p-3">
          <div className="section-label">Point at another backend</div>
          <code className="mt-2 block overflow-x-auto whitespace-pre font-mono text-[10px] leading-relaxed text-slate-400">{`# .env\nLLM_BASE_URL=http://localhost:11434/v1   # Ollama\nLLM_MODEL=qwen2.5-coder\nFORGE_LLM_API_KEY=not-needed`}</code>
        </div>
      </div>
    </section>
  )
}

// ── shared bits (colourblock) ────────────────────────────────────────────────
function Empty({ icon: Icon, text }: { icon: typeof Radio; text: string }) {
  return <div className="flex flex-col items-center justify-center gap-3 px-6 py-16 text-center"><Icon size={28} className="text-slate-600" /><p className="max-w-md text-sm text-slate-500">{text}</p></div>
}
function ConfigRow({ label, value, env }: { label: string; value: string; env?: string }) {
  return <div className="flex items-center justify-between gap-3 rounded-md border border-white/6 bg-white/[0.02] px-3 py-2.5"><div className="text-xs font-bold uppercase tracking-wider text-slate-600">{label}{env ? <div className="mt-0.5 font-mono text-[10px] font-normal normal-case tracking-normal text-slate-700">{env}</div> : null}</div><div className="min-w-0 break-words text-right font-mono text-sm text-slate-300">{value}</div></div>
}
function Metric({ label, value, note, icon: Icon }: { label: string; value: string; note: string; icon: typeof Clock3 }) {
  return <div className="flex items-center gap-4 border-b border-r border-white/8 px-4 py-5 sm:px-6"><Icon size={22} className="text-slate-500" /><div><div className="section-label">{label}</div><div className="mt-1 font-mono text-lg font-semibold text-white">{value} <span className="font-sans text-sm font-normal text-slate-600">{note}</span></div></div></div>
}
function WorkflowNode({ name, status, note, index }: { name: string; status: NodeStatus; note: string; index: number }) {
  return <div className={`workflow-node ${status}`}><div className="flex items-center justify-between"><span className="font-mono text-sm text-slate-600">0{index}</span><span className={`node-indicator ${status}`} /></div><div className="mt-5 text-lg font-bold text-white">{name}</div><div className="mt-2 text-sm font-medium text-slate-500">{note}</div></div>
}
function EventRow({ event }: { event: RunEvent }) {
  return <div className="event-row grid grid-cols-[82px_94px_minmax(0,1fr)] gap-3 px-4 py-4 text-sm hover:bg-white/[0.02] md:grid-cols-[94px_110px_minmax(0,1fr)] md:px-6"><span className="font-mono text-slate-600">{event.time}</span><span className="font-mono text-sm font-semibold uppercase tracking-wider text-slate-500">{event.source}</span><div className="min-w-0"><div className="flex items-start gap-3 break-words leading-relaxed text-slate-300"><span className={`event-mark ${event.kind}`} />{event.message}</div></div></div>
}
function ContractRow({ icon: Icon, label, value }: { icon: typeof FileCode2; label: string; value: string }) {
  return <div className="flex items-center gap-3 rounded-md border border-white/6 bg-white/[0.02] px-3 py-3"><Icon size={18} className="text-slate-500" /><div className="min-w-0"><div className="text-xs font-bold uppercase tracking-wider text-slate-600">{label}</div><div className="mt-1 break-words font-mono text-sm leading-relaxed text-slate-300">{value}</div></div></div>
}

export default App
