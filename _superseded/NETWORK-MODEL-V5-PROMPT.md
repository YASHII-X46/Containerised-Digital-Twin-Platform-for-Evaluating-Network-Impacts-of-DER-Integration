# Claude Code build prompt: Swinburne Hawthorn network model v5.0

**How to use this file.** Open a terminal at the repository root
(`.../Final Year Project/New-stack-5.0`), start Claude Code, and paste
everything from the `=== PROMPT STARTS HERE ===` line to the end of the file as
your first message. Work through it phase by phase and approve each gate.

This prompt is a companion to `EMS-BMS-BUILD-PROMPT.md`. The network model work
here creates the buses, ratings, and DER records that the EMS and BMS layer
needs. Do this one first if you are running both.

---

=== PROMPT STARTS HERE ===

You are working in the DT-Stack v5.0 repository. Your task is to upgrade the
Swinburne Hawthorn campus network model from v3.0 to a v5.0 pair of CGMES 3.0
(CIM100) and PSS/E RAW v36.5 files, together with the importer, audit, diagram,
documentation, and test changes that the new model content requires. There is no
intermediate version, v5.0 is the only new model produced.

Four things change: zone-level buses inside ATC and AMDC, the real DCH5 DER at
ATC, operational limits in the CIM file, and unbalanced phase and earthing
detail. Sections 5 to 8 specify each one.

Do not start writing code yet. Follow the phase plan in section 14.

---

## 1. Read before you write

| Purpose | Files |
|---|---|
| The model, its provenance, and its honesty statements | `sample-networks/README.md` in full |
| The single source of truth you will edit | `sample-networks/generate_swinburne_v3.py` |
| The current outputs | `sample-networks/swinburne_hawthorn_v3.0.xml`, `sample-networks/swinburne_hawthorn_v3.0.raw` |
| Existing checks you must extend | `sample-networks/audit_models.py` |
| Diagram generators that parse the CIM | `sample-networks/generate_sld.py`, `sample-networks/generate_sld_geo.py` |
| Importers you will upgrade | `simulation-engine/app/network/importers.py` (`from_cim`, `from_raw`, `from_rawx`, `_assemble`) |
| Schema and validation you may extend | `simulation-engine/app/network/model.py` |
| Importer regression tests | `simulation-engine/tests/test_importers.py` |
| Real-data intake templates | `sample-networks/real-data-templates/` |
| The physical DER being added | `../Report_DCH5_Final-Project.pdf`, section 2.1 Table 1, section 4, section 7.1 |
| Downstream consumer of this work | `EMS-BMS-BUILD-PROMPT.md` in the repository root |

Write `sample-networks/MODEL-V5-DESIGN.md` before coding. That is the phase 0
deliverable.

---

## 2. Ground rules

1. **One source of truth.** Everything is generated from
   `generate_swinburne_v*.py`. Never hand-edit the XML or RAW. If a value cannot
   be expressed in the generator's data dictionaries, extend the dictionaries,
   not the output.
2. **v3.0 is frozen.** Leave `swinburne_hawthorn_v3.0.xml`, `.raw`, both SVGs,
   and `generate_swinburne_v3.py` exactly as they are. They are the regression
   baseline and the thesis before-case. Create
   `generate_swinburne_v5.py` and the v5.0 output files alongside them.
3. **Bus numbering is append-only.** Buses 1 to 17 keep their existing ids,
   names, voltages, and positions. Every new bus takes id 18 upward. This keeps
   old scenarios, saved UI configurations, and any pinned test numbers valid.
4. **Load is conserved.** Splitting a building's load across zone buses must
   preserve the building total exactly, in both kW and kvar. Print the check.
5. **No invented precision.** Every new number is either taken from the DCH5
   report, computed from documented conductor or transformer data, or clearly
   labelled representative in both the code comment and the README table. The
   existing README has a grounded-versus-representative table, every value you
   add gets a row in it.
6. **The equivalence claim must be restated, not quietly broken.** Today the
   README claims the CIM and RAW files are equivalent to 7e-6 pu. After this
   work the CIM file carries phase, earthing, and limit detail that RAW cannot
   express. The new claim must be precise: identical in the balanced
   positive-sequence solve, with a named list of what each format carries that
   the other does not. Verify the new delta and publish the number.
7. **The power flow must not change for content that is only metadata.** Adding
   DER records and operational limits must not shift a single bus voltage in a
   run that does not enable DER. Prove it with a test.
8. **Existing tests stay green.** All 332 tests pass unchanged, including the
   v3.0 importer regressions.

---

## 3. Two known defects to resolve while you are in here

Both are in the current model. Decide, document, and fix.

1. **0.4 kV versus 0.415 kV.** `generate_swinburne_v3.py` sets `LV = 0.4` while
   every campus substation name says `11/0.415 kV`. Australian distribution
   transformers are commonly 11/0.433 or 11/0.415 kV nominal secondary feeding a
   400/230 V system, so both numbers are defensible but they must not disagree
   inside one model. Pick one convention, apply it to the transformer `ratedU`,
   the bus `nominalVoltage`, the RAW `BASKV`, and the per-unit base, and explain
   the choice in the README. Report the effect on reported per-unit voltages,
   it is not zero.
2. **Public LV boards versus campus LV boards share a nominal.** The Glenferrie
   and residential boards are genuinely 0.4 kV public LV while the campus boards
   are the customer's own. If you change the campus nominal, do not change the
   public ones by accident.

---

## 4. Target topology overview

After this work the model is roughly 30 buses across two voltage levels, with
the campus embedded network expanded inside ATC and AMDC:

```
bus 1   CitiPower 66/11 kV zone substation 11 kV busbar      [SOURCE, OLTC]
bus 2   11 kV feeder junction
bus 3   11 kV feeder node east
bus 4   Swinburne campus HV point of supply
  ├─ tx_atc  1250 kVA  → bus 6  ATC main switchboard
  │                        ├─ ATC101 zone board        [new]
  │                        ├─ ATC103 zone board        [new]
  │                        ├─ ATC206 zone board        [new]
  │                        ├─ ATC balance of building  [new]
  │                        └─ ATC DER connection point [new, DCH5 PV/BESS]
  └─ tx_amdc 1000 kVA  → bus 7  AMDC main switchboard
                           ├─ AMDC301 zone board       [new]
                           ├─ AMDC303 zone board       [new]
                           ├─ AMDC355 zone board       [new]
                           ├─ AMDC451 zone board       [new]
                           └─ AMDC balance of building [new]
bus 5   campus internal 11 kV node → EN/EW, AGSE, AS/FS, sports (unchanged)
buses 12 to 17  Glenferrie and residential (unchanged)
```

ATC and AMDC remain on **separate transformers from the same HV point of
supply**, which is the real arrangement and the reason the DCH5 project could
not physically share energy between the two buildings. Do not merge them, and
make sure the transformer boundary is unambiguous in the exported files so the
EMS work in `EMS-BMS-BUILD-PROMPT.md` can derive its sharing constraint from the
network rather than from configuration.

---

## 5. Workstream A: zone-level buses inside ATC and AMDC

### 5.1 Which zones

Use the instrumented zones from the DCH5 report, they are the ones with real
sensors and therefore the ones a validation study can use:

ATC101, ATC103, ATC206, AMDC301, AMDC303, AMDC355, AMDC451.

The report describes them as large lecture theatres and private study areas,
selected for dynamic and stochastic occupancy. Each building also gets a
**balance of building** bus carrying everything not separately modelled, so the
building total is preserved without pretending the whole building is
instrumented.

### 5.2 Load split

Derive each zone's share from floor area and use type, not from a guess. State
the method in a comment and in the README. A defensible approach:

- Assign each zone a use type (lecture theatre, private study, teaching lab,
  office) and a floor area.
- Apply an Australian tertiary-building energy intensity per use type in W/m2 to
  get an indicative peak, and cite the source you use (NABERS or NCC Section J
  based figures are acceptable, name which).
- Scale all zone shares so they sum to at most the building's instrumented
  fraction, and put the remainder on the balance bus, so ATC still totals 900 kW
  and 300 kvar and AMDC still totals 700 kW and 230 kvar.
- Keep the same power factor per zone as the parent building unless you have a
  reason to differ, and say so.

The zone loads exist to be **overwritten by real interval data**. Add a
`meter_map.csv` style row per zone to `real-data-templates/` so a Swinburne BMS
or meter export drops straight in.

### 5.3 Submain electrical data

Each zone board hangs off the building main switchboard through an LV submain:

- Conductor from the generator's existing documented conductor tables, extended
  if needed with building submain sizes (for example 4-core 185 or 240 mm2
  copper), with the source of the R and X per km named in the table.
- Route lengths in the 20 to 150 m range, chosen to reflect the floor the zone
  is on (a level 4 zone has a longer riser than a level 1 zone), and labelled
  representative.
- Ratings from the protective device size (for example a 400 A or 630 A
  submain), converted to kVA at the LV nominal.
- Zero-sequence impedance per section 8.

Because these are short LV runs, the added voltage drop should be small but not
zero. **Publish the new minimum bus voltage** and confirm it stays inside the
0.95 to 1.05 pu band at coincident peak. If it does not, the submain sizing is
wrong, fix the sizing rather than the limit.

### 5.4 Geography

Every zone bus sits inside its parent building, so its WGS84 position is within
tens of metres of the parent. Give each a distinct position anchored on the real
building footprint, keep the audit's rule that every line's crow-fly distance is
less than or equal to its route length, and make sure the geographic SLD's
campus detail inset can still be read at that zoom. If it cannot, add a
building-level inset.

---

## 6. Workstream B: the DCH5 DER at ATC

### 6.1 What is physically there

From `../Report_DCH5_Final-Project.pdf` section 2.1, installed on the ATC West
Wing roof and in the ATC inverter room:

| Item | Value |
|---|---|
| PV modules | 84 x Trina Vertex TSM-390-DE09.08 at 390 W |
| Array rating | 32.76 kWp, 167 m2, 15 degree tilt |
| Inverters | 3 x Sungrow SH10RT hybrid, 10 kVA each, 30 kVA total |
| Battery | 3 x BYD HVS 10.2, 30.6 kWh total |
| Operating floor | 20 percent state-of-charge backup reserve |
| Compliance | AS 4777.1, AS 4777.2, AS 5033, AS 3000, AS 3008, AS 1170.2 |

All three inverters are physically in the ATC inverter room. The report assigns
them **logically** to ATC generation, AMDC generation, and a shared community
battery. That assignment is an experiment construct, not an electrical fact.
Model the electrical reality (all at ATC) and record the logical assignment as
metadata, clearly labelled. Do not fabricate an AMDC PV array that does not
exist.

### 6.2 State the scale honestly

32.76 kWp against an ATC peak of 900 kW is about 3.6 percent. The DCH5 report
says exactly this, that on-site generation was inadequate relative to building
loads, which is why the project built a hardware emulator. Put that ratio in the
README next to the DER table. If a study wants a high-penetration campus, that
belongs in a scenario knob or a clearly named what-if variant, never as inflated
numbers inside the base model.

### 6.3 CIM encoding

Use the CIM100 power-electronics classes, contained in the ATC substation and
terminated on the ATC LV connectivity node:

- `PowerElectronicsConnection` with `ratedS` (30 kVA total, or three connections
  of 10 kVA each, pick one and justify), `p`, `q`, `maxQ`, `minQ`, and the
  `BaseVoltage` reference.
- `PhotoVoltaicUnit` as a `PowerElectronicsUnit` with `maxP` set to the array
  rating.
- `BatteryUnit` with `ratedE` 30.6 kWh, `storedE`, and `batteryState`.
- `OperationalLimitSet` on the connection per workstream C.
- A `Location` and `PositionPoint` on the ATC roof.

If you choose three separate connections, name them so the ATC, AMDC, and
community roles from the report are visible in the identifiers, with a comment
that the AMDC and community roles are logical.

### 6.4 RAW encoding

RAW v36 has no storage class, so:

- Write `GENERATOR DATA` records at the ATC LV bus with `MBASE` equal to the
  inverter rating, `PT` and `PB` limits, `QT` and `QB` from the AS/NZS 4777.2
  reactive capability (44 percent of rating is the figure the stack already uses
  for commanded VAr), and the renewable machine mode and power factor fields
  set appropriately for a non-synchronous inverter.
- Represent the battery's ability to charge with a negative `PB`, and add a
  comment stating plainly that **RAW cannot carry the 30.6 kWh energy capacity
  or the 20 percent reserve**, so those live in the companion scenario preset.
- Do not invent a RAW section that does not exist in the specification.

### 6.5 How DER reaches the rest of the stack

This is the important design decision, get it agreed at the pass 0 gate.

The `NetworkModel` schema deliberately has no DER concept: DER is assigned per
scenario through the Load Engine, and the network model carries topology and
electrical data only. Adding generation to the model files must not smuggle
dispatchable generation into the power flow behind the scenario controls.

Implement it as an **optional, inert hint**:

- Extend the per-bus schema in `simulation-engine/app/network/model.py` with an
  optional `der` block, for example
  `{"pv_kw": 32.76, "inverter_kva": 30.0, "unit_count": 3, "bess_kwh": 30.6, "bess_kw": 30.0, "bess_soc_min": 0.2, "source": "cim"}`,
  validated for type and sign, defaulting to absent.
- The importers populate it when the source file carries the records.
- The QSTS solve **ignores it entirely**. A network with a `der` block and a
  network without one solve identically, and there is a test that proves it.
- The UI reads it to pre-fill the DER controls when that network is selected,
  and shows a marker on the topology map, so uploading the campus model offers
  the real installed system as the starting scenario rather than a blank form.
- The EMS and BMS work consumes it as the site asset preset.

Also ship a companion `sample-networks/swinburne_hawthorn_v5.0_scenario.json`
carrying what the electrical formats cannot: battery energy and reserve, the
inverter model name, the tilt and area, the zone-to-building mapping, and the
logical ATC/AMDC/community role assignment. Document that this file is the
scenario preset, not a network model.

---

## 7. Workstream C: operational limits in the CIM file

Today the README states that CIM branch ratings default to 100 MVA, so thermal
loading percentages are meaningless in the CIM path and only the RAW file is
usable for hosting-capacity and thermal work. Close that gap.

- Emit `OperationalLimitType` definitions (at minimum a normal continuous rating
  and one short-term rating, with `acceptableDuration` and `direction`).
- Emit `OperationalLimitSet` per `ACLineSegment`, per `PowerTransformerEnd`, and
  per `PowerElectronicsConnection`, with `CurrentLimit` in amperes and, where it
  is the more natural expression, `ApparentPowerLimit` in volt-amperes.
- Use the same physical ratings the RAW file already carries, converted
  correctly at each voltage level. Print the conversion in the audit so the two
  files can be checked against each other.
- Upgrade `from_cim` to read the limits into `rating_kva`, preferring the normal
  continuous rating, falling back to today's behaviour when no limit set exists
  so every existing CIM file keeps importing.

Acceptance: importing the v5.0 CIM file and the v5.0 RAW file produces the same
`rating_kva` on every branch, to within a stated tolerance, and the same thermal
loading percentages on an identical run.

---

## 8. Workstream D: unbalanced phase and earthing detail

The engine already supports per-bus `phases`, explicit `r0_ohm` and `x0_ohm`,
transformer `connection` of `wye_wye` or `delta_wye`, fixed `tap`, and `oltc`.
The model currently uses almost none of it, so the stack's unbalanced mode and
its VUF KPI have nothing interesting to chew on. Fix that.

- **Phases.** Declare three-phase on all MV buses, LV boards, and campus zone
  boards. Where a single-phase connection is realistic (a residential street
  pillar, a small zone board), declare a single phase and distribute the
  assignments across a, b, and c so the phases stay roughly balanced overall.
  Document the rule you used.
- **Zero sequence.** Give every line explicit `r0_ohm` and `x0_ohm` rather than
  leaning on the 3x default, using documented conductor zero-sequence data where
  you have it and stating the assumption where you do not. Cables and overhead
  lines differ substantially here, be explicit about which each segment is.
- **Vector groups.** The campus and public distribution transformers are
  realistically Dyn11, which is the Australian norm for 11/0.4 kV distribution
  transformers. Set `delta_wye` on those and explain the earthing consequence in
  one paragraph, including what the stack's three-wire model can and cannot show.
- **Tap changers.** Put an `oltc` on the zone substation transformer boundary if
  the model represents it, and fixed off-load taps on the campus transformers
  where a real installation would have them set away from nominal. Keep the
  values inside the engine's accepted 0.8 to 1.2 pu range.
- **What RAW can carry.** RAW does express transformer winding connection angle,
  tap ratio, and tap-changer control fields, so encode the vector group and taps
  there. RAW cannot express per-bus phase membership or per-segment
  zero-sequence impedance in any standard way. Do not invent a convention. State
  the limitation in the README and in the generator comments, and make the RAW
  file the balanced twin of the CIM file rather than pretending otherwise.

Acceptance: an `unbalanced` solve on the v5.0 CIM model converges, produces a
non-trivial `max_vuf_pct`, and the balanced solve still matches the RAW file to
the tolerance published in section 2 rule 6.

---

## 9. Importer upgrades

In `simulation-engine/app/network/importers.py`:

- `from_cim`: read `OperationalLimitSet` and `CurrentLimit` or
  `ApparentPowerLimit` into `rating_kva`; read `PowerElectronicsConnection`,
  `PhotoVoltaicUnit`, and `BatteryUnit` into the per-bus `der` hint; read
  `PowerTransformerEnd.connectionKind` into `connection`; read `RatioTapChanger`
  into `tap` and `oltc`; read phase information where the file carries it.
- `from_raw`: read `GENERATOR DATA` into the `der` hint; read the transformer
  connection angle and tap fields into `connection` and `tap`; read tap-changer
  control fields into `oltc`.
- `from_rawx`: the same generator and transformer handling as RAW, since it is
  the JSON variant of the same data.
- Keep every parser **version-tolerant and additive**. A file without the new
  elements must import exactly as it does today. Unsupported statements stay
  skipped with a logged count, never silently.

In `simulation-engine/app/network/model.py`: validate the `der` block, keep it
optional, and make sure nothing in `NetworkModel` or the QSTS loop consumes it.

---

## 10. Audit upgrades

Extend `sample-networks/audit_models.py` with checks for everything new. It
should fail loudly rather than warn:

- Zone load conservation per building, in kW and kvar.
- Every zone bus is downstream of the correct building transformer.
- Submain conductor maths: R and X consistent with the stated conductor and
  length, same method as the existing check.
- Transformer utilisation recomputed with the zone split, still inside the
  design margin.
- CIM limit set present on every branch, and matching the RAW rating within
  tolerance.
- DER records present, and the CIM and RAW DER totals agreeing where both can
  express the quantity.
- Phase declarations valid, and the count per phase roughly balanced.
- Zero-sequence values present and physically plausible relative to positive
  sequence.
- Vector group and tap fields valid per the engine's validation rules.
- Geographic consistency for the new buses, including the crow-fly rule.
- A printed summary table of buses, branches, total load, total DER, minimum
  voltage, losses, and maximum transformer utilisation, suitable for pasting
  into the thesis.

---

## 11. Diagram upgrades

Both SLD generators parse the CIM, so they must keep working and must show the
new content:

- `generate_sld.py`: draw the zone boards under their building switchboards, and
  draw the PV array and battery at ATC with IEC 60617 symbols. Keep the drawing
  legible, use a building sub-block if the flat layout becomes crowded.
- `generate_sld_geo.py`: add a building-level inset if the campus inset can no
  longer resolve the zone buses, and place the DER at the real roof position.
- Regenerate both SVGs and confirm every value on them comes from the model.

---

## 12. Documentation

Rewrite `sample-networks/README.md` for v5.0. It is currently a good document,
keep its structure and its honesty, and update:

- The file table, with v3.0 marked as the frozen baseline and v5.0 as current.
- The grounded versus representative table, with a row for every new value
  including the DCH5 hardware (grounded, cite the report) and the zone splits and
  submain lengths (representative).
- The topology diagram and bus list.
- A new DER section with the hardware table, the 3.6 percent penetration note,
  and the explanation of the `der` hint and the companion scenario file.
- A rewritten CIM versus RAW section: what each format now carries that the other
  does not, and the re-measured equivalence number.
- The 0.4 versus 0.415 kV decision from section 3.
- The supported-formats table, updated for the new elements the importers read.
- The verification block, with the new bus and branch counts, voltage range,
  losses, and transformer utilisation range.

Also update `sample-networks/real-data-templates/README.md` for the new zone
meter rows, and add a short `sample-networks/MODEL-V5-DESIGN.md` recording the
decisions and their rationale.

---

## 13. Tests

In `simulation-engine/tests/test_importers.py` and a new
`simulation-engine/tests/test_network_v5.py` as appropriate, target at least 20
new tests:

- v5.0 CIM and v5.0 RAW import to the same buses, loads, branch impedances, and
  ratings, and solve to the same voltages within the published tolerance.
- v3.0 files still import identically to today, byte-for-byte in the parsed
  dictionary where that is reasonable.
- CIM operational limits read into `rating_kva`; a CIM file without limits still
  falls back to the old behaviour.
- CIM `PowerElectronicsConnection`, `PhotoVoltaicUnit`, and `BatteryUnit` read
  into the `der` hint; RAW `GENERATOR DATA` likewise; RAWX likewise.
- The `der` hint changes no solve result: identical KPIs with and without it.
- Transformer `connection`, `tap`, and `oltc` round-trip from both formats.
- Zone bus load conservation, asserted against the building totals.
- An unbalanced solve on the v5.0 CIM model converges and reports a non-zero
  `max_vuf_pct`.
- Geographic positions identical between CIM and RAW for every bus, extending
  the existing `test_cim_gl_positions` and `test_raw_substation_geo`.
- The audit script runs clean on the generated pair, invoked from a test.

---

## 14. Phase plan and gates

Five phases producing one model, v5.0. Every file you emit is named v5.0 from
the first phase, there is no intermediate version to rename or retire later.
Stop at every gate and wait for my approval.

**Phase 0, design.** Read section 1, write `sample-networks/MODEL-V5-DESIGN.md`
covering: the zone list and load-split method, the DER encoding choice (one
connection or three), the `der` hint schema, the limit classes you will emit,
the phase assignment rule, the 0.4 versus 0.415 kV decision, and the restated
equivalence claim. Flag anything in this brief you think is wrong. Gate: I
approve the design.

**Phase 1, the model.** Create `generate_swinburne_v5.py` as a copy of the v3.0
generator, implement all four workstreams, and emit
`swinburne_hawthorn_v5.0.xml` and `swinburne_hawthorn_v5.0.raw`. Extend the
audit and get it passing. Import both through the stack and run a balanced and
an unbalanced power flow. Gate: the audit is clean, both files import, both
solve, and you report bus count, branch count, voltage range, losses,
transformer utilisation, VUF, and the CIM-versus-RAW delta.

**Phase 2, importers and tests.** Upgrade the CIM, RAW, and RAWX importers and
the schema, add the tests in section 13, keep the 332 existing tests green.
Gate: full test suite green, new count reported.

**Phase 3, diagrams and documentation.** Regenerate both SVGs, rewrite the
README, update the templates. Gate: I can read the drawings and the README
matches the model.

**Phase 4, end to end.** `docker compose up --build`, upload the v5.0 model
through the browser, run a scenario with the pre-filled DCH5 DER, and show the
topology map with the zone buses and the DER marker. Fold in any review feedback
from the earlier gates and regenerate, so the committed files are the reviewed
ones. Gate: the folder contains exactly two model pairs, v3.0 frozen and v5.0
current, and the run completes.

At each gate report what changed, what you had to assume, and anything you found
in the existing model or code that is wrong.

---

## 15. Acceptance numbers to publish

Put these in the README verification block and in `MODEL-V5-DESIGN.md`:

- Bus count, branch count, transformer count, voltage levels.
- Total base load in kW and kvar, unchanged from v3.0 at the building level.
- Total installed DER: 32.76 kWp PV, 30 kVA inverters, 30.6 kWh storage, and the
  penetration percentage against campus and against ATC alone.
- Balanced solve: minimum and maximum bus voltage in pu, total losses in kW and
  percent, maximum transformer utilisation percent.
- Unbalanced solve: the same, plus maximum VUF percent.
- CIM versus RAW maximum voltage delta in pu, and the list of what each format
  carries exclusively.
- Audit result, clean.
- Test counts before and after.

---

## 16. House style

- Match the existing generator's style: data dictionaries at the top with
  comments naming the data source, emit functions below, no magic numbers buried
  in string formatting.
- Inline comments are single-sentence `#` lines. No block comment banners.
- No em dashes in prose you write, use a comma instead.
- Documentation tables stay plain, no colour, no styling beyond existing
  markdown.
- Do not add Python dependencies. The generator, audit, and diagram scripts are
  standard library only today, keep them that way.
- Do not reformat files you are not otherwise changing.

---

## 17. Definition of done

- [ ] `generate_swinburne_v5.py` emits `swinburne_hawthorn_v5.0.xml` and `.raw`
      from one data model, guaranteed consistent
- [ ] v3.0 files, generator, and SVGs are untouched
- [ ] Buses 1 to 17 keep their ids, names, voltages, and positions
- [ ] ATC and AMDC carry the seven DCH5 instrumented zones plus a balance bus
      each, with load conserved exactly
- [ ] The real DCH5 PV, inverters, and battery appear in both formats, with the
      3.6 percent penetration stated plainly
- [ ] The CIM file carries operational limits, and thermal loading is now
      meaningful in the CIM path
- [ ] Phases, zero-sequence data, vector groups, and taps are present, and an
      unbalanced solve reports a non-zero VUF
- [ ] The `der` hint is inert: identical solve results with and without it
- [ ] Importers read the new elements and still import every old file unchanged
- [ ] `audit_models.py` passes and prints the thesis summary table
- [ ] Both SLDs regenerated from the CIM and readable
- [ ] `sample-networks/README.md` rewritten, including the restated CIM versus
      RAW equivalence claim with a measured number
- [ ] All 332 existing tests pass, plus at least 20 new ones

Begin with Phase 0.
