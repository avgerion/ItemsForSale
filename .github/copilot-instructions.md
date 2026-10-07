# Project Instructions

## Goal

Build a lightweight inventory and sales site for items from Home Depot, Lowe's, and Amazon returns. The owner should be able to identify and record items from photos of the item, packaging, or barcode, review estimated value and pricing evidence, and publish approved inventory in a searchable catalog for local buyers.

Use [I have a bunch of items to sell.md](../I%20have%20a%20bunch%20of%20items%20to%20sell.md) as the initial product brief. Its named frameworks, APIs, and price examples are suggestions to evaluate, not fixed technology or pricing requirements.

## Product Strategy

Implement in small, verifiable stages:

1. **Inventory foundation:** Define a clear item record and storage for product identity, category, condition, quantity, availability, asking price, and timestamps. Keep original retail value, current resale-market evidence, and the owner's asking price as distinct fields.
2. **Private intake:** Make adding an item practical on a phone. Accept item, box, and barcode photos; support manual entry and correction. Treat AI identification and price lookups as suggestions, never as verified facts or automatic publication.
3. **Research and review:** Use barcode/product lookup and market-price sources where useful. Retain the source, link, and retrieval date for price evidence; distinguish active listings from completed sales. Surface uncertain matches and missing evidence for owner review. Do not imply an estimated or historical price is a guaranteed current retail value.
4. **Public catalog:** Publish only owner-approved items. Provide responsive search by name and useful filters such as category, condition, and availability. Keep internal notes, credentials, and private operational data out of public responses.
5. **Improve from use:** Favor a working end-to-end path with a small inventory before adding bulk processing, advanced AI, or complex deployment.

## Implementation Guidance

- Keep intake, identification, pricing research, persistence, and public browsing separable so providers and interfaces can change without replacing the inventory data.
- Use FastAPI for the backend, SQLite for persistence, and Bokeh for the frontend. Keep these boundaries clear and testable; do not add external services before they are needed.
- Make uncertain or failed lookups visible and recoverable; preserve manual overrides and avoid silently substituting invented product or price data.
- Protect private administration and API credentials. Load secrets from an appropriate local or deployment secret store; never commit them or expose them to the public catalog.
- Do not expose owner/helper routes until authentication is configured; require HTTPS for production administration.
- Design for mobile photo capture and garage-sale browsing, including readable product details, clear condition and price, and usable touch targets.
- Keep room/shelf/bin locations, private notes, package/barcode photos, and unpublished inventory out of public responses. Publish product photos only when explicitly selected.
- Keep public listing content accurate: identify condition clearly and label retail comparisons and market estimates with their source and date where available.

## Validation

- Test the complete flow from item entry through review, saving, and public search/filtering.
- Cover barcode/photo lookup failures, ambiguous identification, missing prices, manual corrections, and unpublished items.
- Verify that public views expose only approved listing fields and work at phone-sized widths.
- Before adopting a suggested provider or API from the product brief, verify its current capabilities, terms, cost, rate limits, and data coverage.
