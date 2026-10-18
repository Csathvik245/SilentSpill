import { Suspense, lazy, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { Activity, AlertCircle, ArrowRight, Check, ChevronDown, CloudRain, Database, Eye, GitBranch, Layers, MapPin, RefreshCw, ShieldCheck, Waves } from 'lucide-react'
import { toast } from 'sonner'
import { API_BASE, getHealth, getIncident, getReach, getSensors, runScenario, streamUrl } from './lib/api'
import type { AgentMode, CandidateFacility, IncidentResponse, ReachResponse, ScenarioRunResponse, SensorSample, SensorSeriesResponse, StreamEvent } from './lib/types'
import { Button } from './components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './components/ui/tabs'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from './components/ui/accordion'
import { NumberTicker } from './components/ui/number-ticker'
import { Skeleton } from './components/ui/skeleton'
import { Toaster } from './components/ui/sonner'
import './App.css'
import './charts.css'
const ReachMap = lazy(() => import('./components/ReachMap').then(module => ({ default: module.ReachMap })))
const SensorChart = lazy(() => import('./components/SensorChart').then(module => ({ default: module.SensorChart })))
const NO_CANDIDATES: CandidateFacility[] = []
const AGENTS = ['triage', 'trace', 'compliance', 'report'] as const
const LABELS = { triage: 'Triage', trace: 'Trace', compliance: 'Compliance', report: 'Report' }
const EASE = [0.22, 1, 0.36, 1] as const
const ROUTES = [['/', 'Overview'], ['/how', 'How it works'], ['/investigate', 'Investigate'], ['/data', 'Data & honesty'], ['/about', 'About']] as const
const BACKEND_COMMAND = '.\\.venv\\Scripts\\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000'
type RunState = 'idle' | 'loading' | 'ready' | 'error'
type HealthState = 'checking' | 'online' | 'degraded' | 'offline'
type AgentState = 'waiting' | 'active' | 'done' | 'skipped'
type AgentMap = Record<(typeof AGENTS)[number], AgentState>
const initialAgents: AgentMap = { triage: 'waiting', trace: 'waiting', compliance: 'waiting', report: 'waiting' }
function initialAgentState(modes: ScenarioRunResponse['agent_modes']): AgentMap {
  return Object.fromEntries(AGENTS.map(agent => [agent, modes[agent] === 'skipped' ? 'skipped' : 'waiting'])) as AgentMap
}
function readPage() { const path = window.location.hash.slice(1) || '/'; return ROUTES.some(([route]) => route === path) ? path : '/' }

function App() {
  const reduceMotion = useReducedMotion()
  const [page, setPage] = useState(readPage)
  const [dataReady, setDataReady] = useState<Record<string, boolean>>({})
  const [hoveredFacility, setHoveredFacility] = useState<string | null>(null)
  const [reviewState, setReviewState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [reviewDecision, setReviewDecision] = useState<'' | 'confirmed' | 'storm_runoff' | 'false_alarm'>('')
  const [reviewMessage, setReviewMessage] = useState('')
  useEffect(() => {
    const change = () => { setPage(readPage()); window.scrollTo({ top: 0, behavior: 'instant' }) }
    window.addEventListener('hashchange', change)
    return () => window.removeEventListener('hashchange', change)
  }, [])
  const [reach, setReach] = useState<ReachResponse | null>(null)
  const [health, setHealth] = useState<HealthState>('checking')
  const [runState, setRunState] = useState<RunState>('idle')
  const [caseName, setCaseName] = useState<'spill' | 'storm'>('spill')
  const [incident, setIncident] = useState<IncidentResponse | null>(null)
  const [latestIncident, setLatestIncident] = useState<IncidentResponse | null>(null)
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
      .then(([healthResult, reachResult]) => { setDataReady(healthResult.data_ready); setHealth(healthResult.status === 'ok' ? 'online' : 'degraded'); setReach(reachResult) })
      .catch(() => { if (controller.signal.aborted) return; setHealth('offline'); setQueryError('The local backend is not running. Start it to load the river network and begin a review.') })
    return () => { controller.abort(); eventSource.current?.close() }
  }, [])

  const candidates = incident?.trace.candidates ?? NO_CANDIDATES
  const storm = incident?.report.classification === 'storm_runoff'
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

  async function startReview(selectedCase: 'spill' | 'storm' = caseName) {
    if (health === 'offline' || health === 'checking' || runState === 'loading') return
    setCaseName(selectedCase); setReviewState('idle'); setReviewMessage(''); setHoveredFacility(null); setPlumePulse(0)
    eventSource.current?.close()
    setRunState('loading'); setQueryError(''); setSensorError(''); setIncident(null); setSensors(null); setRun(null)
    setReplayRows([]); setScenarioTime(null); setAgentState(initialAgents); setReplayLive(false); setActiveFacility(null)
    try {
      const runResult = await runScenario(selectedCase)
      setRun(runResult)
      setAgentState(initialAgentState(runResult.agent_modes))
      const [reportResult, sensorResult] = await Promise.allSettled([getIncident(runResult.incident_id), getSensors(runResult.incident_id)])
      if (reportResult.status === 'rejected') throw reportResult.reason
      const report = reportResult.value
      setIncident(report); setLatestIncident(report)
      setReviewDecision('')
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

  async function markReviewed() {
    if (!incident || !reviewDecision || reviewState === 'saving') return
    setReviewState('saving'); setReviewMessage('')
    try {
      const response = await fetch(`${API_BASE}/incidents/${encodeURIComponent(incident.incident_id)}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision: reviewDecision }) })
      const result = await response.json() as { detail?: string; memory_mode?: string; memory_reason?: string | null }
      if (!response.ok) throw new Error(result.detail ?? 'The review could not be saved. Please retry.')
      setReviewState('saved')
      const message = result.memory_mode === 'backboard' ? 'Review saved to incident memory.' : `Review received. Incident memory: ${result.memory_mode ?? 'unavailable'}.${result.memory_reason ? ` ${result.memory_reason}` : ''}`
      setReviewMessage(message); toast.success('Review received', { description: message })
    } catch (error) { setReviewState('error'); setReviewMessage(error instanceof Error ? error.message : 'The review could not be saved. Please retry.') }
  }

  const bannerText = incident?.report.banner ?? 'SIMULATED — requires human review'
  const disabled = health === 'offline' || health === 'checking' || runState === 'loading'
  const progress = runState === 'loading' ? 0 : !incident ? -1 : !replayLive ? 4 : agentState.report === 'done' ? 4 : agentState.compliance === 'done' ? 3 : agentState.trace === 'done' ? 2 : replayRows.length ? 1 : 0
  const mapView = (overview = false) => <div className={`map-stage ${overview ? 'overview-map' : ''}`}>
    {reach ? <Suspense fallback={<MapLoading />}><ReachMap reach={reach} facilities={overview || storm ? NO_CANDIDATES : candidates} activeFacility={overview ? null : activeFacility} hoveredFacility={overview ? null : hoveredFacility} pulse={overview || storm ? 0 : plumePulse} onSelect={selectFacility} /></Suspense> : <MapLoading offline={health === 'offline'} />}
    <div className="map-location"><Waves size={18} /><span>{reach?.gauge.name ?? 'Connecting to the river network'}<small>{reach ? `USGS ${reach.gauge.site_id}` : 'Real river geometry'}</small></span></div>
    <div className="map-legend"><span><i className="water-key" /> River network</span><span><i className="gauge-key" /> Gauge</span>{!overview && !storm && candidates.length > 0 && <span><i className="pin-key" /> Facilities for review</span>}</div>
    {!overview && scenarioTime && <time className="scenario-clock">{replayLive ? 'Replay · ' : ''}{new Date(scenarioTime).toLocaleTimeString([], { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' })} UTC</time>}
  </div>

  return <>
    <header className="site-header"><a href="#/" className="wordmark" aria-label="SilentSpill overview"><Waves size={23} />SilentSpill</a><nav aria-label="Main navigation">{ROUTES.map(([route, label]) => <a key={route} href={`#${route}`} aria-current={page === route ? 'page' : undefined}>{label}{page === route && <motion.span className="nav-indicator" layoutId="nav-indicator" transition={{ duration: reduceMotion ? 0 : .3, ease: EASE }} />}</a>)}</nav><span className="demo-pill"><span />Simulated demo</span></header>
    <div className="simulation-banner" role="note">{bannerText}</div>
    {health === 'offline' && <div className="offline-notice" role="alert"><AlertCircle size={22} /><div><strong>Backend not running</strong><p>Start the backend from the project folder, then reload this page.</p><code>{BACKEND_COMMAND}</code><Button variant="outline" onClick={() => window.location.reload()}>Retry connection</Button></div></div>}
    <AnimatePresence mode="wait" initial={false}><motion.main key={page} className={page === '/investigate' ? 'investigation-page' : 'content-page'} initial={{ opacity: reduceMotion ? 1 : 0 }} animate={{ opacity: 1 }} exit={{ opacity: reduceMotion ? 1 : 0 }} transition={{ duration: reduceMotion ? 0 : .3, ease: EASE }}>
      {page === '/' && <>
        <Reveal className="overview-hero"><h1>Know where to look when a river goes wrong.</h1><p className="lede">SilentSpill combines a river camera and water sensors, rules out storms, traces the water upstream, and lists permitted facilities with reported violations — for a human to review.</p><div className="hero-actions"><a className="button-link primary" href="#/investigate">Run the investigation <ArrowRight size={18} /></a><a className="button-link secondary" href="#/how">How it works <ArrowRight size={18} /></a></div></Reveal>
        <Reveal className="hero-map-wrap">{mapView(true)}<p className="map-caption">Explore the real river network, upstream from the monitoring gauge.</p></Reveal>
        <Reveal className="value-points"><div><Activity /><h3>Two signals must agree</h3><p>Camera observations are checked against changes in the water.</p></div><div><CloudRain /><h3>Storms are ruled out</h3><p>Rainfall provides context before facilities are considered.</p></div><div><ShieldCheck /><h3>Never accuses — always human review</h3><p>A lead to investigate, with the evidence and limits in view.</p></div></Reveal>
        <Reveal className="real-data-strip"><Database size={20} /><p>{reach && dataReady.baseline_snapshot ? <>Real USGS gauge <span className="number">{reach.gauge.site_id}</span> · Real NOAA rainfall</> : 'Gauge and rainfall data pending'}{dataReady.echo_data && ' · Real EPA permit records'}{reach && <> · <span className="number">{reach.upstream_path.length}</span> upstream river segments</>}</p><a href="#/data">What’s real <ArrowRight size={16} /></a></Reveal>
      </>}
      {page === '/how' && <HowPage />}
      {page === '/investigate' && <div className="investigate-split"><section className="investigate-map" aria-label="River investigation map">{mapView()}</section><aside className="investigate-panel" aria-label="Investigation results">
        <div className="panel-controls"><div className="panel-heading"><h1>Investigate</h1><span className="connection-status">{health === 'online' ? 'Connected' : health === 'degraded' ? 'Limited data' : health === 'offline' ? 'Offline' : 'Connecting…'}</span></div><div className="scenario-buttons"><Button className={`scenario-button ${caseName === 'spill' && run ? 'chosen' : ''}`} variant="outline" disabled={disabled} onClick={() => void startReview('spill')}><Activity size={18} />Spill scenario</Button><Button className={`scenario-button ${caseName === 'storm' && run ? 'chosen storm-choice' : ''}`} variant="outline" disabled={disabled} onClick={() => void startReview('storm')}><CloudRain size={18} />Storm scenario</Button><Button variant="ghost" className="replay-button" disabled={!run || disabled} onClick={replayAgain}><RefreshCw size={17} />Replay</Button></div>
          <ol className="progress-stepper" aria-label="Investigation progress">{['Detect', 'Rain check', 'Trace', 'Facilities', 'Report'].map((label, index) => <li key={label} className={index <= progress ? 'complete' : ''} aria-current={index === progress ? 'step' : undefined}><span>{index < progress || (index === progress && !replayLive && incident) ? <Check size={12} /> : index + 1}</span><small>{label}</small></li>)}</ol>
        </div>
        <div className="panel-body" aria-live="polite">
          {queryError && health !== 'offline' && <p className="error-message" role="alert">{queryError}</p>}
          {runState === 'loading' && <div className="loading-report"><p>Checking the signals and upstream records…</p><Skeleton className="loading-title" /><Skeleton /><Skeleton /><Skeleton /></div>}
          {!incident && runState !== 'loading' && <div className="investigate-empty"><Waves size={36} /><h2>Follow a signal.</h2><p>Choose a scenario to see the evidence, trace the river, and review what the system finds.</p><p className="muted">The spill tests an unusual signal. The storm shows when no facilities should be flagged.</p></div>}
          {incident && runState === 'ready' && <>
            <section className={`answer-card ${storm ? 'storm-answer' : incident.report.classification === 'incident' ? 'incident-answer' : 'review-answer'}`}><div><span className="answer-label">Investigation result</span><h2>{storm ? 'Storm runoff — no facilities flagged' : incident.report.classification === 'incident' ? 'Likely incident — needs review' : incident.report.classification === 'no_incident' ? 'No incident indicated' : 'Further review needed'}</h2></div><div className="confidence"><strong>{reduceMotion ? Math.round(incident.report.confidence * 100) : <NumberTicker key={incident.incident_id} value={Math.round(incident.report.confidence * 100)} className="confidence-number" />}<span>%</span></strong><span>Report confidence</span></div></section>
            <Tabs defaultValue="facilities" className="investigate-tabs"><TabsList className="investigate-tablist">{['Facilities', 'Sensors', 'Report', 'Footage'].map(tab => <TabsTrigger key={tab} value={tab.toLowerCase()}>{tab}</TabsTrigger>)}</TabsList>
              <TabsContent value="facilities" className="tab-content"><p className="api-disclaimer">{incident.report.disclaimer}</p>{storm ? <div className="storm-empty"><CloudRain size={30} /><h3>Storm runoff — no facilities flagged</h3><p>The report remains available in the Report tab.</p></div> : <><h3 className="facility-heading">Facilities with reported violations near this reach</h3>{candidates.length ? <Accordion multiple className="facility-list">{candidates.map((candidate, index) => <FacilityRow key={candidate.facility_id} candidate={candidate} index={index} selected={activeFacility === candidate.facility_id} onSelect={selectFacility} onHover={setHoveredFacility} />)}</Accordion> : <p>No facilities were returned for this report.</p>}</>}</TabsContent>
              <TabsContent value="sensors" className="tab-content"><h3>Water sensor history</h3><p className="muted">Real observations and simulated changes, shown separately.</p>{sensorError ? <p className="error-message">{sensorError}</p> : <Suspense fallback={<Skeleton className="chart-fallback" />}><SensorChart data={sensors} replayRows={replayRows} scenarioTime={scenarioTime} /></Suspense>}</TabsContent>
              <TabsContent value="report" className="tab-content"><h3>The report</h3><p className="api-copy">{incident.report.summary}</p><h3 className="section-spacer">Timeline</h3><ol className="report-timeline">{incident.report.timeline.map((item, index) => <li key={index}><time>{item.timestamp ? new Date(item.timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' }) : 'Time not specified'}{item.timestamp && ' UTC'}</time><p>{item.event}</p></li>)}</ol><Accordion multiple className="text-accordion"><ApiTextSection value="assumptions" title="Assumptions" items={incident.report.assumptions} /><ApiTextSection value="limitations" title="Limitations" items={incident.report.limitations} /></Accordion><p className="report-mode">Report: {incident.report.report_mode} · Memory: {incident.memory_mode}</p></TabsContent>
              <TabsContent value="footage" className="tab-content">{run?.case === 'spill' && !storm ? <><span className="footage-label">SIMULATED FOOTAGE</span><h3>Simulated aerial footage</h3><p className="muted">Synthetic visual evidence for this demonstration.</p><video className="spill-video" autoPlay={!reduceMotion} muted loop playsInline controls aria-label="Simulated aerial footage"><source src="/spill_20261009T2000Z_seed42.webm" type="video/webm" /><source src="/spill_20261009T2000Z_seed42.mp4" type="video/mp4" /></video></> : <div className="storm-empty"><Eye size={28} /><h3>No footage for the storm scenario</h3><p>The simulated aerial clip belongs to the spill scenario.</p></div>}</TabsContent>
            </Tabs>
            <section className="review-footer"><div className="agent-pills" aria-label="Agent status">{AGENTS.map(agent => <span key={agent}>{LABELS[agent]} <b>{run?.agent_modes[agent] ?? incident.agent_modes[agent]}</b></span>)}</div><details className="related-events"><summary>Related past events <ChevronDown size={16} /></summary><p>{incident.report.related_past_events_text}</p>{incident.report.related_past_events.map((past, i) => <p key={i}>{past.text}</p>)}</details><div className="human-review"><label htmlFor="review-decision">Your review decision</label><select id="review-decision" value={reviewDecision} disabled={reviewState === 'saving'} onChange={e => { setReviewDecision(e.target.value as typeof reviewDecision); setReviewState('idle'); setReviewMessage('') }}><option value="" disabled>Choose your decision</option><option value="confirmed">Confirm incident for follow-up</option><option value="storm_runoff">Storm runoff</option><option value="false_alarm">False alarm</option></select><Button className="review-submit" onClick={() => void markReviewed()} disabled={!reviewDecision || reviewState === 'saving' || reviewState === 'saved'}>{reviewState === 'saving' ? 'Saving review…' : reviewState === 'saved' ? <><Check size={17} />Reviewed</> : 'Mark as reviewed'}</Button>{reviewMessage && <p role="status" className={reviewState === 'error' ? 'error-message' : 'muted'}>{reviewMessage}</p>}</div></section>
          </>}
        </div>
      </aside></div>}
      {page === '/data' && <DataPage incident={incident ?? latestIncident} dataReady={dataReady} />}
      {page === '/about' && <AboutPage />}
    </motion.main></AnimatePresence>
    {page !== '/investigate' && <footer className="site-footer"><span>SilentSpill</span><p>Every finding is a starting point for human review.</p><a href="#/investigate">Open the investigation <ArrowRight size={16} /></a></footer>}
    <Toaster position="bottom-right" closeButton />
  </>
}

function Reveal({ children, className = '' }: { children: ReactNode; className?: string }) {
  const reduced = useReducedMotion()
  return <motion.section className={className} initial={reduced ? false : { opacity: 0, y: 12 }} whileInView={{ opacity: 1, y: 0 }} viewport={{ once: true, amount: .1 }} transition={{ duration: reduced ? 0 : .4, ease: EASE }}>{children}</motion.section>
}
function MapLoading({ offline = false }: { offline?: boolean }) { return <div className="map-loading">{!offline && <Waves size={30} />}<p>{offline ? 'Map unavailable until the backend is running.' : 'Loading the river network…'}</p></div> }
function FacilityRow({ candidate: c, index, selected, onSelect, onHover }: { candidate: CandidateFacility; index: number; selected: boolean; onSelect: (id: string) => void; onHover: (id: string | null) => void }) {
  const reduced = useReducedMotion()
  return <motion.div className={`facility-row ${selected ? 'selected' : ''}`} initial={reduced ? false : { opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .3, delay: reduced ? 0 : index * .05, ease: EASE }} onMouseEnter={() => onHover(c.facility_id)} onMouseLeave={() => onHover(null)} onFocus={() => onHover(c.facility_id)} onBlur={() => onHover(null)}><AccordionItem value={c.facility_id}><AccordionTrigger onClick={() => onSelect(c.facility_id)}><span className="facility-rank number">{index + 1}</span><span className="facility-info"><strong>{c.name}</strong><span>{c.permit_type_desc ?? 'Permit type not reported'}</span><span className="facility-distance"><span className="number">{c.upstream_distance_km == null ? 'Distance unavailable' : `${c.upstream_distance_km.toFixed(1)} km upstream`}</span> · {c.travel_time?.display ?? 'Travel time unavailable'}</span><span className="violation-count">{c.numeric_violation_count} reported limit violations</span><span>{c.violation_window_label}</span></span></AccordionTrigger><AccordionContent><div className="facility-detail"><p className="muted">Facility ID: <span className="number">{c.facility_id}</span></p><p>{c.location_note}</p>{c.violations.length > 0 && <><h4>Reported violations</h4><div className="violations">{c.violations.map((v, i) => <div key={i}><strong>{v.parameter}</strong><p>{v.violation_desc}</p><time>{v.monitoring_period_end ?? 'Date not reported'}</time></div>)}</div></>}<Button variant="outline" onClick={() => onSelect(c.facility_id)}><MapPin size={16} />Show on map</Button></div></AccordionContent></AccordionItem></motion.div>
}
function ApiTextSection({ value, title, items }: { value: string; title: string; items: string[] }) { return <AccordionItem value={value}><AccordionTrigger>{title}</AccordionTrigger><AccordionContent>{items.length ? <ul className="plain-list">{items.map((text, index) => <li key={index}>{text}</li>)}</ul> : <p>None listed in this report.</p>}</AccordionContent></AccordionItem> }
function HowPage() {
  const steps = [
    { title: 'Detect', text: 'A river camera or water sensor picks up an unusual change.', icon: Eye },
    { title: 'Rule out rain', text: 'Recent rainfall is checked to see whether storm runoff explains the signal.', icon: CloudRain },
    { title: 'Combine camera and sensors', text: 'The camera and gauge must agree within the allowed time window.', icon: Activity },
    { title: 'Trace upstream', text: 'The real river network connects the gauge to upstream reaches.', icon: GitBranch },
    { title: 'Report with AI agents', text: 'The agents organize the evidence and records into a report for a person to review.', icon: Layers },
  ]
  return <div className="editorial-page"><Reveal className="page-intro"><h1>From a signal to a place to look.</h1><p className="lede">Five checks turn an unusual river observation into a reviewable lead.</p></Reveal><div className="how-steps">{steps.map(({ title, text, icon: Icon }, index) => <Reveal key={title} className="how-step"><span className="step-number number">{index + 1}</span><div><h2>{title}</h2><p>{text}</p></div><Icon size={30} strokeWidth={1.4} /></Reveal>)}</div><Reveal className="agents-explained"><h2>The AI, explained</h2><p className="section-intro">Each agent has one job. The backend supplies the facts.</p><div className="agent-explanations">{[['Triage', 'Checks the evidence and rainfall context.'], ['Trace', 'Follows the connected river network upstream.'], ['Compliance', 'Matches facilities with reported permit violations.'], ['Report', 'Presents the evidence, assumptions, and limits.']].map(([title, text], i) => <div key={title}><span className="agent-name">{title}{i < 3 && <ArrowRight size={18} />}</span><p>{text}</p></div>)}</div></Reveal><Reveal className="guardrails"><ShieldCheck size={28} /><div><h2>Guardrails</h2><ul className="plain-list"><li>No invented facilities.</li><li>No invented numbers.</li><li>No blame words.</li><li>A template fallback if the AI fails.</li><li>A human makes the final call.</li></ul></div></Reveal><a className="button-link primary" href="#/investigate">See it in the investigation <ArrowRight size={18} /></a></div>
}
function DataPage({ incident, dataReady }: { incident: IncidentResponse | null; dataReady: Record<string, boolean> }) {
  const real = [['River network', 'USGS NHDPlus HR river geometry and connectivity.', 'nhdplus_gpkg'], ['Gauge readings', 'USGS NWIS measurements from the local baseline snapshot.', 'baseline_snapshot'], ['Rainfall', 'NOAA rainfall in the local baseline snapshot.', 'baseline_snapshot'], ['Permits and violations', 'EPA ECHO permit and reported violation records.', 'echo_data'], ['TRI records', 'EPA annual self-reported releases; not evidence of an individual event.', 'tri_data']]
  return <div className="editorial-page"><Reveal className="page-intro"><h1>Real records.<br />A simulated event.</h1><p className="lede">Know what you’re looking at before deciding what to do with it.</p></Reveal><Reveal className="data-columns"><div><h2><span className="real-dot" />Real</h2>{real.map(([title, text, key]) => <div className="data-item" key={title}><h3>{title}</h3><p>{text}</p><span className="availability">{dataReady[key] ? 'Available in the local data' : 'Availability not confirmed'}</span></div>)}</div><div><h2><span className="sim-dot" />Simulated</h2><div className="data-item"><h3>Spill spike</h3><p>Injected changes to water sensor readings for the demonstration.</p><span className="availability">Dashed amber lines in the charts</span></div><div className="data-item"><h3>Aerial footage</h3><p>A rendered plume clip, created for the spill scenario.</p><span className="availability">Labelled simulated wherever it appears</span></div><p className="honesty-note">Facilities are listed only because they have reported permit violations upstream. SilentSpill never assigns blame; a person reviews the evidence.</p></div></Reveal><Reveal className="limitations-section"><h2>What this report cannot tell you</h2>{incident ? <><p className="muted">Limitations from the latest investigation.</p><ul className="plain-list">{incident.report.limitations.map((limit, i) => <li key={i}>{limit}</li>)}</ul></> : <p>Run a scenario to see its limitations, exactly as returned by the backend. <a href="#/investigate">Open Investigate <ArrowRight size={16} /></a></p>}</Reveal></div>
}
function AboutPage() {
  return <div className="editorial-page about-page"><Reveal className="page-intro"><h1>A weekend to follow the river.</h1><p className="lede">Built by first-time hackers at <span className="todo-name">[TODO: hackathon name]</span> in one weekend.</p></Reveal><Reveal className="about-section"><h2>How we built it</h2><p>We wrote the plan and rules, and AI coding agents (Claude Code and Codex) helped build it step by step.</p><p>The central rule stayed simple: show the evidence and its limits, and leave the final decision to a person.</p></Reveal><Reveal className="about-section"><h2>The tools behind it</h2><div className="stack-grid">{['FastAPI', 'NetworkX', 'Groq', 'Backboard', 'Roboflow', 'React', 'MapLibre', 'shadcn/ui'].map(name => <span key={name}>{name}</span>)}</div></Reveal><Reveal className="about-section"><h2>What we learned</h2><p className="muted">Draft notes for the team to edit.</p><ul className="plain-list"><li>Write the data rules before building the interface.</li><li>Show uncertainty where people make decisions.</li><li>Keep a working fallback when a model is unavailable.</li><li>Make simulated evidence easy to recognize.</li></ul></Reveal></div>
}
export default App

