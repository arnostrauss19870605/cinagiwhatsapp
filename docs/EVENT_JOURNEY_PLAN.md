# Event journey — build plan

**Event:** Annual Product Update & Launch · Wednesday 30 September 2026 · Bryanston · registration 08h30
**Invite send:** 15 September 2026 (T-2w) — **28 days from today**
**Decisions taken 18 Aug:** run on the existing number, build inside the platform, core journey + P1 add-ons
**Domain:** `brokers.cinagi.co.za`

This plan supersedes §4 (architecture), §13 (delivery) and D1 of the functional specification. Everything
else in that document stands as written.

---

## 1. What changes from the specification

### D1 is closed: no BSP

You are already on the **WhatsApp Cloud API directly** — WABA `1807786146881003`, number
`+27 84 235 8896` (phone number ID `1323270194191931`), business verified, display name approved,
payment method on file, messaging limit **10,000 business-initiated conversations per rolling 24h**
against an expected 200–600 guests. A BSP would add licence cost, a middleman on the critical path,
and a second system to integrate. Delete the BSP row from §4 and the procurement task from §13.

### Flows are mostly ported, not built

The RSVP Flow (F1.2) needs Meta's encrypted data-exchange endpoint: RSA-OAEP key exchange, AES-128-GCM
payloads, and the response encrypted with a **bitwise-inverted IV**. That implementation already exists
and works in the Wellniciti codebase (shop checkout and consult booking). We port it rather than
discover it. Budget: 2 days including the Flow JSON, not the 5+ a first build would cost.

### The 24-hour window is the whole cost model — and it changes check-in

Everything a guest does inside 24 hours of their last inbound message is free-form and free of template
cost. Everything else needs an approved template. That single rule reshapes two things:

**Entry is guest-initiated by design.** `wa.me/27842358896?text=RSVP-<token>` means the guest opens the
conversation, so the greeting, the RSVP Flow, the confirmation, the ticket and the concierge all run
inside the free window. No marketing template needed to start a journey — which also sidesteps the
opt-in problem in §3.3 entirely.

**Check-in must not be steward-initiated.** F5.1 as written has a steward scanning the guest's ticket,
and the server then pushing a welcome message — but that guest last messaged us at RSVP, weeks earlier,
so their window is shut and the message would need a template. Fix: put a **venue QR at the door that
the guest scans**, encoding `wa.me/27842358896?text=CHECKIN-<token>`. The guest's scan is an inbound
message: it checks them in, opens a fresh 24-hour window, and everything for the rest of the day —
content drops, polls, quiz, Q&A, feedback — is free-form and free. The steward's own scanner stays for
door count and for guests who can't scan (F5.4).

This is not a cost optimisation at the margins. It is the difference between roughly a dozen billable
template sends per guest and roughly four.

---

## 2. Number and workspace design

You chose to reuse `+27 84 235 8896`. Consequences, stated plainly:

- One number is one workspace in the platform. Event guests and broker support conversations land in
  the **same inbox**, so the event needs its own lane: every event conversation is tagged `event-2026`
  on creation, the inbox gets a saved filter for it, and the events team works that filter while
  support works theirs.
- The event drip and the support line **share one quality rating and one messaging limit**. A 600-guest
  marketing burst that gets blocked or reported damages the number your brokers use every day. Mitigations:
  cap marketing templates at one per guest per week (§11), send the drip in batches with a delay rather
  than one burst, and watch the quality rating daily in the week of the invite — the platform surfaces it
  on the dashboard.
- Guests see the sender as **"Cinagi Broker Support"**. If that reads oddly for an event invitation, the
  display name can be changed in WhatsApp Manager, but it re-enters approval and affects the support line
  too. Worth a decision this week rather than the week of the send.

**Recommendation I'll keep making:** if the guest list turns out to include people who are not brokers,
add a second number under the same WABA before 15 September. It is a two-day path, and it removes the
shared-quality-rating risk entirely.

---

## 3. Template pack — submit this week

Approval is the critical path. Marketing templates on a number with little sending history get read
carefully, and a rejection costs days. Submit all of these together, in `en`, and expect to iterate on
one or two.

Category matters for both approval and price. A message about an event the guest has already registered
for is defensible as **UTILITY**; anything that promotes the event to someone who has not yet said yes is
**MARKETING**.

| # | Name | Category | Purpose | Variables |
|---|------|----------|---------|-----------|
| 1 | `event_invite` | MARKETING | Push invite to consented guest list | 1 first name |
| 2 | `event_rsvp_reminder` | MARKETING | Nudge non-responders (T-1w) | 1 first name |
| 3 | `event_agenda_reveal` | MARKETING | T-2w agenda drop, image header | 1 first name, 2 interest topic |
| 4 | `event_teaser` | MARKETING | T-1w "three launches" hook, image header | 1 first name |
| 5 | `event_rsvp_confirmed` | UTILITY | Ticket + QR fallback if window shut | 1 first name, 2 guest number |
| 6 | `event_logistics` | UTILITY | T-1d logistics + re-confirmation buttons | 1 first name |
| 7 | `event_morning` | UTILITY | Event morning, QR re-send | 1 first name |
| 8 | `event_feedback` | UTILITY | Post-event feedback, if window shut | 1 first name |
| 9 | `event_recap` | UTILITY | Recap pack delivery | 1 first name |
| 10 | `event_demo_booking` | MARKETING | T+2 demo slots | 1 first name |

### Copy, ready to paste

Buttons are listed under each. Samples are what Meta needs in the "sample content" fields — use these
exactly; placeholder-looking samples get rejected.

**1. `event_invite` · MARKETING · en**
> Hi {{1}}, Cinagi is hosting its Annual Product Update and Launch on Wednesday 30 September in Bryanston, and we would like you there.
>
> Three product announcements, live demos, and breakfast from 08h30.
>
> Tap below and I'll take your RSVP right here in this chat — it takes about a minute.

Buttons: `RSVP now` (quick reply) · `Not this time` (quick reply)
Sample: `{{1}} = Thabo`

**2. `event_rsvp_reminder` · MARKETING · en**
> Hi {{1}}, seats for the Cinagi Product Launch on 30 September are filling up and I have not heard back from you yet.
>
> Tap below if you would like one — or let me know if you would rather join the online stream.

Buttons: `Count me in` · `Send stream link` · `Not this time`
Sample: `{{1}} = Thabo`

**3. `event_agenda_reveal` · MARKETING · en · header: IMAGE**
> Hi {{1}}, the agenda for 30 September is out.
>
> Based on what you told me, the session on {{2}} is the one to watch — it runs mid-morning, right after the keynote.
>
> Full agenda in the image above. Ask me anything about it.

Buttons: `Ask about the agenda`
Samples: `{{1}} = Thabo`, `{{2}} = API integrations`

**4. `event_teaser` · MARKETING · en · header: IMAGE**
> Hi {{1}}, one week to go.
>
> We are announcing three things on 30 September. One of them has been the single most requested item from brokers for two years running.
>
> Want to guess which?

Buttons: `Guess the announcement` · `See the agenda`
Sample: `{{1}} = Thabo`

**5. `event_rsvp_confirmed` · UTILITY · en · header: IMAGE (the QR ticket)**
> You are confirmed, {{1}} — you are guest #{{2}}.
>
> Wednesday 30 September, registration from 08h30, Bryanston.
>
> Show the code above at the door. It is also your entry into the lucky draw.

Buttons: `Add to calendar` · `Send venue pin` · `Ask a question`
Samples: `{{1}} = Thabo`, `{{2}} = 84`

**6. `event_logistics` · UTILITY · en**
> Hi {{1}}, we are on for tomorrow.
>
> Doors and breakfast from 08h30, keynote at 09h15. Parking is free in the basement — take the P2 level and the lifts to reception.
>
> Still joining us?

Buttons: `I'm still in` · `Plans changed`
Sample: `{{1}} = Thabo`

**7. `event_morning` · UTILITY · en · header: IMAGE (the QR ticket)**
> Morning {{1}} — today is the day.
>
> Doors are open from 08h30 and here is your code again. See you shortly.

Buttons: `Send venue pin` · `Running late`
Sample: `{{1}} = Thabo`

**8. `event_feedback` · UTILITY · en**
> Hi {{1}}, thank you for joining us today.
>
> Two quick questions and then I'll send you everything from the day. How was it?

Buttons: `Excellent` · `Good` · `Could be better`
Sample: `{{1}} = Thabo`

**9. `event_recap` · UTILITY · en · header: DOCUMENT**
> Here is everything from Wednesday, {{1}} — the slides, the demo links and the photos.
>
> This chat stays open. Ask me anything about what we launched, any time.

Buttons: `Book a demo` · `Talk to my account manager`
Sample: `{{1}} = Thabo`

**10. `event_demo_booking` · MARKETING · en**
> Hi {{1}}, you asked good questions about the Co-pilot last week.
>
> Would you like 30 minutes with the team to see it against your own book? Pick a slot below.

Buttons: `See available times` · `Not right now`
Sample: `{{1}} = Thabo`

### Notes on approval

- A URL button whose suffix contains `:` is silently dropped by WhatsApp. If any button links out, base64url-wrap the token.
- Language must be `en` exactly, everywhere, forever. There is no `en_ZA`.
- An approved template accepts one edit per 24 hours, so get the copy signed off by compliance **before** submitting.
- The FSP disclosure line ("Cinagi (Pty) Ltd, authorised FSP 50104") belongs in the recap PDF and invite materials, not inside every message — §12.7.

---

## 4. RSVP Flow (F1.2)

Three screens, published in WhatsApp Manager, backed by an encrypted data-exchange endpoint at
`https://brokers.cinagi.co.za/wa/flows/rsvp/`.

- **S1 Attendance** — radio: joining in person / joining the online stream / can't make it
- **S2 Details** (skipped for decline) — dietary single-select (none / halaal / kosher / vegetarian / vegan / other + text); party size (just me / +1 / +2)
- **S3 Interests** — multi-select over AI onboarding, API integrations, advisor tools, pricing, client portal; plus optional free text

Terminal screen returns the payload to our endpoint; we persist within 5 s (F1.3), mint the ticket token,
and reply with the confirmation and QR in the same window.

Natural-language RSVP (F1.6) runs ahead of the Flow: the concierge extracts attendance and party size from
free text, confirms back, and only opens the Flow for what is still missing.

---

## 5. The `events` app

New Django app on the existing platform. It reuses the transport, webhook, idempotency, contacts,
conversations, inbox, template library, agent handoff and audit log that already exist.

```
Event              name, starts_at, venue, venue_lat/lng, capacity, workspace FK,
                   stream_url, checkin_opens_at, state
Guest              event FK, contact FK (platform contact), first_name, last_name, company,
                   segment (client|prospect|vip|press), invite_token, language,
                   rsvp_status (none|attending|stream|declined|waitlist), party_size,
                   dietary, interests[], flow_raw_response,
                   ticket_token, guest_number, draw_entries, checkin_ts,
                   feedback_rating, feedback_theme, sentiment, referral_of FK,
                   recap_delivered_ts, demo_booking_ts
StationVisit       guest FK, station (1-4), scanned_at            -- A3 passport
Poll               event FK, question, options[], opens_at, closes_at, kind (poll|quiz)
PollVote           poll FK, guest FK, option_index, is_correct, created_at
Question           event FK, guest FK, body, audio_ref, cluster_id, queue_number, asked_at
QuestionCluster    event FK, theme, representative_text, count
DrawEntry          guest FK, reason (ticket|quiz|passport|referral|prediction), created_at
JourneyEvent       guest FK, step, payload, created_at          -- the audit trail of the journey
```

**Journey state machine** — `invited → engaged → rsvp_pending → confirmed → reconfirmed → checked_in →
participating → feedback_given → alumni`, with `declined`, `waitlisted` and `stopped` as terminal side
states. Every transition writes a `JourneyEvent`, so "why did this guest get that message" is always
answerable, the same way `AssignmentLog` answers it for the inbox.

**Message routing.** Inbound event messages are claimed by the events resolver before the support
pipeline sees them: token prefixes (`RSVP-`, `CHECKIN-`, `STATION-`) first, then Flow responses, then
button and list replies against the current step, then free text to the concierge. Anything the concierge
will not answer escalates into the normal inbox with the `event-2026` tag, where the events team picks it
up alongside its context.

**Organiser console** — a page per event: live arrivals, push a content drop to checked-in guests, open
and close a poll, run the quiz and execute the draw from an auditable ledger, moderate the Q&A themes,
and a big obvious pause switch for all automation.

**Stage screen** — a separate read-only page consuming the same API, for the venue AV feed: live poll
results, Q&A themes, word cloud.

---

## 6. Schedule — 28 days to invite send

| Week | Work | Gate |
|---|---|---|
| **w/c 18 Aug** (now) | Compliance sign-off on the ten templates; submit all ten to Meta; guest list into the platform; `events` app models and journey state machine; server deployed at brokers.cinagi.co.za with the webhook live | Templates submitted by **22 Aug** |
| **w/c 25 Aug** | RSVP Flow built and published (port the Wellniciti encryption); ticket QR minting and signature verification; confirmation journey end to end on a test cohort; knowledge base v1 authored | A test guest completes RSVP → ticket, on the real number |
| **w/c 1 Sep** | AI concierge live against the KB with guardrails and escalation; NL RSVP extraction; venue and station QR generation; check-in flow; A4 voice notes; A11 tap-to-call | Concierge answering ≥ 90% of a 40-question test set |
| **w/c 8 Sep** | Drip scheduling with quiet hours and the marketing frequency cap; waitlist logic; organiser console; **full rehearsal on a test cohort of 20** | Rehearsal passes → **invite send 15 Sep** |
| **w/c 15 Sep** | Invite live, RSVPs flowing, monitor quality rating daily; polls, quiz, Q&A clustering, stage screen; A1 co-pilot sandbox; A3 passport | Poll load-tested at 2× expected concurrency |
| **w/c 22 Sep** | Feedback, recap, demo booking; content lockdown; dry-run check-in with stewards; freeze **28 Sep** | Freeze |
| **30 Sep** | Event. T+1 sentiment report, T+2 demo-booking send, retro | |

The gate that matters is **22 August**. If the ten templates are not with Meta by Friday, the 15 September
invite date starts to move, and every date after it moves too.

---

## 7. Risks

| Risk | Handling |
|---|---|
| Template rejection close to 15 Sep | Submit all ten this week; keep the copy plain and free of product claims; have a fallback invite that is pure logistics |
| Quality rating drops and takes the support line with it | Batch the drip, cap marketing at one per guest per week, watch the dashboard daily, and be ready to pause the drip |
| Guests reply in a burst the concierge cannot ground | KB coverage tested against a written question set before invite send; escalation to the events team is always one hop away |
| Flow endpoint fails at RSVP peak | The natural-language RSVP path (F1.6) is a genuine fallback, not a nicety — a guest can RSVP entirely by typing |
| Venue wifi or signal fails at check-in | Scanner caches offline and reconciles; stewards have a printed guest list; the door does not depend on WhatsApp |
| Event traffic swamps the support inbox | `event-2026` tag applied on creation, saved filter per team, automation owns event conversations until escalation |

---

## 8. Open decisions

D1 is closed (no BSP). Of the rest:

| # | Decision | Needed by |
|---|---|---|
| D2 | CRM of record for sync — or run the guest list in the platform and export | 28 Aug |
| D3 | Afrikaans (A13): doubles the template pack to twenty. Recommendation: **no** for this event | 22 Aug — it changes what we submit this week |
| D4 | Co-pilot sandbox demo dataset and compliance boundary | 4 Sep |
| D5 | Photo delivery (A10) — recommendation: next event, and manual tagging when it happens, never face matching | closed if we hold to P1 |
| D6 | Waitlist policy: auto-offer order and cutoff time | 11 Sep |
| D7 | Lucky-draw prize and CPA competition terms | 11 Sep |
| **D8** | **Sender name.** Guests will see "Cinagi Broker Support". Change it, accept it, or add a second number | **22 Aug** |
