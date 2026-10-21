# Implementation Plan

## Objective

Create a small, phone-friendly inventory app for retail-return items. The owner can enter and verify items, attach identification and pricing evidence, and choose which items appear in a searchable public catalog for local buyers.

## Guiding decisions

- Keep AI identification and market-price results as editable suggestions; the owner confirms item identity, condition, and asking price.
- Track product/retail information, resale-price evidence, and asking price separately, with sources and dates where available.
- Keep the public catalog read-only and limited to explicitly published listing data.
- Start with the smallest complete workflow. Add external APIs only after the manual workflow is useful.

## Milestones

### 0. Confirm scope and constraints

- Confirm where the app will run and who needs access to owner/admin functions.
- **Decided:** The initial catalog should be internet-accessible to buyers.
- **Decided:** The private management area should support the owner and a small set of invited helpers.
- **Decided:** Host the app on a computer at home.
- **Decided:** The home computer can stay powered on and connected to the internet most of the time.
- **Decided:** Use condition labels `New/sealed`, `Open box`, `Used/good`, `Used/fair`, and `For parts/untested`.
- **Decided:** Listing availability states are `Available`, `Reserved`, `Sold`, and `Hidden`.
- **Decided:** Track identical products as one listing with a quantity; allow separate item records when individual condition or history needs tracking.
- **Decided:** Support reserving a specific number of units and show the remaining available quantity.
- **Decided:** Include photo upload in the first usable version; AI identification and automated pricing can follow later.
- **Decided:** Categories are managed by the owner/helper, with examples such as Lighting, Tools, and Home.
- **Decided:** Sold listings should no longer appear in the public catalog.
- **Decided:** Track each item's physical storage location, including nested locations such as room, shelf, and bin.
- **Decided:** The public catalog should promote garage-sale dates and location and offer buyers text and email contact options.
- **Decided:** Buyer contact actions should open the buyer's SMS or email app with a prefilled message, rather than adding a contact-form service in v1.
- **Decided:** Support multiple future garage-sale events, each with its own dates and public sale details.
- **Decided:** Show the exact garage-sale address publicly only while sale dates are active.
- **Decided:** Use FastAPI for the backend, SQLite for persistence, and Bokeh for the frontend.
- **Decided:** Back up the database and photos automatically to a separate local drive.
- **Deferred:** Choose the sign-in provider later; keep authentication behind a replaceable boundary and support individually invited helpers.
- **Deferred:** Choose between a tunnel service and router port forwarding for internet access. Keep the application portable so this can be decided at deployment.
- Plan host availability and backup restore testing. A separate local drive helps with drive failure but is not off-site disaster recovery. Do not expose the owner/admin service or database directly to the internet.

**Exit:** The first release has a bounded deployment target and a clear definition of what is public.

### 1. Build the manual inventory MVP

- Create the application shell and persistent item store.
- Keep the FastAPI backend, SQLite persistence, and Bokeh interface in clear, testable boundaries.
- Keep authentication replaceable until the sign-in provider is selected; do not expose admin operations without authentication.
- Define item fields for product identity, category, condition, total/reserved quantity, availability, asking price, notes, and timestamps.
- Support item, package, and barcode photos from a phone. Keep uploads private by default; an owner/helper explicitly selects which product photos may appear publicly.
- Provide an owner-managed category list for consistent inventory entry and public filtering.
- Model private inventory location separately from public garage-sale location and dates; never expose room/shelf/bin details to buyers.
- Build owner-facing create, edit, and review screens.
- Build a public, read-only catalog with item-name search and category/availability filters.
- Include owner-configured garage-sale events, exact address, and buyer text/email contact actions in the public experience; stop showing an event's address outside its active dates.
- Seed a small sample inventory and verify the flow on a phone-sized screen.

**Exit:** An owner can manually add and update an item, publish it, and a buyer can find it in the catalog.

### 2. Add assisted intake, identification, and price research

- Add a scan-first intake workspace where an owner/helper can stage product, packaging, and barcode photos before creating a permanent inventory item. Staged photos remain private and can be removed or expire if the intake is abandoned.
- Accept a manually entered or corrected barcode and manufacturer part number (MPN) independently. Keep MPN distinct from the product's model field, and carry both identifiers into a private draft.
- Let the owner choose which staged photos and barcode/identifier to send to external providers. Do not send private notes, storage locations, account data, or unrelated photos.
- Let the owner select one provider method per run. Preserve prior candidates so vision, OCR, product catalogs, and pricing research can be run sequentially after the owner reviews or corrects identifiers.
- Read a UPC/EAN from a barcode image where possible using local barcode/OCR software, and allow manual correction or entry. Treat OCR and vision-extracted identifiers as unverified until confirmed.
- Use a replaceable provider adapter for image-capable models and product/barcode catalogs. Candidate providers include Google Gemini, OpenAI vision models, GroqCloud, UPCitemdb, and UPCDatabase.org. Keep provider and model identifiers configurable rather than hard-coding a model name whose availability may change.
- Use one vision provider for the normal identification pass. Make a second model an optional fallback or explicit comparison, not an automatic call for every intake.
- Query barcode catalogs using the confirmed or proposed UPC/EAN. Permit independent provider failures and rate limits; a failed or unavailable service must not discard the staged intake or block manual entry.
- Search eBay separately for active listings and, if access is approved, sold-item evidence. Label active asking prices distinctly from completed-sale prices; do not present either as guaranteed retail value or as the owner's asking price.
- Return results in an owner-review list. Each candidate should identify its provider/source and include available product name, brand/model, identifiers, category, description, product link, price amount/type/currency, retrieval date, and confidence or match rationale. Show conflicting and low-confidence results clearly; do not invent missing values.
- Allow selecting, correcting, or ignoring suggestions field by field and selecting which price evidence to keep. Preserve the chosen source, URL/identifier, price type, currency, and lookup date with accepted evidence.
- Create an unpublished draft only after the owner confirms the desired fields. The owner remains responsible for condition, quantity, physical storage location, and garage-sale asking price. Never auto-publish or replace the asking price with a provider's estimate.
- Keep provider credentials server-side, use timeouts and bounded retries, respect each provider's terms and rate limits, and report provider errors visibly. Do not expose credentials or unrestricted raw provider responses to the browser.

**Exit:** An owner can stage photos, compare independently sourced identification and price candidates in a selectable review list, and create a private draft from only the information they approve. Provider failures leave manual intake available; nothing is silently accepted or published.

### 3. Prepare the catalog for buyers

- Refine mobile browsing, readable item cards/details, and useful filters.
- Show condition, availability, asking price, and clearly labeled retail/market comparisons with source/date where useful.
- Provide a simple contact or sale-location path without exposing private owner data.
- Show configured public sale events and dates; keep each item's storage path private.
- Show each event's exact address only during that event's active dates and verify that it disappears outside that window.
- Confirm unpublished, unavailable, and sold items behave as intended.
- Keep sold and hidden listings out of the public buyer view while preserving them in private inventory history.

**Exit:** A local buyer can browse the available items and understand what is being offered.

### 4. Deploy and improve

- Configure deployment, automated local backups, secrets, and access protection for owner/helper operations.
- Test restoring both the database and uploaded photos from backup.
- Test the full workflow, public/private data boundaries, and recovery from lookup failures.
- Use real inventory feedback to prioritize bulk intake, additional providers, or operational features.

**Exit:** The app is usable in its intended setting and can be maintained without losing inventory data.

## First implementation slice

Start with Milestone 1. Keep the first slice manual: an authenticated owner/helper can add an item, attach photos, set its condition and asking price, publish it, and find it through the public catalog. This validates the data model and buyer experience before adding AI or paid/external integrations.

### Suggested v1 data boundaries

- **Inventory listing:** Product name, brand/model and identifier when known; category; description; condition; total quantity; reserved quantity; sold quantity; remaining available quantity; availability; asking price; private notes; created/updated timestamps. Keep quantity changes consistent; derive remaining quantity rather than maintaining conflicting counts.
- **Photos:** Item, package, and barcode images associated with a listing. Private by default; only owner-selected product photos are exposed in public listings.
- **Price evidence:** Retail or resale estimate kept separately from asking price, with amount, source/link, evidence type, and lookup date. Optional in the first manual-only slice.
- **Assisted intake:** A private, temporary intake record for staged photos and submitted identifiers, separate from permanent inventory. Retain enough provider/source and result metadata to review candidates, show errors, and record selected evidence; expire or delete abandoned intake data according to a documented retention rule.
- **Identification and price candidates:** Keep provider results as reviewable candidates rather than treating them as item facts. Store source/provider, retrieval time, confidence or match notes when available, product link, and typed price evidence (for example, retail reference, active listing, or completed sale). Only owner-approved values move to the draft item or its price-evidence records.
- **Storage location:** Private hierarchical location, such as room → shelf → bin. Never include this field in public responses.
- **Sale details:** Multiple owner-managed sale events, each with dates and public location details. Publish an event's exact address only during that event's active window. Contact actions open the buyer's SMS/email app with a prefilled message including the item name and catalog link. Keep contact details out of the public page when not configured.
- **Publication:** Draft/published state is separate from availability. Public results include published `Available` and `Reserved` listings with their status labeled; `Sold` and `Hidden` records remain in private inventory.

### Public/private field contract

- **Public when published:** Product name, category, description, condition, asking price, remaining quantity/status, owner-selected product photos, and configured buyer contact actions.
- **Public only during an active sale event:** That event's dates and exact address.
- **Private:** Admin identity/access data, private notes, room/shelf/bin location, package/barcode photos unless explicitly selected for publication, and lookup/debug details.
- Implement public catalog responses as an explicit allowlist; do not serialize the full private inventory record.

### Build order for the first slice

1. Establish the FastAPI application, SQLite persistence, and Bokeh UI boundaries.
2. Implement the item model, fixed condition and availability values, managed categories, and private storage locations.
3. Add authenticated owner/helper item create/edit workflows, phone photo uploads, and explicit product-photo publication; keep public operations read-only.
4. Add the public catalog, item-name search, category/availability filters, and sale details/contact actions.
5. Add focused tests for publication rules, reserved/available quantity consistency, image visibility, private-field exclusion, sale-address date gating, and catalog search/filtering.
6. Manually verify the workflows on a desktop and phone-sized viewport.

### Assisted intake implementation order

Status: Steps 1–5 now have an initial implementation: private staging, sequential live-provider review, manually editable UPC/MPN, explicit selected-photo vision calls, local barcode/OCR, product catalogs, and active eBay listing evidence. Live integrations still require owner-supplied credentials and local Tesseract installation; they have only been exercised with mocked responses, not against live accounts.

1. [x] Define temporary intake and candidate-result boundaries, retention/cleanup behavior, private photo handling, and typed price-evidence fields before connecting external providers.
2. [x] Build the scan-first staging and review UI; verify that provider candidates are selectable, editable, dismissible, and never create or publish inventory without confirmation.
3. [x] Add local barcode/OCR extraction with owner confirmation; add UPCitemdb and UPCDatabase.org adapters and show their results as separate sources, including rate-limit and no-match states.
4. [x] Add configurable Gemini, OpenAI, and GroqCloud vision providers; require explicit photo selection for each call and run only the owner-selected model.
5. [x] Add eBay active-listing comparisons, clearly labeled as asking prices. Completed/sold history remains unavailable unless official restricted access is granted.
6. [ ] Test provider timeouts, invalid credentials, rate limits, conflicting candidates, no matches, abandoned intake cleanup, and that secrets/private fields never reach public responses.

## Decisions to revisit before internet deployment

- Which sign-in provider and helper invite/revocation flow will be used?
- Will internet access use a tunnel service or router port forwarding?
- Is off-site backup needed in addition to the chosen local backup?

## Initial acceptance checks

- Draft items are not visible in the public catalog.
- Sold and hidden items do not appear in public search results.
- Each garage-sale address is visible only during its configured active event dates.
- Published items show accurate condition, availability, and asking price.
- Remaining quantity is accurate when units of a listing are reserved or sold.
- Search and filters work on both desktop and phone-sized screens.
- Missing or uncertain identification/pricing never becomes a fabricated fact.
- An owner can edit suggestions, recover from lookup errors, and manually enter an item.
- Staged photos and lookup results are private, and an owner can choose which photos leave the home server for a provider request.
- Product identification, retail references, active eBay asking prices, completed-sale evidence, and the owner's asking price remain clearly distinct.
- Provider candidates cannot alter permanent inventory, the owner's asking price, or publication status without explicit owner selection and confirmation.
- A provider timeout, rate limit, or no-match result does not lose staged intake or prevent manual item creation.
- Each live provider call is an explicit single-method action; photo uploads to cloud models include only photos checked for that run.
- Private notes, credentials, and admin functions are not exposed to public visitors.
