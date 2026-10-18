# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are Virginia state water-quality staff, including DEQ inspectors, and watershed groups such as Riverkeeper-style river monitors. They use SilentSpill when a river sensor or camera shows a possible problem and they need to decide quickly where to investigate first. In the hackathon demo, judges watch a 2–3 minute walkthrough on one screen; the main flow must be clear at a glance.

## Product Purpose

SilentSpill turns a possible river spill signal into a human-review lead: what may have happened, roughly when it may have entered the river, and which permitted upstream facilities to check first. It combines camera and water-sensor evidence, traces upstream through the real river network, checks real EPA violation records, and considers storms. Every result supports a person’s investigation; it is never a verdict.

## Positioning

SilentSpill combines camera and water-sensor signals, real river-network tracing, and reported EPA facility records to help reviewers prioritize an investigation. It rules out storm runoff where the evidence supports that, and presents confidence, assumptions, and limitations without assigning blame.

## Operating Context

The product is a web-based investigation tool for reviewing a possible event in Virginia waterways. The hackathon demonstration runs as a concise, single-screen walkthrough. Reviewers need to understand the event timeline, storm context, upstream trace, and candidate facilities quickly.

## Capabilities and Constraints

- The spill event, injected sensor spike, and drone/aerial plume footage are simulated. The river network, USGS gauge readings, NOAA rainfall, and EPA records are real local data.
- Every screen must show a visible “SIMULATED — requires human review” banner. Every result is a lead for a person to check, never a verdict.
- Clearly label real and simulated data differently.
- Never use “caused,” “responsible,” “polluter,” “source,” or “culprit” in product language. Describe facilities as “facilities with reported violations near this reach” and describe evidence as “consistent with.” Never imply a facility caused an event.
- When storms explain the signal, show “Storm runoff, no facilities flagged” and do not show a candidate list.
- When facilities are listed, show the disclaimer text supplied by the API.
- Facility names, numbers, records, and report text must come from the backend API. Do not invent or hard-code data, and do not have the frontend rewrite the report.
- The system should show confidence and limitations and leave the final decision to a person.

## Brand Commitments

The product should feel like a calm investigation tool rather than a dramatic alarm. Its language is honest, non-accusatory, and clear about uncertainty and human responsibility.

## Evidence on Hand

- Real local data: Virginia NHDPlus HR river-network data, USGS NWIS gauge data, NOAA rainfall, EPA ECHO facility and violation records, EPA TRI releases, and terrain data.
- Simulated data: rendered plume footage and the injected spill sensor spike. No real spill footage or paired spill/video sensor dataset is available.
- Facility coordinates are facility locations, not confirmed outfall locations. EPA violations are reported/alleged records; TRI releases are annual self-reported records and cannot be matched to a live plume.
- The backend API is the authority for facilities and incident report text; the frontend must display API content without rewriting it.

## Product Principles

- Treat every detection as a lead that requires human review.
- Separate real observations from simulated evidence visibly.
- State uncertainty, assumptions, and limitations plainly.
- Use only locally available, API-backed facts; do not invent evidence.
- Help reviewers prioritize where to look without assigning blame.
