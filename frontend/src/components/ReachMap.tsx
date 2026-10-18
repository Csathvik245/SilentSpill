import { useEffect, useRef } from 'react'
import { LngLatBounds, Map as MapLibreMap, Marker, NavigationControl, type GeoJSONSource } from 'maplibre-gl'
import { setWorkerUrl } from 'maplibre-gl'
import mapLibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { CandidateFacility, ReachResponse } from '../lib/types'

setWorkerUrl(mapLibreWorkerUrl)

const DEM = 'watershed-dem'
const SHADE_DEM = 'watershed-shade-dem'
const FLOW_STEPS = 48
// Shift the visible dash toward increasing line distance: upstream → downstream.
const FLOW_DASHES = Array.from({ length: FLOW_STEPS }, (_, i) => {
  const offset = i * 8 / FLOW_STEPS
  return offset <= 5.5 ? [0, offset, 2.5, 5.5 - offset] : [offset - 5.5, 5.5, 8 - offset, 0]
})
// Solve the shared cubic-bezier(.22, 1, .36, 1) for MapLibre's easing callback.
const easeOut = (t: number) => {
  let s = t
  for (let i = 0; i < 6; i++) {
    const x = 3 * (1 - s) ** 2 * s * .22 + 3 * (1 - s) * s ** 2 * .36 + s ** 3
    const derivative = 3 * (1 - s) ** 2 * .22 + 6 * (1 - s) * s * (.36 - .22) + 3 * s ** 2 * (1 - .36)
    s = Math.max(0, Math.min(1, s - (x - t) / derivative))
  }
  return 1 - (1 - s) ** 3
}

function downstreamBearing(upstream: [number, number], downstream: [number, number]) {
  const radians = Math.PI / 180
  const latitude1 = upstream[1] * radians
  const latitude2 = downstream[1] * radians
  const longitude = (downstream[0] - upstream[0]) * radians
  return Math.atan2(Math.sin(longitude) * Math.cos(latitude2), Math.cos(latitude1) * Math.sin(latitude2) - Math.sin(latitude1) * Math.cos(latitude2) * Math.cos(longitude)) / radians
}

type Props = { reach: ReachResponse | null; facilities: CandidateFacility[]; activeFacility: string | null; hoveredFacility?: string | null; pulse: number; onSelect: (id: string) => void }
export function ReachMap({ reach, facilities, activeFacility, hoveredFacility, pulse, onSelect }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<MapLibreMap | null>(null)
  const initialized = useRef(false)
  const selectionTimeout = useRef<number | undefined>(undefined)
  const plumeFrame = useRef(0)
  const markers = useRef<Map<string, HTMLButtonElement>>(new Map())
  useEffect(() => {
    if (!container.current || !reach) return
    const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)')
    let reduced = motionPreference.matches
    let flowFrame = 0
    let revealFrame = 0
    let cameraTimeout: number | undefined
    let disposed = false
    let terrainFailed = false
    let syncMotion = () => {}
    const pinInstances: { marker: Marker; button: HTMLButtonElement; coordinates: [number, number] }[] = []
    const mainName = reach.reach.properties.gnis_name
    const features = [...(reach.upstream_geojson?.features ?? [reach.reach])]
      .map((feature) => ({ ...feature, properties: { ...feature.properties,
        cumulative_km: Number(feature.properties.cumulative_km ?? 0),
        main_stem: feature.properties.nhdplusid === reach.gauge.linked_reach_nhdplusid || Boolean(mainName && feature.properties.gnis_name === mainName),
      } }))
      .sort((a, b) => a.properties.cumulative_km - b.properties.cumulative_km)
    const mainFeatures = features.filter((feature) => feature.properties.main_stem)
    const upstreamEnd = mainFeatures.at(-1)?.geometry.coordinates[0] ?? reach.reach.geometry.coordinates[0]
    const valleyBearing = downstreamBearing(upstreamEnd, [reach.gauge.longitude, reach.gauge.latitude])
    const colors = getComputedStyle(container.current)
    const water = colors.getPropertyValue('--color-river').trim()
    const warm = colors.getPropertyValue('--color-simulated').trim()
    const paper = colors.getPropertyValue('--color-paper').trim()
    const field = colors.getPropertyValue('--color-field').trim()
    const instance = new MapLibreMap({
      container: container.current,
      style: { version: 8, sources: { osm: { type: 'raster', tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], tileSize: 256, attribution: '© OpenStreetMap contributors' } }, layers: [{ id: 'paper', type: 'raster', source: 'osm', paint: { 'raster-brightness-max': .23, 'raster-saturation': -1 } }] },
      center: [reach.gauge.longitude, reach.gauge.latitude], zoom: 5, dragRotate: true, maxPitch: 75,
    })
    map.current = instance
    instance.addControl(new NavigationControl({ showCompass: true, visualizePitch: true }), 'top-right')
    instance.touchZoomRotate.enableRotation()
    const flattenTerrain = () => {
      if (disposed || terrainFailed) return
      terrainFailed = true
      instance.setTerrain(null)
      if (instance.getLayer('watershed-shade')) instance.removeLayer('watershed-shade')
      if (instance.getSource(DEM)) instance.removeSource(DEM)
      if (instance.getSource(SHADE_DEM)) instance.removeSource(SHADE_DEM)
    }
    instance.on('error', (event) => {
      if ([DEM, SHADE_DEM].includes((event as typeof event & { sourceId?: string }).sourceId ?? '') || /terrarium|watershed.*dem/.test(event.error.message)) flattenTerrain()
      else console.error(event.error)
    })
    instance.on('load', () => {
      instance.setSky({ 'sky-color': paper, 'horizon-color': paper, 'fog-color': paper, 'fog-ground-blend': .25, 'horizon-fog-blend': .7, 'sky-horizon-blend': .6 })
      try {
        instance.addSource(DEM, { type: 'raster-dem', tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'], encoding: 'terrarium', tileSize: 256, maxzoom: 14, attribution: 'Elevation: Mapzen / AWS Terrain Tiles' })
        instance.addSource(SHADE_DEM, { type: 'raster-dem', tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'], encoding: 'terrarium', tileSize: 256, maxzoom: 14 })
        instance.addLayer({ id: 'watershed-shade', type: 'hillshade', source: SHADE_DEM, paint: { 'hillshade-exaggeration': .22, 'hillshade-shadow-color': paper, 'hillshade-highlight-color': field, 'hillshade-accent-color': paper, 'hillshade-illumination-direction': 315 } })
        instance.setTerrain({ source: DEM, exaggeration: 1.6 })
      } catch { flattenTerrain() }
      // Dash lengths use ordinary line distance; lineMetrics would distort their scale.
      instance.addSource('upstream-path', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
      instance.addLayer({ id: 'river-glow', type: 'line', source: 'upstream-path', paint: { 'line-color': water, 'line-width': 8, 'line-blur': 4, 'line-opacity': .24, 'line-opacity-transition': { duration: 300, delay: 0 }, 'line-width-transition': { duration: 360, delay: 0 } } })
      instance.addLayer({ id: 'river-path', type: 'line', source: 'upstream-path', paint: { 'line-color': water, 'line-width': ['case', ['get', 'main_stem'], 3, 1.4], 'line-opacity': ['case', ['get', 'main_stem'], .45, .22] } })
      instance.addLayer({ id: 'plume-glow', type: 'line', source: 'upstream-path', filter: ['==', ['get', 'main_stem'], true], paint: { 'line-color': warm, 'line-width': 17, 'line-blur': 7, 'line-opacity': 0, 'line-opacity-transition': { duration: 0, delay: 0 } } })
      for (const main of [false, true]) instance.addLayer({ id: main ? 'main-flow' : 'tributary-flow', type: 'line', source: 'upstream-path', filter: ['==', ['get', 'main_stem'], main], layout: { 'line-cap': 'butt', 'line-join': 'round' }, paint: { 'line-color': water, 'line-width': main ? 2.4 : 1.2, 'line-opacity': main ? .98 : .48, 'line-dasharray': FLOW_DASHES[0], 'line-dasharray-transition': { duration: 0, delay: 0 } } })
      const pointFeatures = facilities.map((item) => ({ type: 'Feature' as const, geometry: { type: 'Point' as const, coordinates: [item.longitude, item.latitude] }, properties: { id: item.facility_id, name: item.name } }))
      pointFeatures.push({ type: 'Feature', geometry: { type: 'Point', coordinates: [reach.gauge.longitude, reach.gauge.latitude] }, properties: { id: reach.gauge.site_id, name: reach.gauge.name ?? '' } })
      instance.addSource('review-points', { type: 'geojson', data: { type: 'FeatureCollection', features: pointFeatures } })
      instance.addLayer({ id: 'point-halo', type: 'circle', source: 'review-points', paint: { 'circle-radius': ['case', ['==', ['get', 'id'], reach.gauge.site_id], 7, 0], 'circle-color': ['case', ['==', ['get', 'id'], reach.gauge.site_id], water, warm], 'circle-opacity': .95, 'circle-stroke-color': paper, 'circle-stroke-width': 2, 'circle-radius-transition': { duration: 360, delay: 0 }, 'circle-opacity-transition': { duration: 360, delay: 0 } } })
      instance.addLayer({ id: 'selection-pulse', type: 'circle', source: 'review-points', filter: ['==', ['get', 'id'], '__none__'], paint: { 'circle-radius': 5, 'circle-color': warm, 'circle-opacity': .35, 'circle-stroke-color': warm, 'circle-stroke-width': 1, 'circle-radius-transition': { duration: reduced ? 0 : 360, delay: 0 }, 'circle-opacity-transition': { duration: reduced ? 0 : 360, delay: 0 } } })
      facilities.forEach((facility, index) => {
        const anchor = document.createElement('div')
        const button = document.createElement('button')
        button.className = 'rank-marker'
        button.textContent = String(index + 1)
        button.setAttribute('aria-label', `Facility ${index + 1}: ${facility.name}`)
        button.addEventListener('click', () => onSelect(facility.facility_id))
        anchor.append(button)
        const coordinates: [number, number] = [facility.longitude, facility.latitude]
        const marker = new Marker({ element: anchor, anchor: 'center' }).setLngLat(coordinates).addTo(instance)
        pinInstances.push({ marker, button, coordinates })
        button.classList.toggle('is-selected', facility.facility_id === activeFacility)
        markers.current.set(facility.facility_id, button)
      })
      // Separate nearby rank labels in screen space; the map coordinates stay unchanged.
      const spacePins = () => {
        const placed: { x: number; y: number }[] = []
        pinInstances.forEach(({ marker, coordinates }) => {
          const point = instance.project(coordinates)
          let offset = 0
          while (placed.some(other => Math.abs(other.x - point.x - offset) < 32 && Math.abs(other.y - point.y) < 32)) offset += 34
          marker.setOffset([offset, 0])
          marker.getElement().classList.toggle('offset-pin', offset > 0)
          marker.getElement().style.setProperty('--pin-offset', `${offset}px`)
          placed.push({ x: point.x + offset, y: point.y })
        })
      }
      instance.on('move', spacePins)
      spacePins()
      instance.on('click', 'point-halo', (event) => {
        const feature = event.features?.[0]
        const id = feature?.properties?.id
        if (typeof id === 'string' && facilities.some((candidate) => candidate.facility_id === id)) onSelect(id)
      })
      instance.on('mouseenter', 'point-halo', () => { instance.getCanvas().style.cursor = 'pointer' })
      instance.on('mouseleave', 'point-halo', () => { instance.getCanvas().style.cursor = '' })
      const bounds = new LngLatBounds()
      features.forEach((feature) => feature.geometry.coordinates.forEach((coordinate) => bounds.extend(coordinate)))
      bounds.extend([reach.gauge.longitude, reach.gauge.latitude])
      if (facilities.length) facilities.forEach((item) => bounds.extend([item.longitude, item.latitude]))
      if (reduced) instance.fitBounds(bounds, { padding: 70, maxZoom: 12, pitch: 55, bearing: valleyBearing, duration: 0 })
      else {
        instance.fitBounds(bounds, { padding: 70, maxZoom: 12, duration: 0 })
        const targetZoom = instance.getZoom()
        instance.setZoom(Math.max(4, instance.getZoom() - 4))
        cameraTimeout = window.setTimeout(() => instance.flyTo({ center: bounds.getCenter(), zoom: Math.min(12, targetZoom), pitch: 55, bearing: valleyBearing, duration: 1000, easing: easeOut, essential: false }), 150)
      }
      initialized.current = true
      const source = instance.getSource('upstream-path') as GeoJSONSource
      if (reduced || features.length < 2) source.setData({ type: 'FeatureCollection', features })
      else {
        const maxDistance = Math.max(1, ...features.map((feature) => Number(feature.properties.cumulative_km ?? 0)))
        const start = performance.now()
        const paintFrame = (now: number) => {
          if (map.current !== instance) return
          const progress = Math.min(1, (now - start) / 2100)
          const distance = maxDistance * progress
          let visibleCount = 0
          while (visibleCount < features.length && Number(features[visibleCount].properties.cumulative_km ?? 0) <= distance) visibleCount += 1
          source.setData({ type: 'FeatureCollection', features: features.slice(0, Math.max(1, visibleCount)) })
          if (progress < 1) revealFrame = window.requestAnimationFrame(paintFrame)
        }
        revealFrame = window.requestAnimationFrame(paintFrame)
      }
      let previousMain = -1
      let previousTributary = -1
      const flow = (now: number) => {
        if (disposed || reduced) return
        if (!document.hidden) {
          const mainStep = Math.floor(now / 1800 * FLOW_STEPS) % FLOW_STEPS
          const tributaryStep = Math.floor(now / 3600 * FLOW_STEPS) % FLOW_STEPS
          if (mainStep !== previousMain) instance.setPaintProperty('main-flow', 'line-dasharray', FLOW_DASHES[mainStep])
          if (tributaryStep !== previousTributary) instance.setPaintProperty('tributary-flow', 'line-dasharray', FLOW_DASHES[tributaryStep])
          previousMain = mainStep
          previousTributary = tributaryStep
        }
        flowFrame = requestAnimationFrame(flow)
      }
      syncMotion = () => {
        reduced = motionPreference.matches
        cancelAnimationFrame(flowFrame)
        if (reduced) {
          clearTimeout(cameraTimeout)
          cancelAnimationFrame(revealFrame)
          cancelAnimationFrame(plumeFrame.current)
          plumeFrame.current = 0
          source.setData({ type: 'FeatureCollection', features })
          instance.setPaintProperty('main-flow', 'line-dasharray', FLOW_DASHES[0])
          instance.setPaintProperty('tributary-flow', 'line-dasharray', FLOW_DASHES[0])
          instance.setPaintProperty('plume-glow', 'line-opacity', 0)
          instance.stop()
        } else { previousMain = -1; previousTributary = -1; flowFrame = requestAnimationFrame(flow) }
      }
      motionPreference.addEventListener('change', syncMotion)
      if (!reduced) flowFrame = requestAnimationFrame(flow)
    })
    return () => { disposed = true; cancelAnimationFrame(flowFrame); cancelAnimationFrame(revealFrame); cancelAnimationFrame(plumeFrame.current); plumeFrame.current = 0; clearTimeout(cameraTimeout); motionPreference.removeEventListener('change', syncMotion); window.clearTimeout(selectionTimeout.current); markers.current.clear(); instance.remove(); map.current = null; initialized.current = false }
  }, [reach, facilities, onSelect])

  useEffect(() => {
    markers.current.forEach((button, id) => {
      button.classList.toggle('is-hovered', id === hoveredFacility)
      button.classList.toggle('is-selected', id === activeFacility)
    })
  }, [hoveredFacility, activeFacility])

  useEffect(() => {
    const instance = map.current
    if (!instance || !initialized.current || !activeFacility) return
    const point = facilities.find((candidate) => candidate.facility_id === activeFacility)
    if (point) {
      const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      instance.flyTo({ center: [point.longitude, point.latitude], zoom: Math.max(instance.getZoom(), 11), duration: reduced ? 0 : 400, easing: easeOut })
      if (instance.getLayer('selection-pulse')) {
        instance.setFilter('selection-pulse', ['==', ['get', 'id'], activeFacility])
        instance.setPaintProperty('selection-pulse', 'circle-radius', reduced ? 9 : 6)
        instance.setPaintProperty('selection-pulse', 'circle-opacity', .55)
        window.clearTimeout(selectionTimeout.current)
        if (!reduced) selectionTimeout.current = window.setTimeout(() => {
          instance.setPaintProperty('selection-pulse', 'circle-radius', 17)
          instance.setPaintProperty('selection-pulse', 'circle-opacity', 0)
          selectionTimeout.current = window.setTimeout(() => {
            instance.setFilter('selection-pulse', ['==', ['get', 'id'], '__none__'])
            instance.setPaintProperty('selection-pulse', 'circle-radius', 5)
            instance.setPaintProperty('selection-pulse', 'circle-opacity', .35)
          }, 400)
        }, 60)
      }
    }
  }, [activeFacility, facilities])

  useEffect(() => {
    const instance = map.current
    if (!instance?.getLayer('plume-glow') || !pulse || plumeFrame.current) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      instance.setPaintProperty('plume-glow', 'line-opacity', .3)
      return
    }
    const start = performance.now()
    const extent = reach?.max_km ?? 1
    // Cumulative distance gives the replay cue a soft spreading front without inventing a plume location.
    const spread = (now: number) => {
      if (map.current !== instance) return
      const progress = Math.min(1, (now - start) / 2200)
      const intensity = progress > .75 ? (1 - progress) * 4 * .6 : .6
      instance.setPaintProperty('plume-glow', 'line-opacity', ['*', intensity, ['interpolate', ['linear'], ['-', extent * progress / .75, ['get', 'cumulative_km']], 0, 0, extent * .12, 1]])
      if (progress < 1) plumeFrame.current = requestAnimationFrame(spread)
      else { plumeFrame.current = 0; instance.setPaintProperty('plume-glow', 'line-opacity', 0) }
    }
    plumeFrame.current = requestAnimationFrame(spread)
  }, [pulse, reach])

  return <div ref={container} className="map-canvas" style={{ filter: 'none' }} aria-label="Interactive 3D watershed map with downstream water flow. Drag with the right mouse button to rotate." />
}
