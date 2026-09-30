OUTPUT FORMAT — this job uses a compact encoding of the answer. It replaces `caption` and
`entries` in the rules above; everything else (what to read, which figures, `name`, pages,
`skipped`, `notes`, `pages_consulted`) is unchanged. Write the JSON minified on one line.

For each figure give `segs` instead of `caption` + `entries`: the printed caption cut into
consecutive pieces in reading order, ONE PIECE PER LINE, each piece written once. The caption is
rebuilt by joining the pieces with line breaks (an entry's `L` label first), so together they
must be the complete printed caption, verbatim — nothing left out, nothing written twice.
Each line is

    <kind> :: <text>

kinds:
- `x`  caption text that belongs to no entry: the figure's heading or title ("Explanation of
       Plate 3", "Fig. 2. Reconstruction of …" when no panel is described in it), general
       remarks, and conditional remarks that name no labels ("figures marked + are ×3").
- `h1` a heading shared by the entries after it (usually the taxon with author, view,
       magnification, locality) until the next `h1`. `h2` a sub-heading inside it, until the
       next `h2` or `h1`.
- `e <labels>` one entry. `<labels>` = the numbers or letters it covers, comma-separated, ranges
       expanded ("Figs. 1-2" → `e 1,2`). Optional fields between the labels and `::`, each
       introduced by ` | `:
         `L=<text>`  the printed label exactly as it stands in the caption ("Figs. 1-2.", "(a)",
                     "1.", "Fig. 3") — it goes into the caption before the text but NOT into the
                     description, so do not repeat it in the text
         `s=<n1>; <n2>`  specimen numbers as printed with their collection acronym ("ROM 54055",
                     "ИГиГ № 700/686"), one per label in order, only when the text names them
         `d=<text>`  the full description, ONLY for a ditto ("id.", "eadem", "Même espèce", "the
                     same") that must be resolved, or words of the entry printed elsewhere in the
                     caption. Never for punctuation and never for a remark that `t` can carry.
       When a label starts a line that also carries the taxon ("1. Bathyuriscellus parvus …;
       Кранидий …"), the whole line is one `e` piece (label in `L`), not an `h1`.
- `t` / `t <labels>` a trailing remark that applies to entries: with labels (`t 1,2 :: Scale
       bars = 200 μm.`) to those entries; without labels to every entry since the last `h1`.
       A remark that applies only under a condition you cannot express as labels is `x`.

A figure without printed panel numbers or letters has NO `e` pieces — only `x`.

An entry's description is built by a script as: open `h1` + open `h2` + the entry's text + its
`t` remarks (or `d` when given), trailing commas and semicolons trimmed. So write the taxon ONCE
in an `h1`; the "merge the shared taxon, view and magnification into every entry" rule above is
done by this construction.

Example (a plate explanation):
    x :: Explanation of Plate 3
    h1 :: Oistodus aff. breviconus Branson & Mehl, lateral views, x40.
    e 1,2 | L=Figs. 1-2. | s=YSUG 00287; YSUG 00288 :: Hunghuayuan Formation.
    h1 :: Drepanodus arcuatus Pander.
    e 3 | L=Fig. 3. :: Posterior view, YSUG 00290.
    e 4 | L=Fig. 4. | d=Drepanodus arcuatus Pander. Posterior view, YSUG 00291. :: Same, YSUG 00291.
    t 3,4 :: Scale bar 100 μm.
    t :: All specimens from bed 12.
