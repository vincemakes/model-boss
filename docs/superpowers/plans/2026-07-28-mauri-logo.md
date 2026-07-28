# Mauri Logo Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce three count-agnostic, strictly symmetric, editable SVG logo candidates for Mauri and a fair comparison page.

**Architecture:** Each candidate is a standalone SVG with a shared `0 0 256 256` view box and reusable geometry transformed around the exact center. A separate HTML page embeds the SVG files at multiple sizes without duplicating their source.

**Tech Stack:** SVG 1.1-compatible markup, HTML/CSS, `xmllint`, ImageMagick or macOS Quick Look for optional raster verification.

---

## Chunk 1: Vector assets

### Task 1: Create the continuous-weave candidate

**Files:**
- Create: `media/mauri-logo/mauri-weave.svg`

- [ ] **Step 1: Define a 256-unit SVG canvas and a centered closed path**
- [ ] **Step 2: Build the final mark from exact rotations around `(128, 128)`**
- [ ] **Step 3: Keep the central counterform open and remove all visible endpoints**
- [ ] **Step 4: Run `xmllint --noout media/mauri-logo/mauri-weave.svg`**
- [ ] **Step 5: Confirm the SVG contains no text, raster image, gradients, filters, masks, or scripts**

### Task 2: Create the orbit-kernel candidate

**Files:**
- Create: `media/mauri-logo/mauri-orbit.svg`

- [ ] **Step 1: Define one closed orbital primitive**
- [ ] **Step 2: Use exact transforms to create a balanced count-agnostic silhouette**
- [ ] **Step 3: Preserve a central negative-space kernel and generous exterior margin**
- [ ] **Step 4: Run `xmllint --noout media/mauri-logo/mauri-orbit.svg`**

### Task 3: Create the bounded-flow candidate

**Files:**
- Create: `media/mauri-logo/mauri-flow.svg`

- [ ] **Step 1: Define a closed architectural loop with softened inner corners**
- [ ] **Step 2: Mirror or rotate the geometry for exact symmetry**
- [ ] **Step 3: Check that no repeated unit resembles a literal port or fixed agent slot**
- [ ] **Step 4: Run `xmllint --noout media/mauri-logo/mauri-flow.svg`**

## Chunk 2: Comparison and verification

### Task 4: Build the comparison page

**Files:**
- Create: `media/mauri-logo/index.html`

- [ ] **Step 1: Add a three-column large-mark comparison**
- [ ] **Step 2: Add 64px and 16px icon rows on white and black fields**
- [ ] **Step 3: Label each option outside the SVG; keep the logo assets text-free**
- [ ] **Step 4: Open the page through the visual companion**

### Task 5: Verify the deliverables

**Files:**
- Verify: `media/mauri-logo/*.svg`
- Verify: `media/mauri-logo/index.html`

- [ ] **Step 1: Run `xmllint --noout media/mauri-logo/*.svg media/mauri-logo/index.html`**
- [ ] **Step 2: Search for forbidden SVG constructs with `rg '<(text|image|linearGradient|radialGradient|filter|script)' media/mauri-logo/*.svg` and expect no matches**
- [ ] **Step 3: Inspect all candidates at large, 64px, and 16px sizes**
- [ ] **Step 4: Commit the SVG assets and comparison page**
