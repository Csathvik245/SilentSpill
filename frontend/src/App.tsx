import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'motion/react'
import { Activity, AlertCircle, ArrowUpRight, Check, ChevronDown, CircleHelp, Clock3, Database, Droplets, Film, Gauge, LoaderCircle, Map as MapIcon, MapPin, RefreshCw, Waves } from 'lucide-react'
import { toast } from 'sonner'
import { getHealth, getIncident, getReach, getSensors, runScenario, streamUrl } from './lib/api'
import type { AgentMode, CandidateFacility, IncidentResponse, ReachResponse, ScenarioRunResponse, SensorSample, SensorSeriesResponse, StreamEvent } from './lib/types'
const ReachMap = lazy(() => import('./components/ReachMap').then((module) => ({ default: module.ReachMap })))
const SensorChart = lazy(() => import('./components/SensorChart').then((module) => ({ default: module.SensorChart })))
import { Button } from './components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from './components/ui/card'
import { Badge } from './components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './components/ui/tabs'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from './components/ui/accordion'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from './components/ui/tooltip'
import { ScrollArea } from './components/ui/scroll-area'
import { Separator } from './components/ui/separator'
// Counts the backend confidence value when a report arrives.
import { NumberTicker } from './components/ui/number-ticker'
// Gives the API report a brief, low-motion entrance.
import { BlurFade } from './components/ui/blur-fade'
// Reveals ranked API candidates in sequence.
import { AnimatedList } from './components/ui/animated-list'
// Adds a static, low-contrast texture to the empty state.
import { DotPattern } from './components/ui/dot-pattern'
import { Skeleton } from './components/ui/skeleton'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from './components/ui/sheet'
import { Toaster } from './components/ui/sonner'
import './App.css'
import './charts.css'

const NO_CANDIDATES: CandidateFacility[] = []
const AGENTS = ['triage', 'trace', 'compliance', 'report'] as const
const LABELS: Record<(typeof AGENTS)[number], string> = { triage: 'Triage', trace: 'Trace', compliance: 'Compliance', report: 'Report' }
const EASE = [0.22, 1, 0.36, 1] as const
type RunState = 'idle' | 'loading' | 'ready' | 'error'
type HealthState = 'checking' | 'online' | 'degraded' | 'offline'
type AgentState = 'waiting' | 'active' | 'done' | 'skipped'
type AgentMap = Record<(typeof AGENTS)[number], AgentState>
const initialAgents: AgentMap = { triage: 'waiting', trace: 'waiting', compliance: 'waiting', report: 'waiting' }

function initialAgentState(modes: ScenarioRunResponse['agent_modes']): AgentMap {
  return Object.fromEntries(AGENTS.map((agent) => [agent, modes[agent] === 'skipped' ? 'skipped' : 'waiting'])) as AgentMap
}

function modeLabel(mode: AgentMode | undefined) {
  if (mode === 'llm') return 'Model-assisted'
  if (mode === 'fallback') return 'Template fallback'
  if (mode === 'skipped') return 'Not run for this case'
  return 'Awaiting'
}

function App() {
  const reduceMotion = useReducedMotion()
  const [reach, setReach] = useState<ReachResponse | null>(null)
  const [health, setHealth] = useState<HealthState>('checking')
  const [runState, setRunState] = useState<RunState>('idle')
  const [caseName, setCaseName] = useState<'spill' | 'storm'>('spill')
  const [incident, setIncident] = useState<IncidentResponse | null>(null)
  const [sensors, setSensors] = useState<SensorSeriesResponse | null>(null)
  const [sensorError, setSensorError] = useState('')
  const [run, setRun] = useState<ScenarioRunResponse | null>(null)
  const [agentState, setAgentState] = useState<AgentMap>(initialAgents)
  const [replayRows, setReplayRows] = useState<SensorSample[]>([])
  const [scenarioTime, setScenarioTime] = useState<string | null>(null)
  const [replayLive, setReplayLive] = useState(false)
  const [plumePulse, setPlumePulse] = useState(0)
  const [activeFacility, setActiveFacility] = useState<string | null>(null)
  const [queryError, setQueryError] = useState('')
  const eventSource = useRef<EventSource | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([getHealth(controller.signal), getReach(controller.signal)])
      .then(([healthResult, reachResult]) => { setHealth(healthResult.status === 'ok' ? 'online' : 'degraded'); setReach(reachResult) })
      .catch(() => { if (controller.signal.aborted) return; setHealth('offline'); setQueryError('The local backend is not running. Start it to load the river network and begin a review.') })
    return () => { controller.abort(); eventSource.current?.close() }
  }, [])

  const candidates = incident?.trace.candidates ?? NO_CANDIDATES
  const storm = incident?.report.classification === 'storm_runoff'
  const candidateCount = storm ? 0 : candidates.length
  const selectFacility = useCallback((id: string) => setActiveFacility(id), [])

  const receiveEvent = useCallback((event: MessageEvent<string>) => {
    let ev: StreamEvent
    try { ev = JSON.parse(event.data) as StreamEvent } catch { return }
    if (ev.scenario_time) setScenarioTime(ev.scenario_time)
    if (ev.kind === 'sensor_tick') {
      const payload = ev.payload as { values?: SensorSample['values']; simulated?: boolean }
      if (payload.values) setReplayRows((rows) => [...rows, { timestamp: ev.scenario_time ?? new Date().toISOString(), values: payload.values!, simulated: payload.simulated ?? false }])
    } else if (ev.kind === 'plume_detection') {
      setPlumePulse((value) => value + 1)
    } else if (ev.kind === 'agent_output') {
      const payload = ev.payload as { agent?: keyof AgentMap; mode?: AgentMode }
      if (!payload.agent || !AGENTS.includes(payload.agent)) return
      setAgentState((previous) => ({ ...previous, [payload.agent!]: 'active' }))
      window.setTimeout(() => setAgentState((previous) => ({ ...previous, [payload.agent!]: previous[payload.agent!] === 'skipped' ? 'skipped' : 'done' })), 220)
    } else if (ev.kind === 'complete') {
      setAgentState((previous) => Object.fromEntries(AGENTS.map((agent) => [agent, previous[agent] === 'skipped' ? 'skipped' : 'done'])) as AgentMap)
      setReplayLive(false)
      eventSource.current?.close()
    }
  }, [])

  async function startReview() {
    if (health === 'offline' || health === 'checking' || runState === 'loading') return
    eventSource.current?.close()
    setRunState('loading'); setQueryError(''); setSensorError(''); setIncident(null); setSensors(null); setRun(null)
    setReplayRows([]); setScenarioTime(null); setAgentState(initialAgents); setReplayLive(false); setActiveFacility(null)
    try {
      const runResult = await runScenario(caseName)
      setRun(runResult)
      setAgentState(initialAgentState(runResult.agent_modes))
      const [reportResult, sensorResult] = await Promise.allSettled([getIncident(runResult.incident_id), getSensors(runResult.incident_id)])
      if (reportResult.status === 'rejected') throw reportResult.reason
      const report = reportResult.value
      setIncident(report)
      if (sensorResult.status === 'fulfilled') setSensors(sensorResult.value)
      else setSensorError(sensorResult.reason instanceof Error ? sensorResult.reason.message : 'Sensor history could not be loaded.')
      setRunState('ready')
      if (runResult.case === 'spill') {
        setReplayLive(true)
        const source = new EventSource(streamUrl(runResult.incident_id))
        eventSource.current = source
        for (const name of ['sensor_tick', 'plume_detection', 'agent_output', 'complete']) source.addEventListener(name, receiveEvent as EventListener)
        source.onerror = () => { setReplayLive(false); source.close(); toast.error('Replay disconnected', { description: 'The API report is still available; the live sequence ended early.' }) }
      } else {
        setAgentState((previous) => Object.fromEntries(AGENTS.map((agent) => [agent, previous[agent] === 'skipped' ? 'skipped' : 'done'])) as AgentMap)
      }
      toast.success('Review loaded', { description: report.report.classification === 'storm_runoff' ? 'Storm context is ready for human review.' : 'Report and upstream facility records are ready for human review.' })
    } catch (error) {
      const message = error instanceof Error ? error.message : 'The review could not be loaded.'
      setQueryError(message); setRunState('error'); setReplayLive(false)
      toast.error('Review unavailable', { description: message })
    }
  }

  function replayAgain() {
    if (!run) return
    eventSource.current?.close(); setReplayRows([]); setScenarioTime(null); setAgentState(initialAgentState(run.agent_modes)); setReplayLive(true)
    const source = new EventSource(streamUrl(run.incident_id)); eventSource.current = source
    for (const name of ['sensor_tick', 'plume_detection', 'agent_output', 'complete']) source.addEventListener(name, receiveEvent as EventListener)
    source.onerror = () => { setReplayLive(false); source.close(); toast.error('Replay unavailable', { description: 'Check the local backend connection and retry the review.' }) }
  }

  const bannerText = incident?.report.banner ?? 'SIMULATED — requires human review'
  const healthText = health === 'online' ? 'Backend connected' : health === 'degraded' ? 'Backend degraded' : health === 'offline' ? 'Backend not running' : 'Connecting'
  const mapClass = incident?.report.classification === 'incident' ? 'incident' : storm ? 'storm' : incident ? 'review' : ''

  return <TooltipProvider>
    <main className="dashboard">
      <motion.div className="sim-banner" initial={reduceMotion ? false : { opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .32, ease: EASE }}><span className="banner-led" />{bannerText}<span className="banner-side">DEMONSTRATION DATA</span></motion.div>
      <header className="masthead"><motion.a href="#top" className="wordmark" initial={reduceMotion ? false : { opacity: 0, y: -5 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .28, ease: EASE }}><span className="wordmark-icon"><Waves size={20} /></span><span>SilentSpill<span className="wordmark-period">.</span></span></motion.a><motion.div className="head-context" initial={reduceMotion ? false : { opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: .28, delay: reduceMotion ? 0 : .08, ease: EASE }}><span className={`connection-state ${health}`}><i />{healthText}</span><span className="head-rule" /><span className="place-name">Virginia waterways</span><Sheet><SheetTrigger render={<Button variant="ghost" size="icon" aria-label="About data and review requirements"><CircleHelp size={17} /></Button>} /><SheetContent className="data-sheet"><SheetHeader><SheetTitle>Review guidance</SheetTitle><SheetDescription>SilentSpill provides an investigation lead for a person to check.</SheetDescription></SheetHeader><div className="sheet-note"><p>River network, gauge readings and permit records are returned by the local service. Simulated event signals are labelled throughout.</p><p>Facility coordinates represent reported locations and may not be surveyed discharge points. Review the API disclaimer and limitations before taking action.</p><Badge variant="outline">Human review required</Badge></div></SheetContent></Sheet></motion.div></header>

      <div className="workarea" id="top">
        <section className="map-column" aria-label="River network map">
          <div className="map-head"><motion.div initial={reduceMotion ? false : { opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .38, ease: EASE }}><motion.h1 initial={reduceMotion ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .4, delay: reduceMotion ? 0 : .05, ease: EASE }}>Follow the river<span>.</span></motion.h1><motion.p className="map-subtitle" initial={reduceMotion ? false : { opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .32, delay: reduceMotion ? 0 : .1, ease: EASE }}>Trace the upstream path from the monitoring gauge.</motion.p></motion.div><motion.div className="map-actions" initial={reduceMotion ? false : { opacity: 0, x: 7 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: .35, delay: reduceMotion ? 0 : .12, ease: EASE }}><Tooltip><TooltipTrigger render={<Button variant="outline" size="sm" className="source-button"><Database size={14} /> Live local data</Button>} /><TooltipContent>Reach geometry and facility records load from the API.</TooltipContent></Tooltip><span className="map-actions-separator" /><Button variant="outline" size="sm" onClick={() => { if (run?.case === 'spill') replayAgain(); else void startReview() }} disabled={health === 'offline' || health === 'checking' || runState === 'loading'}><RefreshCw size={14} className={replayLive ? 'rotate-icon' : ''} /> {run?.case === 'spill' ? 'Replay' : run ? 'Run again' : 'Start review'}</Button></motion.div></div>
          {health === 'offline' && <div className="backend-alert" role="status"><AlertCircle size={15} /><span><b>Backend not running</b> — start the local API to load the river reach and run scenarios.</span><code>localhost:8000</code></div>}
          <div className={`map-stage ${mapClass}`}>
            {reach ? <Suspense fallback={<div className="map-skeleton"><Skeleton className="skeleton-river" /><div className="skeleton-message">Preparing map renderer</div></div>}><ReachMap reach={reach} facilities={storm ? NO_CANDIDATES : candidates} activeFacility={activeFacility} pulse={plumePulse} onSelect={selectFacility} /></Suspense> : <div className="map-skeleton"><Skeleton className="skeleton-river" /><Skeleton className="skeleton-gauge" /><div className="skeleton-message"><LoaderCircle size={16} className="spin" />{health === 'offline' ? 'Map data unavailable' : 'Loading river network'}</div></div>}
            <div className="map-overlay-top"><Badge className={`classification-badge ${mapClass || 'neutral'}`}>{incident ? incident.report.classification.replaceAll('_', ' ') : 'Awaiting review'}</Badge>{incident && <span className="clock-chip"><Clock3 size={13} />{scenarioTime ? new Date(scenarioTime).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }) + ' UTC' : 'Report ready'}</span>}</div>
            <div className="map-legend"><span><i className="legend-line" /> Upstream river trace</span>{!storm && candidates.length > 0 && <span><i className="legend-facility" /> Facility for review</span>}<span><i className="legend-gauge" /> Monitoring gauge</span></div>
            <div className="map-scale"><MapIcon size={14} /><span>{reach?.max_km ?? '—'} km trace</span><span className="scale-line" /></div>
          </div>
          <div className="map-caption"><div><MapPin size={14} /><span><b>{reach?.gauge.name ?? 'Monitoring gauge'}</b><small>{reach ? `USGS ${reach.gauge.site_id} · linked to river network` : 'Connecting to local reach data'}</small></span></div><span className="map-caption-right"><span className="real-key" /> Real network and gauge <span className="caption-divider" /> <span className="sim-key" /> Simulated event evidence</span></div>
          <div className="map-utility-row"><div className="map-stat"><span className="stat-icon"><Waves size={15} /></span><span><small>NETWORK REACH</small><b>{reach ? `${reach.upstream_path.length} connected segments` : 'Loading from API'}</b></span></div><Separator orientation="vertical" className="utility-separator" /><div className="map-stat"><span className="stat-icon"><Gauge size={15} /></span><span><small>MONITORING SITE</small><b>{reach?.gauge.site_id ?? 'Pending connection'}</b></span></div><Separator orientation="vertical" className="utility-separator" /><div className="map-stat"><span className="stat-icon amber"><Activity size={15} /></span><span><small>UPSTREAM FACILITIES</small><b>{storm ? 'None flagged' : incident ? `${candidateCount} for review` : 'Run a scenario'}</b></span></div></div>
          {caseName === 'spill' && incident?.report.classification !== 'storm_runoff' && <section className="video-section" aria-label="Simulated aerial footage"><div className="video-head"><div><span className="video-index"><Film size={15} /></span><div><b>Simulated aerial footage</b><small>Supporting visual context · not a real spill recording</small></div></div><Badge variant="outline" className="video-tag">SIMULATED CLIP</Badge></div><video className="spill-video" autoPlay muted loop playsInline controls aria-label="Muted looping simulated aerial footage"><source src="/spill_20261009T2000Z_seed42.webm" type="video/webm" /><source src="/spill_20261009T2000Z_seed42.mp4" type="video/mp4" /></video></section>}
        </section>

        <motion.aside className="review-panel" aria-label="Investigation report panel" initial={reduceMotion ? false : { opacity: 0, x: 14 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: .42, delay: reduceMotion ? 0 : .08, ease: EASE }}>
          <div className="panel-topline"><div><span className="panel-kicker">FIELD REVIEW</span><h2>Investigation</h2></div><div className="panel-tools"><span className={`panel-health ${health}`}><i />{health === 'online' ? 'Connected' : health === 'degraded' ? 'Degraded' : health === 'offline' ? 'Offline' : 'Checking'}</span><Tooltip><TooltipTrigger render={<Button variant="ghost" size="icon" aria-label="Refresh backend status" onClick={() => { setHealth('checking'); getHealth().then((value) => setHealth(value.status === 'ok' ? 'online' : 'degraded')).catch(() => setHealth('offline')) }}><RefreshCw size={15} /></Button>} /><TooltipContent>Check local backend status</TooltipContent></Tooltip></div></div>
          <div className="scenario-controls"><div className="scenario-tabs" role="group" aria-label="Scenario type"><Button variant={caseName === 'spill' ? 'secondary' : 'ghost'} size="sm" onClick={() => setCaseName('spill')} disabled={runState === 'loading'}><Activity size={14} /> Signal review</Button><Button variant={caseName === 'storm' ? 'secondary' : 'ghost'} size="sm" onClick={() => setCaseName('storm')} disabled={runState === 'loading'}><Droplets size={14} /> Storm context</Button></div><Button onClick={startReview} disabled={health === 'offline' || health === 'checking' || runState === 'loading'} className="primary-action">{runState === 'loading' ? <><LoaderCircle size={15} className="spin" /> Preparing</> : run ? <>Run again <ArrowUpRight size={15} /></> : <>Run review <ArrowUpRight size={15} /> </>}</Button></div>
          <div className="panel-message-area" aria-live="polite">{queryError && <div className={`panel-error ${health === 'offline' ? 'offline' : ''}`}><AlertCircle size={15} /><span>{queryError}</span></div>}{runState === 'loading' && <LoadingCard />}</div>
          {incident && runState === 'ready' ? <Tabs defaultValue="findings" className="review-tabs">
            <TabsList className="review-tablist"><TabsTrigger value="findings">Findings</TabsTrigger><TabsTrigger value="evidence">Evidence & review</TabsTrigger></TabsList>
            <ScrollArea className="review-scroll"><TabsContent value="findings" className="tab-pane">
              <ClassificationCard incident={incident} storm={Boolean(storm)} reduced={Boolean(reduceMotion)} />
              <section className="confidence-row"><div><span className="confidence-label">REPORT CONFIDENCE</span><Tooltip><TooltipTrigger render={<button className="info-button" aria-label="About confidence"><CircleHelp size={13} /></button>} /><TooltipContent>Confidence is supplied by the backend report and is not a probability of facility involvement.</TooltipContent></Tooltip></div><ConfidenceValue value={incident.report.confidence} reduced={Boolean(reduceMotion)} /></section>
              {!storm && <section className="candidate-section"><div className="section-heading"><div><h3>Facilities with reported violations near this reach</h3><p>Prioritized for a person to check · details from local records</p></div><Badge variant="outline" className="count-badge">{candidates.length}</Badge></div>{candidates.length ? <Accordion className="candidate-list" multiple>{reduceMotion ? candidates.map((candidate, index) => <CandidateRow key={candidate.facility_id} candidate={candidate} index={index} selected={activeFacility === candidate.facility_id} onSelect={selectFacility} reduceMotion />) : <AnimatedList className="animated-candidate-list" delay={55}>{candidates.map((candidate, index) => <CandidateRow key={candidate.facility_id} candidate={candidate} index={index} selected={activeFacility === candidate.facility_id} onSelect={selectFacility} reduceMotion={false} />)}</AnimatedList>}</Accordion> : <EmptyCandidates />}</section>}
              {storm && <StormState />}
              <div className="timeline-section"><div className="section-heading"><div><h3>Event timeline</h3><p>Report events in the order provided by the API</p></div></div><div className="timeline-list">{incident.report.timeline.map((item, index) => <motion.div key={`${item.timestamp}-${index}`} className="timeline-row" initial={reduceMotion ? false : { opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: .28, delay: reduceMotion ? 0 : Math.min(index * .045, .27), ease: EASE }}><span className="timeline-dot" /><div><time>{item.timestamp ? new Date(item.timestamp).toLocaleString([], { timeZone: 'UTC', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' }) : 'Time not specified'}</time><p>{item.event}</p></div></motion.div>)}</div></div>
              <section className="agent-section"><div className="section-heading"><div><h3>Analysis chain</h3><p>Agent run status from this scenario</p></div>{replayLive && <span className="replay-status"><i /> Replaying</span>}</div><AgentChain run={run} state={agentState} reduced={Boolean(reduceMotion)} /></section>
            </TabsContent><TabsContent value="evidence" className="tab-pane evidence-pane">
              <section className="chart-section"><div className="section-heading"><div><h3>Sensor history</h3><p>Baseline and scenario measurements from the API</p></div><Badge variant="outline">{sensors?.site_id ?? 'No site'}</Badge></div>{sensorError ? <div className="panel-error"><AlertCircle size={14} /><span>{sensorError}</span></div> : <Suspense fallback={<Skeleton className="chart-fallback" />}><SensorChart data={sensors} replayRows={replayRows} scenarioTime={scenarioTime} /></Suspense>}</section>
              {incident.report.disclaimer && <section className="report-disclaimer"><span className="disclaimer-mark"><AlertCircle size={15} /></span><p>{incident.report.disclaimer}</p></section>}
              <Accordion className="report-sections" multiple><ApiTextSection value="assumptions" title="Assumptions" items={incident.report.assumptions} /><ApiTextSection value="limitations" title="Limitations" items={incident.report.limitations} /><AccordionItem value="related"><AccordionTrigger><span>Related past events</span><Badge variant="outline">{incident.report.related_past_events.length}</Badge></AccordionTrigger><AccordionContent><p className="api-copy">{incident.report.related_past_events_text}</p>{incident.report.related_past_events.map((past, index) => <p className="api-copy past-event" key={`${past.incident_id}-${index}`}>{past.text}</p>)}</AccordionContent></AccordionItem></Accordion>
              <div className="mode-grid"><ModeItem label="Report mode" mode={incident.report.report_mode} /><ModeItem label="Memory mode" mode={incident.memory_mode} /></div>
              <div className="origin-note"><Database size={14} /><span>{incident.report.data_origin}</span></div>
            </TabsContent></ScrollArea>
          </Tabs> : runState !== 'loading' && <EmptyReview health={health} />}
        </motion.aside>
      </div>
      <footer className="app-footer"><span><Waves size={14} /> SilentSpill <i /> Investigation support for Virginia waterways</span><span>Every finding requires human review.</span></footer>
      <Toaster position="top-right" closeButton />
    </main>
  </TooltipProvider>
}

function LoadingCard() {
  return <Card className="loading-card"><CardHeader><CardTitle><LoaderCircle size={15} className="spin" /> Preparing a review</CardTitle><CardDescription>Loading report, sensors, and upstream records from the backend.</CardDescription></CardHeader><CardContent className="loading-lines"><Skeleton /><Skeleton /><Skeleton /></CardContent></Card>
}

function EmptyReview({ health }: { health: HealthState }) {
  return <div className="empty-review"><DotPattern width={20} height={20} cr={.8} className="empty-pattern" /><span className="empty-icon">{health === 'offline' ? <AlertCircle size={19} /> : <Activity size={19} />}</span><h3>{health === 'offline' ? 'Backend not running' : 'Review a signal'}</h3><p>{health === 'offline' ? 'Start the local API to load the map, reports, sensor history, and facility records.' : 'Choose a scenario and run a review to bring the timeline, sensor evidence, and API report into this panel.'}</p>{health === 'offline' && <code>uvicorn · port 8000</code>}</div>
}

function ClassificationCard({ incident, storm, reduced }: { incident: IncidentResponse; storm: boolean; reduced: boolean }) {
  const classification = incident.report.classification
  return <BlurFade direction="up" blur="0px" duration={reduced ? 0 : .34} transition={{ duration: reduced ? 0 : .34, ease: EASE }}><Card className={`result-card ${storm ? 'storm-result' : classification === 'incident' ? 'incident-result' : 'review-result'}`}><CardHeader><div className="result-label-row"><span className="result-eyebrow">API REPORT · {incident.report.data_origin}</span><Badge className={`classification-badge ${storm ? 'storm' : classification === 'incident' ? 'incident' : 'review'}`}>{classification.replaceAll('_', ' ')}</Badge></div><CardTitle>{storm ? 'Storm runoff, no facilities flagged' : classification === 'incident' ? 'Signal review' : classification.replaceAll('_', ' ')}</CardTitle></CardHeader><CardContent><p className="report-summary">{incident.report.summary}</p></CardContent></Card></BlurFade>
}

function ConfidenceValue({ value, reduced }: { value: number; reduced: boolean }) {
  return <strong className="confidence-value">{reduced ? Math.round(value * 100) : <NumberTicker value={Math.round(value * 100)} className="confidence-ticker" />}<small>%</small></strong>
}

function CandidateRow({ candidate, index, selected, onSelect, reduceMotion }: { candidate: CandidateFacility; index: number; selected: boolean; onSelect: (id: string) => void; reduceMotion: boolean }) {
  return <motion.div className={`candidate-wrap ${selected ? 'selected' : ''}`} layout initial={reduceMotion ? false : { opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .32, delay: reduceMotion ? 0 : Math.min(index * .06, .24), ease: EASE }}><AccordionItem value={candidate.facility_id}><AccordionTrigger onClick={() => onSelect(candidate.facility_id)}><span className="candidate-trigger"><span className="candidate-rank">{String(index + 1).padStart(2, '0')}</span><span className="candidate-names"><b>{candidate.name}</b><small>{candidate.facility_id} <i /> {candidate.location_type}</small></span><span className="candidate-count">{candidate.numeric_violation_count}<small>numeric</small></span><ChevronDown className="candidate-chevron" size={14} /></span></AccordionTrigger><AccordionContent><div className="candidate-content"><div className="candidate-stat-grid"><div><small>UPSTREAM DISTANCE</small><b>{candidate.upstream_distance_km == null ? '—' : `${candidate.upstream_distance_km.toFixed(1)} km`}</b></div><div><small>TRAVEL TIME</small><b>{candidate.travel_time?.display ?? 'Unavailable'}</b></div><div><small>PERMIT TYPE</small><b>{candidate.permit_type_desc ?? 'Not reported'}</b></div><div><small>REPORTED WINDOW</small><b>{candidate.violation_window_label || 'Not specified'}</b></div></div><p className="location-note">{candidate.location_note}</p>{candidate.violations.length > 0 && <div className="violation-list"><b>Reported violations</b>{candidate.violations.map((violation, violationIndex) => <div className="violation-row" key={`${violation.parameter}-${violationIndex}`}><span>{violation.parameter}</span><span>{violation.violation_desc}</span><time>{violation.monitoring_period_end ?? 'Date not reported'}</time></div>)}</div>}<Button variant="outline" size="sm" className="locate-button" onClick={(event) => { event.stopPropagation(); onSelect(candidate.facility_id) }}><MapPin size={13} /> Show on map <ArrowUpRight size={13} /></Button></div></AccordionContent></AccordionItem></motion.div>
}

function EmptyCandidates() { return <div className="empty-candidates"><MapPin size={16} /><span>No facility candidates were returned by the API for this report.</span></div> }
function StormState() { return <motion.div className="storm-state" initial={{ opacity: 0, y: 7 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .35, ease: EASE }}><span className="storm-icon"><Droplets size={17} /></span><div><b>Storm runoff, no facilities flagged</b><p>No facility list is shown for this classification.</p></div></motion.div> }

function AgentChain({ run, state, reduced }: { run: ScenarioRunResponse | null; state: AgentMap; reduced: boolean }) {
  return <ol className="agent-chain">{AGENTS.map((agent, index) => { const status = state[agent]; const mode = run?.agent_modes[agent]; return <li className={`agent-step ${status}`} key={agent}><motion.span className="agent-marker" layout initial={false} animate={{ scale: status === 'active' ? 1.05 : 1 }} transition={{ type: 'spring', stiffness: 380, damping: 28 }}>{status === 'done' ? <Check size={13} /> : status === 'active' ? <LoaderCircle size={13} className={reduced ? '' : 'spin'} /> : <span>{index + 1}</span>}</motion.span>{index < AGENTS.length - 1 && <motion.span className="agent-connector" initial={false} animate={{ opacity: status === 'done' ? 1 : .45 }} transition={{ duration: .3, ease: EASE }} /> }<div className="agent-text"><b>{LABELS[agent]}</b><small>{status === 'skipped' ? modeLabel(mode) : status === 'done' ? modeLabel(mode) : status === 'active' ? 'In progress' : mode ? modeLabel(mode) : 'Awaiting'}</small></div></li> })}</ol>
}

function ApiTextSection({ value, title, items }: { value: string; title: string; items: string[] }) {
  return <AccordionItem value={value}><AccordionTrigger><span>{title}</span><Badge variant="outline">{items.length}</Badge></AccordionTrigger><AccordionContent>{items.length ? items.map((text, index) => <p className="api-copy" key={`${value}-${index}`}>{text}</p>) : <p className="api-copy">None listed in this report.</p>}</AccordionContent></AccordionItem>
}

function ModeItem({ label, mode }: { label: string; mode: string }) { return <div className="mode-item"><small>{label}</small><b>{mode.replaceAll('_', ' ')}</b></div> }

export default App
