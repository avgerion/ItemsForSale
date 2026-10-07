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

### 2. Add assisted identification and price research

- Evaluate image-capable identification and product/barcode lookup providers.
- Return proposed product name, brand/model, category, identifiers, and source links for owner review.
- Read barcode images where possible; retain manual identifier entry as a fallback.
- Research original retail and resale-market evidence separately; distinguish active listings from completed sales.
- Store lookup provider/source and retrieval date; surface ambiguity, unavailable results, and errors.
- Let the owner accept, correct, or discard each suggestion.

**Exit:** Lookups help the owner complete an item record without automatically asserting facts or publishing it.

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
- Private notes, credentials, and admin functions are not exposed to public visitors.
